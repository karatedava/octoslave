"""System-ssh-backed remote execution for OctoSlave.

Wraps the OS ``ssh`` / ``scp`` binaries so the agent's filesystem/bash tools can
run on a remote host over SSH. Uses OpenSSH **ControlMaster multiplexing**: the
first call opens one shared master connection (keyed by user@host:port) under a
private control dir, and every later call reuses it — avoiding a full TCP + auth
handshake per tool call. No third-party dependency; honors ``~/.ssh/config``,
keys, ssh-agent and ``ProxyJump``.

Auth is key/agent based (``BatchMode=yes``) so a mis-configured host fails fast
instead of hanging the agent on a password prompt.
"""

from __future__ import annotations

import atexit
import os
import posixpath
import shlex
import subprocess
import tempfile
import threading


class RemoteError(RuntimeError):
    """A remote ssh/scp operation failed."""


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class RemoteConfig:
    """Normalized connection settings for one remote host."""

    def __init__(self, id: str, host: str, remote_dir: str, user: str | None = None,
                 port: int = 22, name: str | None = None, identity_file: str | None = None):
        self.id = id
        self.host = host
        self.remote_dir = remote_dir or "."
        self.user = user or ""
        self.port = int(port or 22)
        self.name = name or id or host
        self.identity_file = identity_file or ""

    @classmethod
    def from_dict(cls, d: dict) -> "RemoteConfig":
        return cls(
            id=d.get("id", ""),
            host=d.get("host", ""),
            remote_dir=d.get("remote_dir", "."),
            user=d.get("user", ""),
            port=d.get("port", 22),
            name=d.get("name", ""),
            identity_file=d.get("identity_file", ""),
        )

    @property
    def target(self) -> str:
        """``user@host`` (or just ``host`` when no user is configured)."""
        return f"{self.user}@{self.host}" if self.user else self.host

    @property
    def label(self) -> str:
        return f"{self.name} ({self.target}:{self.remote_dir})"

    @property
    def _key(self) -> str:
        return f"{self.target}|{self.port}|{self.identity_file}"


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

