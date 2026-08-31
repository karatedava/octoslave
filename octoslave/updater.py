"""In-place self-update for OctoSlave.

Users install OctoSlave in six different ways (pip, pipx, a plain venv, the
Homebrew tap, one of the three frozen installers, or an editable git clone).
Before this module every one of them had the same upgrade path: go to the
Releases page, download the installer again, and run it. This module removes
that step — it works out *how* this particular copy got onto the machine, asks
GitHub what the newest release is, and knows the right way to replace itself.

Three moving parts:

``detect_install_method()``
    Returns one of the ``M_*`` constants below. Everything else keys off it.

``check(force=False)``
    Compares ``octoslave.__version__`` against the latest GitHub release.
    Result is cached in ``~/.octoslave/update.json`` for ``CHECK_TTL`` seconds
    so opening the web UI ten times doesn't mean ten API calls (GitHub allows
    60/hour unauthenticated, shared across everyone behind one NAT — an HPC
    login node burns that fast).

``start_update()`` / ``status()``
    Runs the platform-appropriate upgrade on a background thread and exposes a
    progress log the web UI polls. Python installs are upgraded in place and
    ask for a restart; the frozen installers stage the new bundle, then hand
    off to a detached helper that swaps it in once this process has exited —
    a running .app/.exe cannot overwrite itself.

Opt out entirely with ``OCTOSLAVE_NO_UPDATE_CHECK=1`` (or the toggle the web
UI writes into the state file). Nothing here ever runs on import.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import __version__

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_SLUG    = os.environ.get("OCTOSLAVE_UPDATE_REPO", "karatedava/octoslave")
REPO_URL     = f"https://github.com/{REPO_SLUG}"
RELEASES_API = f"https://api.github.com/repos/{REPO_SLUG}/releases/latest"
RELEASES_WEB = f"{REPO_URL}/releases/latest"

# Release assets carry stable, version-less names (the CI `release` job renames
# them), so "latest" download URLs stay valid forever.
ASSET_MACOS   = "OctoSlave-macOS.dmg"
ASSET_WINDOWS = "OctoSlave-Windows-Installer.exe"
ASSET_LINUX   = "OctoSlave-x86_64.AppImage"

STATE_FILE = Path.home() / ".octoslave" / "update.json"
CHECK_TTL  = 6 * 3600          # re-ask GitHub at most every 6 h
HTTP_TIMEOUT = 10

# Install methods
M_PIPX      = "pipx"
M_PIP       = "pip"
M_VENV      = "venv"
M_BREW      = "brew"
M_APPIMAGE  = "appimage"
M_MACOS_APP = "macos_app"
M_WINDOWS   = "windows"
M_SOURCE    = "source"          # editable / git clone — developer install
M_UNKNOWN   = "unknown"

_METHOD_INFO: dict[str, dict[str, Any]] = {
    M_PIPX:      {"label": "pipx",                "self_update": True,  "quits_app": False},
    M_PIP:       {"label": "pip",                 "self_update": True,  "quits_app": False},
    M_VENV:      {"label": "virtualenv (pip)",    "self_update": True,  "quits_app": False},
    M_BREW:      {"label": "Homebrew",            "self_update": True,  "quits_app": False},
    M_APPIMAGE:  {"label": "Linux AppImage",      "self_update": True,  "quits_app": False},
    M_MACOS_APP: {"label": "macOS app",           "self_update": True,  "quits_app": True},
    M_WINDOWS:   {"label": "Windows installer",   "self_update": True,  "quits_app": True},
    M_SOURCE:    {"label": "source checkout",     "self_update": False, "quits_app": False},
    M_UNKNOWN:   {"label": "unknown install",     "self_update": False, "quits_app": False},
}

_MANUAL_HINT = {
    M_SOURCE:  "Developer install — update with:  git pull && pip install -e '.[all]'",
    M_UNKNOWN: f"Could not tell how OctoSlave was installed. Grab the latest build from {RELEASES_WEB}",
}


# ---------------------------------------------------------------------------
# Version comparison
# ---------------------------------------------------------------------------

def parse_version(v: str) -> tuple:
    """Loose semver → comparable tuple.

    ``3.5.0`` → ``(3, 5, 0, 1, '')`` and ``3.6.0rc1`` → ``(3, 6, 0, 0, 'rc1')``,
    so a release always sorts above its own pre-releases. Anything unparseable
    (``0.0.0+unknown`` from a build with no metadata) sorts lowest.
    """
    v = (v or "").strip().lstrip("vV")
    m = re.match(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?(.*)$", v)
    if not m:
        return (0, 0, 0, 0, "")
    major, minor, patch, rest = m.groups()
    rest = (rest or "").strip()
    # "+local" build metadata is ignored for ordering, per semver.
    rest = rest.split("+", 1)[0].lstrip(".-")
    return (int(major), int(minor or 0), int(patch or 0), 0 if rest else 1, rest)


def is_newer(candidate: str, current: str) -> bool:
    return parse_version(candidate) > parse_version(current)


# ---------------------------------------------------------------------------
# Install-method detection
# ---------------------------------------------------------------------------

def _dist_direct_url() -> dict:
    """direct_url.json from the installed dist — tells us about editable installs."""
    try:
        from importlib.metadata import distribution
        raw = distribution("octoslave").read_text("direct_url.json")
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def _is_editable() -> bool:
    info = _dist_direct_url()
    if info.get("dir_info", {}).get("editable"):
        return True
    # Fallback: the package sits next to a pyproject.toml declaring itself.
    try:
        pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
        return pyproject.is_file() and 'name = "octoslave"' in pyproject.read_text()
    except Exception:
        return False


def _brew_prefix() -> str | None:
    for candidate in ("/opt/homebrew", "/usr/local/Homebrew", "/home/linuxbrew/.linuxbrew"):
        if Path(candidate).is_dir():
            return candidate
    return None


def detect_install_method() -> str:
    """How did this copy of OctoSlave get here? See the ``M_*`` constants."""
    override = os.environ.get("OCTOSLAVE_INSTALL_METHOD", "").strip()
    if override in _METHOD_INFO:
        return override

    if getattr(sys, "frozen", False):
        if os.environ.get("APPIMAGE"):
            return M_APPIMAGE
        if sys.platform == "win32":
            return M_WINDOWS
        if sys.platform == "darwin" and ".app/Contents/" in sys.executable:
            return M_MACOS_APP
        # A frozen binary that is neither — the Linux one-file build run
        # outside its AppImage wrapper. No safe in-place path.
        return M_UNKNOWN

    exe = str(Path(sys.executable).resolve())
    low = exe.lower().replace("\\", "/")

    if "/pipx/venvs/" in low or "/pipx/shared/" in low:
        return M_PIPX
    prefix = _brew_prefix()
    if prefix and (low.startswith(prefix.lower() + "/cellar/")
                   or "/cellar/octoslave/" in low):
        return M_BREW
    if _is_editable():
        return M_SOURCE
    # sys.prefix != sys.base_prefix means we're inside a venv — `pip install -U`
    # there touches nothing outside it, so it's always safe.
    if sys.prefix != getattr(sys, "base_prefix", sys.prefix):
        return M_VENV
    return M_PIP


def method_info(method: str | None = None) -> dict:
    method = method or detect_install_method()
    info = dict(_METHOD_INFO.get(method, _METHOD_INFO[M_UNKNOWN]))
    info["method"] = method
    if method in _MANUAL_HINT:
        info["hint"] = _MANUAL_HINT[method]
    return info


# ---------------------------------------------------------------------------
# Persisted state (cache + user preferences)
# ---------------------------------------------------------------------------

def _read_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_state(data: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass


def checks_enabled() -> bool:
    """False when the user (or the environment) opted out of update checks."""
    if os.environ.get("OCTOSLAVE_NO_UPDATE_CHECK", "").strip() not in ("", "0", "false", "no"):
        return False
    return bool(_read_state().get("check_enabled", True))


def set_checks_enabled(enabled: bool) -> None:
    state = _read_state()
    state["check_enabled"] = bool(enabled)
    _write_state(state)


def skip_version(version: str) -> None:
    """Stop nagging about one specific release."""
    state = _read_state()
    state["skipped_version"] = version
    _write_state(state)


# ---------------------------------------------------------------------------
# Release check
# ---------------------------------------------------------------------------

def _fetch_latest_release() -> dict:
    import requests
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"octoslave/{__version__}",
    }
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = requests.get(RELEASES_API, headers=headers, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


def check(force: bool = False) -> dict:
    """Is there a newer release? Cached for ``CHECK_TTL`` unless ``force``.

    Never raises: on a network failure the caller gets ``available: False``
    plus an ``error`` string, because a failed update check must never be
    something the user has to deal with.
    """
    state = _read_state()
    info = method_info()
    result: dict[str, Any] = {
        "current":      __version__,
        "latest":       None,
        "available":    False,
        "method":       info["method"],
        "method_label": info["label"],
        "self_update":  info["self_update"],
        "quits_app":    info["quits_app"],
        "hint":         info.get("hint"),
        "notes":        "",
        "url":          RELEASES_WEB,
        "checked_at":   None,
        "skipped":      False,
        "enabled":      checks_enabled(),
        "error":        None,
    }

    if not force and not result["enabled"]:
        return result

    cached = state.get("last_check") or {}
    fresh = (
        not force
        and cached.get("latest")
        and (time.time() - float(cached.get("at") or 0)) < CHECK_TTL
    )
    if fresh:
        latest, notes, checked_at = cached["latest"], cached.get("notes", ""), cached.get("at")
    else:
        try:
            data = _fetch_latest_release()
            latest = str(data.get("tag_name") or "").lstrip("vV")
            notes = str(data.get("body") or "")
            checked_at = time.time()
            state["last_check"] = {"latest": latest, "notes": notes, "at": checked_at}
            _write_state(state)
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
            # Fall back to a stale cache rather than showing nothing.
            if cached.get("latest"):
                latest, notes, checked_at = cached["latest"], cached.get("notes", ""), cached.get("at")
            else:
                return result

    result["latest"] = latest
    result["notes"] = notes
    result["checked_at"] = checked_at
    result["available"] = bool(latest) and is_newer(latest, __version__)
    result["skipped"] = bool(latest) and state.get("skipped_version") == latest
    if latest:
        result["url"] = f"{REPO_URL}/releases/tag/v{latest}"
    return result


# ---------------------------------------------------------------------------
# Update job — one at a time, progress polled by the web UI
# ---------------------------------------------------------------------------

class _Job:
    """State of the running (or last) update. Deliberately a singleton: two
    concurrent upgrades of the same install would corrupt it."""

    def __init__(self) -> None:
        # Reentrant: start_update() mutates state and then calls snapshot() to
        # return it, both under the lock.
        self.lock = threading.RLock()
        self.state = "idle"       # idle | running | done | error
        self.stage = ""
        self.pct: float | None = None
        self.log: list[str] = []
        self.error: str | None = None
        self.target: str | None = None
        self.needs_restart = False
        self.will_quit = False
        self.thread: threading.Thread | None = None

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "state": self.state, "stage": self.stage, "pct": self.pct,
                "log": list(self.log), "error": self.error, "target": self.target,
                "needs_restart": self.needs_restart, "will_quit": self.will_quit,
            }

    def emit(self, line: str, pct: float | None = None, stage: str | None = None) -> None:
        with self.lock:
            self.log.append(line)
            del self.log[:-400]
            if pct is not None:
                self.pct = pct
            if stage:
                self.stage = stage


_JOB = _Job()


def status() -> dict:
    return _JOB.snapshot()


def start_update(version: str | None = None) -> dict:
    """Kick off the update on a background thread. Returns the initial status."""
    with _JOB.lock:
        if _JOB.state == "running":
            return _JOB.snapshot()
        info = method_info()
        if not info["self_update"]:
            _JOB.state = "error"
            _JOB.error = info.get("hint") or "This install cannot update itself."
            return _JOB.snapshot()
        _JOB.state = "running"
        _JOB.stage = "starting"
        _JOB.pct = 0.0
        _JOB.log = []
        _JOB.error = None
        _JOB.target = version
        _JOB.needs_restart = False
        _JOB.will_quit = info["quits_app"]
        _JOB.thread = threading.Thread(target=_run_update, args=(version,), daemon=True)
        _JOB.thread.start()
    return _JOB.snapshot()


def _run_update(version: str | None) -> None:
    method = detect_install_method()
    try:
        if version is None:
            info = check(force=True)
            version = info.get("latest")
            if not version:
                raise RuntimeError("Could not determine the latest release version.")
        with _JOB.lock:
            _JOB.target = version
        _JOB.emit(f"Updating OctoSlave {__version__} → {version}  ({method_info(method)['label']})",
                  stage="starting")

        handler: Callable[[str], bool] = {
            M_PIPX:      _update_pipx,
            M_PIP:       _update_pip,
            M_VENV:      _update_pip,
            M_BREW:      _update_brew,
            M_APPIMAGE:  _update_appimage,
            M_MACOS_APP: _update_macos_app,
            M_WINDOWS:   _update_windows,
        }[method]

        will_quit = handler(version)
        with _JOB.lock:
            _JOB.state = "done"
            _JOB.pct = 100.0
            _JOB.stage = "done"
            _JOB.will_quit = will_quit
            _JOB.needs_restart = True
        _JOB.emit("Update complete.")
    except Exception as exc:
        with _JOB.lock:
            _JOB.state = "error"
            _JOB.error = f"{type(exc).__name__}: {exc}"
            _JOB.stage = "failed"
        _JOB.emit(f"Update failed: {exc}")


# ── shared helpers ────────────────────────────────────────────────────────

def _run(cmd: list[str], stage: str) -> int:
    """Run a command, streaming its output into the job log."""
    _JOB.emit(f"$ {' '.join(cmd)}", stage=stage)
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            _JOB.emit(line)
    return proc.wait()


def _detect_extras() -> str:
    """Preserve the extras the user installed with.

    There is no record of the original ``pip install`` spec, so probe for the
    heaviest sentinel of each extra. Guessing low would silently *remove*
    working tools on upgrade, so ``[all]`` wins whenever its markers are there.
    """
    from importlib.util import find_spec

    def has(mod: str) -> bool:
        try:
            return find_spec(mod) is not None
        except (ImportError, ValueError):
            return False

    if has("rdkit") and has("cryosparc_tools"):
        return "[all]"
    if has("rdkit"):
        return "[bio]"
    if has("fastapi"):
        return "[web]"
    return ""


def _pip_specs(version: str) -> list[str]:
    """Install specs to try in order — PyPI first, git tag as the fallback.

    Mirrors ``scripts/install.sh``: PyPI is faster and needs no git, but the
    GitHub release is the source of truth for what "latest" means, so a tag
    that has not been published to PyPI yet still installs.
    """
    extras = _detect_extras()
    return [
        f"octoslave{extras}=={version}",
        f"git+{REPO_URL}@v{version}#egg=octoslave{extras}",
    ]


def _download(url: str, dest: Path, stage: str) -> Path:
    """Stream a release asset to ``dest``, reporting percentage as it goes."""
    import requests
    _JOB.emit(f"Downloading {url}", pct=0.0, stage=stage)
    with requests.get(url, stream=True, timeout=60,
                      headers={"User-Agent": f"octoslave/{__version__}"}) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length") or 0)
        done = 0
        last_report = 0.0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 18):
                if not chunk:
                    continue
                f.write(chunk)
                done += len(chunk)
                if total:
                    pct = done * 90.0 / total     # leave the last 10% for install
                    if pct - last_report >= 5:
                        last_report = pct
                        _JOB.emit(f"  {done >> 20} / {total >> 20} MB", pct=pct)
    _JOB.emit(f"Downloaded {done >> 20} MB", pct=90.0)
    return dest


def _staging_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="octoslave-update-"))


# ── per-method update strategies ──────────────────────────────────────────

def _update_pip(version: str) -> bool:
    last = 1
    for spec in _pip_specs(version):
        last = _run([sys.executable, "-m", "pip", "install", "--upgrade", spec], "installing")
        if last == 0:
            _JOB.emit("Installed. Restart OctoSlave to run the new version.")
            return False
        _JOB.emit(f"Spec failed ({spec}) — trying the next source.")
    raise RuntimeError(f"pip install failed (exit {last}). See the log above.")


def _update_pipx(version: str) -> bool:
    pipx = shutil.which("pipx")
    base = [pipx] if pipx else [sys.executable, "-m", "pipx"]
    last = 1
    for spec in _pip_specs(version):
        # `pipx upgrade` only follows the original source; --force reinstalls
        # from an explicit spec, which is what pins us to the release tag.
        last = _run([*base, "install", "--force", spec], "installing")
        if last == 0:
            _JOB.emit("Installed. Restart OctoSlave to run the new version.")
            return False
        _JOB.emit(f"Spec failed ({spec}) — trying the next source.")
    raise RuntimeError(f"pipx install failed (exit {last}). See the log above.")


def _update_brew(version: str) -> bool:
    brew = shutil.which("brew")
    if not brew:
        raise RuntimeError("brew not found on PATH.")
    _run([brew, "update"], "installing")
    if _run([brew, "upgrade", "octoslave"], "installing") != 0:
        raise RuntimeError("brew upgrade octoslave failed. See the log above.")
    _JOB.emit("Upgraded. Restart OctoSlave to run the new version.")
    return False


def _update_appimage(version: str) -> bool:
    """Replace the .AppImage file in place.

    The running process keeps the old inode mounted, so an atomic rename over
    the path is safe — the swap only takes effect on the next launch.
    """
    target = Path(os.environ.get("APPIMAGE") or "").resolve()
    if not target.is_file():
        raise RuntimeError("APPIMAGE env var is not set — cannot locate the running AppImage.")
    if not os.access(target.parent, os.W_OK):
        raise RuntimeError(f"No write permission for {target.parent}. "
                           f"Move the AppImage somewhere writable, or update manually.")

    tmp = target.with_name(target.name + f".new-{version}")
    try:
        _download(f"{REPO_URL}/releases/download/v{version}/{ASSET_LINUX}", tmp, "downloading")
        os.chmod(tmp, 0o755)
        backup = target.with_name(target.name + ".old")
        backup.unlink(missing_ok=True)
        # Keep the previous build next to the new one: an AppImage that fails to
        # start leaves the user with no working install otherwise.
        shutil.copy2(target, backup)
        os.replace(tmp, target)
        _JOB.emit(f"Replaced {target} (previous build kept as {backup.name}).", pct=100.0)
    finally:
        tmp.unlink(missing_ok=True)
    _JOB.emit("Restart OctoSlave to run the new version.")
    return False


def _macos_app_path() -> Path:
    """/Applications/OctoSlave.app from …/OctoSlave.app/Contents/MacOS/OctoSlave."""
    exe = Path(sys.executable).resolve()
    for parent in exe.parents:
        if parent.suffix == ".app":
            return parent
    raise RuntimeError("Could not locate the running .app bundle.")


def _update_macos_app(version: str) -> bool:
    """Stage the new .app from the DMG, then let a detached helper swap it in.

    A bundle cannot replace itself while its own binary is executing, so the
    swap has to outlive this process: the helper waits for our PID to go away,
    moves the new bundle into place and relaunches it.
    """
    target = _macos_app_path()
    if not os.access(target.parent, os.W_OK):
        raise RuntimeError(f"No write permission for {target.parent}. "
                           f"Move OctoSlave.app to ~/Applications, or update manually.")

    work = _staging_dir()
    dmg = work / ASSET_MACOS
    _download(f"{REPO_URL}/releases/download/v{version}/{ASSET_MACOS}", dmg, "downloading")

    _JOB.emit("Mounting disk image…", pct=92.0, stage="installing")
    mount = work / "mnt"
    mount.mkdir()
    if _run(["hdiutil", "attach", str(dmg), "-nobrowse", "-readonly",
             "-mountpoint", str(mount)], "installing") != 0:
        raise RuntimeError("Could not mount the downloaded disk image.")
    try:
        src = next((p for p in mount.iterdir() if p.suffix == ".app"), None)
        if src is None:
            raise RuntimeError("No .app found inside the disk image.")
        staged = work / src.name
        _JOB.emit(f"Staging {src.name}…", pct=96.0)
        if _run(["/usr/bin/ditto", str(src), str(staged)], "installing") != 0:
            raise RuntimeError("Failed to copy the new app out of the disk image.")
    finally:
        subprocess.run(["hdiutil", "detach", str(mount), "-quiet"], check=False)

    helper = work / "swap.sh"
    helper.write_text(
        "#!/bin/bash\n"
        "# Wait for the old OctoSlave to exit, then swap the bundle and relaunch.\n"
        f"for _ in $(seq 1 120); do kill -0 {os.getpid()} 2>/dev/null || break; sleep 0.5; done\n"
        f'rm -rf "{target}"\n'
        f'mv "{staged}" "{target}"\n'
        f'xattr -dr com.apple.quarantine "{target}" 2>/dev/null || true\n'
        f'open "{target}"\n'
        f'rm -rf "{work}"\n'
    )
    os.chmod(helper, 0o755)
    subprocess.Popen(["/bin/bash", str(helper)], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _JOB.emit("OctoSlave will close, install the update, and reopen.", pct=100.0)
    return True


def _update_windows(version: str) -> bool:
    """Download the Inno Setup installer and run it silently after we exit.

    Inno upgrades in place over the existing install (same AppId), keeping the
    user's install directory and shortcuts. It is launched from a detached
    ``cmd`` that waits first, because it cannot overwrite ots.exe while ots.exe
    is the process asking for the update.
    """
    work = _staging_dir()
    exe = work / ASSET_WINDOWS
    _download(f"{REPO_URL}/releases/download/v{version}/{ASSET_WINDOWS}", exe, "downloading")

    _JOB.emit("Handing off to the installer…", pct=95.0, stage="installing")
    flags = "/SILENT /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS"
    # Setup installs over the same AppId, so the new ots.exe lands at the path
    # we are running from now — relaunch it afterwards so the user gets their
    # web UI back instead of a closed window and no explanation.
    relaunch = Path(sys.executable).resolve()
    # `timeout` gives this process time to shut down before Setup touches its files.
    subprocess.Popen(
        f'cmd /c timeout /t 3 /nobreak >nul'
        f' & "{exe}" {flags}'
        f' & start "" "{relaunch}" web',
        shell=True,
        creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    _JOB.emit("OctoSlave will close, install the update, and reopen.", pct=100.0)
    return True


# ---------------------------------------------------------------------------
# Quit helper — used by the web UI after a "quits_app" update
# ---------------------------------------------------------------------------

def quit_soon(delay: float = 1.5) -> None:
    """Exit the process shortly, so the swap helper can take over.

    ``os._exit`` on purpose: uvicorn's graceful shutdown waits on the very
    WebSocket connections the UI is holding open, which would stall the handoff.
    """
    def _bye():
        time.sleep(delay)
        os._exit(0)
    threading.Thread(target=_bye, daemon=True).start()


# ---------------------------------------------------------------------------
# Background check for the CLI (never blocks startup)
# ---------------------------------------------------------------------------

def check_in_background() -> None:
    """Refresh the cached check on a daemon thread; results show up next run.

    The CLI must not pay a network round-trip on startup, so it reads the cache
    and warms it for next time. ``~/Documents`` on an iCloud-evicted machine is
    already slow enough to start.
    """
    if not checks_enabled():
        return

    def _work():
        try:
            check()
        except Exception:
            pass
    threading.Thread(target=_work, daemon=True).start()


def cached_notice() -> str | None:
    """One-line 'update available' notice from cache only — no network, no delay."""
    if not checks_enabled():
        return None
    state = _read_state()
    cached = state.get("last_check") or {}
    latest = cached.get("latest")
    if not latest or not is_newer(latest, __version__):
        return None
    if state.get("skipped_version") == latest:
        return None
    return f"OctoSlave {latest} is available (you have {__version__}) — run `ots update`"
