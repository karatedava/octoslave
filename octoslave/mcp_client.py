"""Model Context Protocol (MCP) client for octoslave.

Lets users wire in arbitrary external tools — filesystem servers, git, custom
scripts, remote HTTP tool servers — and expose them to every octoslave role
(single-agent runs, the research pipeline, and the web UI) alongside the
built-in tools.

Design notes
------------
The rest of octoslave is synchronous (the agent loop streams completions and
executes tools in a plain ``for`` loop). The official ``mcp`` Python SDK is
async-only, which is awkward to bridge into a sync loop and adds a heavy
dependency. The MCP wire protocol itself is just JSON-RPC 2.0, so this module
speaks it directly:

  * **stdio** transport — launch the server as a subprocess and exchange
    newline-delimited JSON-RPC over its stdin/stdout. A background reader thread
    demultiplexes responses by request id, so calls are blocking + thread-safe.
  * **http** transport — JSON-RPC over HTTP POST (the "Streamable HTTP"
    transport), handling both ``application/json`` and ``text/event-stream``
    responses and the ``Mcp-Session-Id`` session header. Uses ``requests``,
    which is already a dependency.

Tools are namespaced ``mcp__<server>__<tool>`` so they never collide with the
built-in tool names and the routing layer can recover the originating server.
"""

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import atexit
from pathlib import Path
from typing import Any

# requests is already a hard dependency (used by tools.py); import lazily so a
# stripped-down install without it can still use stdio servers.
try:
    import requests as _requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False


PROTOCOL_VERSION = "2025-06-18"
_CLIENT_INFO = {"name": "octoslave", "version": "0.2.0"}

# Separator between the `mcp` prefix, server name and tool name. Double
# underscore mirrors the convention used by hosted MCP integrations.
_SEP = "__"
_PREFIX = "mcp"


def _log(msg: str) -> None:
    """Best-effort info log that never raises (display may be unavailable)."""
    try:
        from . import display
        display.print_info(msg)
    except Exception:
        pass


def namespaced_name(server: str, tool: str) -> str:
    return f"{_PREFIX}{_SEP}{server}{_SEP}{tool}"


def is_mcp_name(name: str) -> bool:
    return isinstance(name, str) and name.startswith(_PREFIX + _SEP)


def sanitize_server_name(raw: str) -> str:
    """Make a config server name safe to embed in a tool name (model-facing)."""
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", (raw or "").strip()).strip("_")
    return cleaned or "server"


def _path_to_file_uri(path: str) -> str:
    """Absolute filesystem path → ``file://`` URI (what MCP roots expect)."""
    ap = os.path.abspath(os.path.expanduser(str(path)))
    try:
        return Path(ap).as_uri()
    except Exception:
        return "file://" + ap


def _norm_roots(paths) -> list[str]:
    """De-duplicated list of absolute paths from a roots spec."""
    out: list[str] = []
    for p in paths or []:
        if not p:
            continue
        ap = os.path.abspath(os.path.expanduser(str(p)))
        if ap not in out:
            out.append(ap)
    return out


# ---------------------------------------------------------------------------
# Transport-level errors
# ---------------------------------------------------------------------------

class MCPError(Exception):
    pass


# ---------------------------------------------------------------------------
# A single MCP server connection
# ---------------------------------------------------------------------------