class RemoteSession:
    """A multiplexed ssh/scp connection to one host, shared across threads."""

    _sessions: dict[str, "RemoteSession"] = {}
    _sessions_lock = threading.Lock()

    def __init__(self, cfg: RemoteConfig):
        self.cfg = cfg
        # OpenSSH caps the ControlPath socket at ~104 chars, so keep the base
        # short: prefer /tmp (macOS $TMPDIR is a long /var/folders/… path).
        base = "/tmp" if os.path.isdir("/tmp") and os.access("/tmp", os.W_OK) else None
        self._control_dir = tempfile.mkdtemp(prefix="ots_ssh_", dir=base)
        # %C hashes host/port/user → one socket per distinct connection.
        self._control_path = os.path.join(self._control_dir, "cm-%C")

    # -- construction / caching -------------------------------------------

    @classmethod
    def get(cls, cfg) -> "RemoteSession":
        """Return a cached session for this host (opening one if needed)."""
        if isinstance(cfg, dict):
            cfg = RemoteConfig.from_dict(cfg)
        with cls._sessions_lock:
            sess = cls._sessions.get(cfg._key)
            if sess is None:
                sess = cls(cfg)
                cls._sessions[cfg._key] = sess
            else:
                # Refresh dir/name in case the config was edited.
                sess.cfg = cfg
            return sess

    # -- ssh/scp option builders ------------------------------------------

    def _mux_opts(self) -> list[str]:
        return [
            "-o", "ControlMaster=auto",
            "-o", f"ControlPath={self._control_path}",
            "-o", "ControlPersist=300",
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=15",
        ]

    def _ssh_opts(self) -> list[str]:
        opts = list(self._mux_opts())
        if self.cfg.port and self.cfg.port != 22:
            opts += ["-p", str(self.cfg.port)]
        if self.cfg.identity_file:
            opts += ["-i", os.path.expanduser(self.cfg.identity_file)]
        return opts

    def _scp_opts(self) -> list[str]:
        # scp uses -P (capital) for the port and reuses the same master socket.
        opts = list(self._mux_opts())
        if self.cfg.port and self.cfg.port != 22:
            opts += ["-P", str(self.cfg.port)]
        if self.cfg.identity_file:
            opts += ["-i", os.path.expanduser(self.cfg.identity_file)]
        return opts

    # -- low-level runners -------------------------------------------------

    def _ssh(self, remote_cmd: str, timeout: int | None = 60,
             capture_bytes: bool = False) -> subprocess.CompletedProcess:
        cmd = ["ssh", *self._ssh_opts(), self.cfg.target, remote_cmd]
        return subprocess.run(
            cmd,
            capture_output=True,
            text=not capture_bytes,
            timeout=timeout,
        )

    # -- connectivity ------------------------------------------------------

    def check(self) -> tuple[bool, str]:
        """Probe reachability. Returns (ok, human message)."""
        try:
            r = self._ssh("true", timeout=25)
        except subprocess.TimeoutExpired:
            return False, "connection timed out (check host/port/network)"
        except FileNotFoundError:
            return False, "the `ssh` binary was not found on this machine"
        except Exception as e:  # pragma: no cover - defensive
            return False, str(e)
        if r.returncode == 0:
            return True, f"connected to {self.cfg.label}"
        msg = (r.stderr or "").strip() or "ssh returned a non-zero exit code"
        return False, msg

    # -- command execution -------------------------------------------------

    def run(self, command: str, cwd: str | None, timeout: int = 300) -> tuple[str, str, int]:
        """Run ``command`` on the remote host inside ``cwd``.

        Returns (stdout, stderr, returncode). A timeout yields rc 124.
        """
        cd = f"cd {shlex.quote(cwd)} && " if cwd else ""
        wrapped = cd + command
        try:
            r = self._ssh(wrapped, timeout=timeout)
        except subprocess.TimeoutExpired:
            return "", f"(remote command timed out after {timeout}s)", 124
        except FileNotFoundError:
            return "", "the `ssh` binary was not found on this machine", 127
        return (r.stdout or ""), (r.stderr or ""), r.returncode

    # -- filesystem probes -------------------------------------------------

    def exists(self, path: str) -> bool:
        return self.run(f"test -e {shlex.quote(path)}", None, timeout=30)[2] == 0

    def is_dir(self, path: str) -> bool:
        return self.run(f"test -d {shlex.quote(path)}", None, timeout=30)[2] == 0

    def is_file(self, path: str) -> bool:
        return self.run(f"test -f {shlex.quote(path)}", None, timeout=30)[2] == 0

    def size(self, path: str) -> int:
        """Byte size of a remote file (portable across GNU/BSD stat)."""
        q = shlex.quote(path)
        out, _, rc = self.run(f"stat -c %s {q} 2>/dev/null || stat -f %z {q}", None, timeout=30)
        try:
            return int(out.strip())
        except (ValueError, TypeError):
            return -1

    def mkdirs(self, path: str) -> None:
        self.run(f"mkdir -p {shlex.quote(path)}", None, timeout=60)

    def home(self) -> str:
        """Absolute path of the remote login/home directory (fallback '.')."""
        out, _, rc = self.run("pwd", None, timeout=30)
        return out.strip() if rc == 0 and out.strip() else "."

    def list_dirs(self, path: str) -> tuple[bool, str, list[str], str]:
        """List sub-directories of ``path`` (or the remote home when empty).

        Returns (ok, absolute_path, sorted_dir_names, error). Hidden dirs are
        included; the resolved absolute path is taken from the remote ``pwd`` so
        the caller can navigate with ``..`` and always get a real path back.
        """
        cd = f"cd {shlex.quote(path)}" if path else "cd ~"
        out, err, rc = self.run(f"{cd} && pwd && ls -1pA", None, timeout=30)
        if rc != 0:
            return False, path, [], (err or "cannot list directory").strip()
        lines = out.splitlines()
        cwd = lines[0] if lines else (path or ".")
        dirs = sorted(e[:-1] for e in lines[1:] if e.endswith("/"))
        return True, cwd, dirs, ""

    # -- file transfer -----------------------------------------------------

    def pull(self, remote_path: str, suffix: str = "", dest: str | None = None) -> str:
        """Copy a remote file to a local path (a temp file unless ``dest`` given)."""
        if dest:
            local = dest
        else:
            fd, local = tempfile.mkstemp(prefix="ots_pull_", suffix=suffix)
            os.close(fd)
        cmd = ["scp", *self._scp_opts(), f"{self.cfg.target}:{remote_path}", local]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            _silent_unlink(local)
            raise RemoteError(f"scp of {remote_path} timed out")
        if r.returncode != 0:
            _silent_unlink(local)
            raise RemoteError((r.stderr or "scp failed").strip())
        return local

    def push(self, local_path: str, remote_path: str) -> None:
        """Copy a local file to the remote host (creating parent dirs)."""
        self.mkdirs(posixpath.dirname(remote_path) or ".")
        cmd = ["scp", *self._scp_opts(), local_path, f"{self.cfg.target}:{remote_path}"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            raise RemoteError(f"scp to {remote_path} timed out")
        if r.returncode != 0:
            raise RemoteError((r.stderr or "scp failed").strip())

    def read_text(self, path: str) -> str:
        local = self.pull(path)
        try:
            with open(local, "r", errors="replace") as fh:
                return fh.read()
        finally:
            _silent_unlink(local)

    def write_text(self, path: str, content: str) -> None:
        fd, local = tempfile.mkstemp(prefix="ots_push_")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(content)
            self.push(local, path)
        finally:
            _silent_unlink(local)

    # -- teardown ----------------------------------------------------------

    def close(self) -> None:
        try:
            cmd = ["ssh", *self._ssh_opts(), "-O", "exit", self.cfg.target]
            subprocess.run(cmd, capture_output=True, timeout=10)
        except Exception:
            pass
        try:
            import shutil
            shutil.rmtree(self._control_dir, ignore_errors=True)
        except Exception:
            pass


def resolve_start_dir(remote: dict) -> str:
    """Working directory to start in when switching to a remote.

    Uses the (optional) configured ``remote_dir``; otherwise the remote home
    directory, resolved over SSH. Falls back to '.' if the host is unreachable.
    """
    rd = (remote or {}).get("remote_dir")
    if rd:
        return rd
    try:
        return RemoteSession.get(remote).home()
    except Exception:
        return "."


def _silent_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _shutdown_sessions() -> None:
    for sess in list(RemoteSession._sessions.values()):
        sess.close()


atexit.register(_shutdown_sessions)
