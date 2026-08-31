# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec — Windows CLI binary  (ots.exe)
#
# Build:
#   cd <repo-root>
#   pyinstaller installers\windows\ots_cli.spec
#
# Output: dist\ots.exe

import os
import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent.parent
OCTOSLAVE_PKG = ROOT / "octoslave"

def _resolve_version() -> str:
    """Single-source the build version: OTS_VERSION env (set by CI from the git
    tag / pyproject) wins; otherwise read pyproject.toml directly."""
    env = os.environ.get("OTS_VERSION", "").strip()
    if env:
        return env
    try:
        import tomllib
        with open(ROOT / "pyproject.toml", "rb") as f:
            return tomllib.load(f)["project"]["version"]
    except Exception:
        return "0.0.0"


VERSION = _resolve_version()

block_cipher = None

datas = [
    (str(OCTOSLAVE_PKG / "prompt_profiles"), "octoslave/prompt_profiles"),
    (str(OCTOSLAVE_PKG / "constitution.md"), "octoslave"),
    (str(OCTOSLAVE_PKG / "web" / "static"),  "octoslave/web/static"),
    (str(OCTOSLAVE_PKG / "web" / "lab_static"), "octoslave/web/lab_static"),
]

# Version stamp — PyInstaller ships no dist-info, so importlib.metadata cannot
# see a version inside the bundle. octoslave/__init__.py reads this file first
# when frozen; without it the app reports 0.0.0+unknown and the in-app updater
# would offer to "upgrade" to every release forever.
_STAMP_DIR = ROOT / "build" / "version_stamp"
_STAMP_DIR.mkdir(parents=True, exist_ok=True)
(_STAMP_DIR / "_build_version.txt").write_text(VERSION)
datas.append((str(_STAMP_DIR / "_build_version.txt"), "octoslave"))

hidden = [
    "octoslave.agent", "octoslave.interrupt", "octoslave.config", "octoslave.display",
    "octoslave.logger", "octoslave.parallel", "octoslave.research",
    "octoslave.tools", "octoslave.tools_bio", "octoslave.tools_cryo",
    "octoslave.remote", "octoslave.vault", "octoslave.updater",
    "octoslave.web.app", "octoslave.wizard",
    "octoslave.mcp_client", "octoslave.mcp_registry",
    "octoslave.lab", "octoslave.lab.runner", "octoslave.lab.state",
    "octoslave.lab.agent_runtime", "octoslave.lab.director",
    "octoslave.lab.critic", "octoslave.lab.meeting",
    "octoslave.lab.foundry", "octoslave.lab.llm",
    # octoslave Science (web research orchestrator) — imported lazily
    "octoslave.science", "octoslave.science.session",
    "octoslave.science.orchestrator", "octoslave.science.tools",
    "octoslave.science.context",
    "fastapi", "fastapi.routing", "fastapi.middleware",
    "starlette", "starlette.routing", "starlette.responses",
    "starlette.staticfiles", "starlette.websockets",
    "uvicorn", "uvicorn.main", "uvicorn.config",
    "uvicorn.lifespan.on",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.http.httptools_impl",
    "openai", "openai._models", "openai.types",
    "rich", "rich.console", "rich.markdown",
    "prompt_toolkit", "prompt_toolkit.shortcuts",
    "click", "requests", "bs4", "fitz",
    "openpyxl", "docx", "psutil", "multipart",
    "tkinter", "tkinter.ttk", "tkinter.messagebox",
    "email.mime.multipart", "email.mime.text", "email.mime.base",
]

a = Analysis(
    [str(ROOT / "installers" / "cli_entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["rdkit", "anndata", "scipy", "pytesseract"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="ots",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,           # keep console window for TUI
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(OCTOSLAVE_PKG / "web" / "static" / "logo.png"),
)