class MCPServer:
    """One configured MCP server connection (stdio or http).

    Config shape (a dict from config.json ``mcp_servers``):
        {
          "name": "filesystem",          # required, unique
          "enabled": true,               # optional, default true
          # stdio transport:
          "command": "npx",              # presence of `command` ⇒ stdio
          "args": ["-y", "pkg", "/dir"],
          "env": {"FOO": "bar"},
          # http transport:
          "url": "https://host/mcp",     # presence of `url` ⇒ http
          "headers": {"Authorization": "Bearer …"}
        }
    """

    def __init__(self, config: dict):
        self.name: str = sanitize_server_name(config.get("name", ""))
        self.raw_name: str = config.get("name", self.name)
        self.config = config
        self.transport = "http" if config.get("url") else "stdio"

        self.tools: list[dict] = []           # OpenAI-format tool defs (namespaced)
        self._tool_index: dict[str, str] = {}  # namespaced → original tool name
        self.pruned: dict[str, str] = {}       # tool name → missing CLI binary
        self.connected = False
        self.error: str | None = None

        # stdio state
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._pending: dict[int, queue.Queue] = {}
        self._lock = threading.Lock()
        self._io_lock = threading.Lock()  # serialize stdin writes across threads
        self._id = 0

        # MCP roots — directories this client exposes to the server. Tracks
        # octoslave's active working dir so filesystem-style servers stay
        # sandboxed to the task the user is actually running.
        self._roots: list[str] = []
        self._roots_listed = threading.Event()  # set when we answer roots/list
        self._server_queries_roots = False       # server actually pulls roots?

        # http state
        self._session_id: str | None = None

    # -- public API --------------------------------------------------------

    def start(self, timeout: float = 30.0) -> bool:
        """Connect, perform the initialize handshake, and list tools.

        Returns True on success; on failure sets ``self.error`` and returns
        False (never raises — one bad server shouldn't break startup).
        """
        try:
            if self.transport == "stdio":
                self._start_stdio()
            else:
                self._start_http()
            self._initialize(timeout)
            self._load_tools(timeout)
            self.connected = True
            self.error = None
            return True
        except Exception as e:  # noqa: BLE001 — isolate per-server failures
            self.error = str(e)
            self.connected = False
            self._cleanup_process()
            return False

    def call_tool(self, original_tool: str, arguments: dict, timeout: float = 300.0) -> tuple[str, bool]:
        """Invoke a tool by its *original* (un-namespaced) name.

        Returns (text, success) matching octoslave's tool-result contract.
        """
        if not self.connected:
            return f"MCP server '{self.raw_name}' is not connected.", False
        try:
            result = self._request(
                "tools/call",
                {"name": original_tool, "arguments": arguments or {}},
                timeout=timeout,
            )
        except Exception as e:  # noqa: BLE001
            return f"MCP call failed ({self.raw_name}/{original_tool}): {e}", False
        return _render_tool_result(result)

    def original_tool_for(self, namespaced: str) -> str | None:
        return self._tool_index.get(namespaced)

    def seed_roots(self, paths) -> None:
        """Set initial roots *before* ``start()`` so the handshake advertises
        them and the server's first ``roots/list`` gets the right directory."""
        self._roots = _norm_roots(paths)

    def set_roots(self, paths, wait: bool = True, timeout: float = 2.0) -> bool:
        """Update the exposed roots on a live connection. No-op if unchanged.

        On a real change we send ``notifications/roots/list_changed``; if the
        server actually re-reads roots we block briefly until it has, so the
        next tool call sees the new sandbox rather than the old one.
        """
        new = _norm_roots(paths)
        with self._lock:
            if new == self._roots:
                return False
            self._roots = new
        if not (self.connected and self.transport == "stdio"):
            return True
        self._roots_listed.clear()
        try:
            self._notify("notifications/roots/list_changed", {})
        except Exception:
            return True
        if wait and self._server_queries_roots:
            self._roots_listed.wait(timeout)
        return True

    def stop(self) -> None:
        self.connected = False
        self._cleanup_process()

    # -- handshake / discovery --------------------------------------------

    def _initialize(self, timeout: float) -> None:
        self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                # Advertise the `roots` capability so servers can sandbox to the
                # directories we expose (and re-read them on list_changed).
                "capabilities": {"roots": {"listChanged": True}},
                "clientInfo": _CLIENT_INFO,
            },
            timeout=timeout,
        )
        # The spec requires an `initialized` notification after the handshake.
        self._notify("notifications/initialized", {})

    def _load_tools(self, timeout: float) -> None:
        result = self._request("tools/list", {}, timeout=timeout)
        tools = (result or {}).get("tools", []) or []
        # Prune tools that can only ever fail because the CLI they wrap isn't
        # installed (e.g. codag's tail_docker with no docker on PATH). Keyed by
        # the server's configured name against the curated registry; a no-op for
        # servers we don't recognise.
        try:
            from . import mcp_registry
            dead = mcp_registry.unsupported_tools(self.raw_name)
        except Exception:
            dead = {}
        self.pruned: dict[str, str] = {}
        defs: list[dict] = []
        index: dict[str, str] = {}
        for t in tools:
            orig = t.get("name")
            if not orig:
                continue
            if orig in dead:
                self.pruned[orig] = dead[orig]
                continue
            ns = namespaced_name(self.name, orig)
            schema = t.get("inputSchema") or {"type": "object", "properties": {}}
            # Some servers omit `type`; the OpenAI tool schema requires an object.
            if not isinstance(schema, dict) or "type" not in schema:
                schema = {"type": "object", "properties": schema.get("properties", {}) if isinstance(schema, dict) else {}}
            desc = t.get("description") or f"{orig} (via MCP server '{self.raw_name}')"
            defs.append({
                "type": "function",
                "function": {"name": ns, "description": desc, "parameters": schema},
            })
            index[ns] = orig
        self.tools = defs
        self._tool_index = index

    # -- JSON-RPC core -----------------------------------------------------

    def _next_id(self) -> int:
        with self._lock:
            self._id += 1
            return self._id

    def _request(self, method: str, params: dict, timeout: float) -> dict:
        if self.transport == "stdio":
            return self._request_stdio(method, params, timeout)
        return self._request_http(method, params, timeout)

    def _notify(self, method: str, params: dict) -> None:
        msg = {"jsonrpc": "2.0", "method": method, "params": params}
        if self.transport == "stdio":
            self._write_stdio(msg)
        else:
            # Notifications have no id; fire-and-forget POST.
            try:
                self._http_post(msg, expect_response=False)
            except Exception:
                pass

    # -- stdio transport ---------------------------------------------------

    def _start_stdio(self) -> None:
        command = self.config.get("command")
        if not command:
            raise MCPError("stdio MCP server requires a 'command'.")
        resolved = shutil.which(command) or command
        argv = [resolved, *(self.config.get("args") or [])]
        env = dict(os.environ)
        # Don't leak octoslave's venv into the child unless the user wants it.
        env.pop("VIRTUAL_ENV", None)
        env.update({k: str(v) for k, v in (self.config.get("env") or {}).items()})

        try:
            self._proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=env,
                text=True,
                bufsize=1,  # line-buffered
                cwd=self.config.get("cwd") or None,
            )
        except FileNotFoundError:
            raise MCPError(f"command not found: {command}")

        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue  # server log noise on stdout — ignore
            # Server→client request or notification (carries `method`); a
            # response to one of our calls never does.
            if msg.get("method") is not None:
                self._handle_inbound(msg)
                continue
            mid = msg.get("id")
            if mid is None:
                continue  # stray message — nothing to await
            with self._lock:
                q = self._pending.pop(mid, None)
            if q is not None:
                q.put(msg)
        # stdout closed — unblock any waiters.
        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for q in pending:
            q.put({"error": {"message": "MCP server closed the connection."}})

    def _write_stdio(self, msg: dict) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            raise MCPError("MCP server process is not running.")
        line = json.dumps(msg) + "\n"
        # Both request threads and the reader thread (servicing inbound
        # requests) write here — serialize so JSON lines never interleave.
        with self._io_lock:
            proc.stdin.write(line)
            proc.stdin.flush()

    def _handle_inbound(self, msg: dict) -> None:
        """Service a server→client request/notification.

        The only request we implement is ``roots/list`` (lets filesystem-style
        servers sandbox to the active working dir). Any other inbound *request*
        gets a method-not-found reply so the server isn't left waiting;
        notifications we don't recognise are ignored.
        """
        method = msg.get("method")
        mid = msg.get("id")
        if method == "roots/list":
            self._server_queries_roots = True
            with self._lock:
                roots = list(self._roots)
            result = {"roots": [
                {"uri": _path_to_file_uri(p),
                 "name": os.path.basename(p.rstrip("/")) or p}
                for p in roots
            ]}
            if mid is not None:
                try:
                    self._write_stdio({"jsonrpc": "2.0", "id": mid, "result": result})
                except Exception:
                    pass
            self._roots_listed.set()
            return
        if mid is not None:
            try:
                self._write_stdio({
                    "jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32601, "message": f"method not found: {method}"},
                })
            except Exception:
                pass

    def _request_stdio(self, method: str, params: dict, timeout: float) -> dict:
        mid = self._next_id()
        q: queue.Queue = queue.Queue(maxsize=1)
        with self._lock:
            self._pending[mid] = q
        self._write_stdio({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
        try:
            msg = q.get(timeout=timeout)
        except queue.Empty:
            with self._lock:
                self._pending.pop(mid, None)
            raise MCPError(f"timed out after {timeout:.0f}s waiting for '{method}'")
        if "error" in msg and msg["error"]:
            err = msg["error"]
            raise MCPError(err.get("message") if isinstance(err, dict) else str(err))
        return msg.get("result") or {}

    # -- http transport ----------------------------------------------------

    def _start_http(self) -> None:
        if not _HAS_REQUESTS:
            raise MCPError("http MCP transport requires the 'requests' package.")
        if not self.config.get("url"):
            raise MCPError("http MCP server requires a 'url'.")

    def _http_headers(self) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        headers.update(self.config.get("headers") or {})
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    def _http_post(self, msg: dict, expect_response: bool, timeout: float = 30.0):
        resp = _requests.post(
            self.config["url"],
            headers=self._http_headers(),
            json=msg,
            timeout=timeout,
            stream=True,
        )
        resp.raise_for_status()
        # Capture session id assigned by the server on initialize.
        sid = resp.headers.get("Mcp-Session-Id")
        if sid:
            self._session_id = sid
        if not expect_response:
            resp.close()
            return None
        ctype = resp.headers.get("Content-Type", "")
        if "text/event-stream" in ctype:
            return _parse_sse_for_id(resp, msg.get("id"))
        data = resp.json()
        resp.close()
        return data

    def _request_http(self, method: str, params: dict, timeout: float) -> dict:
        mid = self._next_id()
        msg = {"jsonrpc": "2.0", "id": mid, "method": method, "params": params}
        data = self._http_post(msg, expect_response=True, timeout=timeout)
        if data is None:
            raise MCPError(f"no response for '{method}'")
        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            raise MCPError(err.get("message") if isinstance(err, dict) else str(err))
        return (data or {}).get("result") or {}

    # -- cleanup -----------------------------------------------------------

    def _cleanup_process(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.stdin:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception:
            pass
        finally:
            self._proc = None


# ---------------------------------------------------------------------------
# Result rendering
# ---------------------------------------------------------------------------

def _render_tool_result(result: dict) -> tuple[str, bool]:
    """Turn an MCP tools/call result into (text, success)."""
    if not isinstance(result, dict):
        return str(result), True
    is_error = bool(result.get("isError"))
    content = result.get("content")
    parts: list[str] = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                parts.append(str(block))
                continue
            btype = block.get("type")
            if btype == "text":
                parts.append(block.get("text", ""))
            elif btype == "resource":
                res = block.get("resource") or {}
                if res.get("text"):
                    parts.append(str(res["text"]))
                else:
                    parts.append(f"[resource: {res.get('uri', 'unknown')}]")
            elif btype in ("image", "audio"):
                parts.append(f"[{btype} content: {block.get('mimeType', 'binary')} — not rendered]")
            else:
                parts.append(json.dumps(block, ensure_ascii=False))
    # Structured content (newer servers) — surface as JSON if no text content.
    if not parts and result.get("structuredContent") is not None:
        parts.append(json.dumps(result["structuredContent"], ensure_ascii=False, indent=2))
    text = "\n".join(p for p in parts if p) or "(no content)"
    return text, not is_error


def _parse_sse_for_id(resp, want_id) -> dict | None:
    """Read a text/event-stream response and return the JSON message whose
    ``id`` matches ``want_id`` (or the first message carrying a result/error)."""
    fallback = None
    try:
        for raw in resp.iter_lines(decode_unicode=True):
            if not raw or not raw.startswith("data:"):
                continue
            payload = raw[len("data:"):].strip()
            if not payload:
                continue
            try:
                msg = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == want_id:
                return msg
            if fallback is None and ("result" in msg or "error" in msg):
                fallback = msg
    finally:
        resp.close()
    return fallback


# ---------------------------------------------------------------------------
# Manager — the singleton the rest of octoslave talks to
# ---------------------------------------------------------------------------

class MCPManager:
    def __init__(self):
        self.servers: dict[str, MCPServer] = {}  # sanitized name → server
        self._initialized = False
        self._lock = threading.Lock()
        self._roots: list[str] = []  # directories exposed to every server

    def init_from_config(self, cfg: dict | None = None, force: bool = False,
                         roots=None) -> None:
        """Connect to all enabled MCP servers in config. Idempotent unless
        ``force`` is passed (used by ``/mcp reconnect``).

        ``roots`` seeds the directories exposed to each server (typically the
        active working dir) so the handshake advertises them from the start.
        """
        with self._lock:
            if roots is not None:
                self._roots = _norm_roots(roots)
            if self._initialized and not force:
                return
            if force:
                self._shutdown_locked()
            self._initialized = True

            if cfg is None:
                from .config import load_config
                cfg = load_config()
            entries = cfg.get("mcp_servers") or []
            enabled = [e for e in entries if isinstance(e, dict) and e.get("enabled", True) and e.get("name")]
            if not enabled:
                return

            for entry in enabled:
                server = MCPServer(entry)
                if server.name in self.servers:
                    continue  # duplicate name — keep the first
                server.seed_roots(self._roots)
                ok = server.start()
                self.servers[server.name] = server
                if ok:
                    msg = f"MCP: connected '{server.raw_name}' ({len(server.tools)} tool(s))."
                    if server.pruned:
                        dropped = ", ".join(f"{t} (no {b})" for t, b in server.pruned.items())
                        msg += f" Pruned {len(server.pruned)} unavailable: {dropped}."
                    _log(msg)
                else:
                    _log(f"MCP: failed to connect '{server.raw_name}': {server.error}")

    def set_roots(self, paths) -> None:
        """Point every connected server at ``paths`` (octoslave's active
        working dir). Cheap + idempotent — a no-op when nothing changed."""
        norm = _norm_roots(paths)
        with self._lock:
            if norm == self._roots:
                return
            self._roots = norm
            servers = list(self.servers.values())
        for server in servers:
            try:
                server.set_roots(norm)
            except Exception:
                pass

    def tool_definitions(self) -> list[dict]:
        defs: list[dict] = []
        for server in self.servers.values():
            if server.connected:
                defs.extend(server.tools)
        return defs

    def tool_names(self) -> set[str]:
        names: set[str] = set()
        for server in self.servers.values():
            if server.connected:
                names.update(server._tool_index.keys())
        return names

    def is_mcp_tool(self, name: str) -> bool:
        return name in self.tool_names()

    def call(self, namespaced: str, args: dict, timeout: float = 300.0) -> tuple[str, bool]:
        for server in self.servers.values():
            orig = server.original_tool_for(namespaced)
            if orig is not None:
                return server.call_tool(orig, args, timeout=timeout)
        return f"Unknown MCP tool: {namespaced}", False

    def status(self) -> list[dict]:
        """Snapshot for the /mcp command and web UI."""
        out = []
        for server in self.servers.values():
            out.append({
                "name": server.raw_name,
                "transport": server.transport,
                "connected": server.connected,
                "error": server.error,
                "tools": [server._tool_index[ns] for ns in server._tool_index],
                "tool_count": len(server.tools),
                "pruned": dict(server.pruned),
            })
        return out

    def shutdown(self) -> None:
        with self._lock:
            self._shutdown_locked()
            self._initialized = False

    def _shutdown_locked(self) -> None:
        for server in self.servers.values():
            server.stop()
        self.servers.clear()


# Process-wide singleton.
manager = MCPManager()

# Make sure subprocesses are reaped on interpreter exit.
atexit.register(manager.shutdown)
