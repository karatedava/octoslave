# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for macOS OctoSlave.app
#
# Build:
#   cd <repo-root>
#   pyinstaller installers/macos/octoslave.spec
#
# Output:  dist/OctoSlave.app
# DMG:     run  installers/macos/build_dmg.sh  after PyInstaller

import os
import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent.parent          # repo root
OCTOSLAVE_PKG = ROOT / "octoslave"


def _resolve_version() -> str:
    """Single-source the bundle version: OTS_VERSION env (set by CI from the
    git tag / pyproject) wins; otherwise read pyproject.toml directly."""
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

# ---------------------------------------------------------------------------
# Data files bundled into the app
# ---------------------------------------------------------------------------
datas = [
    (str(OCTOSLAVE_PKG / "prompt_profiles"), "octoslave/prompt_profiles"),
    (str(OCTOSLAVE_PKG / "constitution.md"), "octoslave"),
    (str(OCTOSLAVE_PKG / "web" / "static"),  "octoslave/web/static"),
    (str(OCTOSLAVE_PKG / "web" / "lab_static"), "octoslave/web/lab_static"),
]

# ---------------------------------------------------------------------------
# Hidden imports (modules loaded at runtime by FastAPI / uvicorn / click)
# ---------------------------------------------------------------------------
hidden = [
    # octoslave internals
    "octoslave.agent", "octoslave.interrupt", "octoslave.config", "octoslave.display",
    "octoslave.logger", "octoslave.parallel", "octoslave.research",
    "octoslave.tools", "octoslave.tools_bio", "octoslave.tools_cryo",
    "octoslave.remote", "octoslave.vault",
    "octoslave.web.app", "octoslave.wizard",
    "octoslave.mcp_client", "octoslave.mcp_registry",
    # octoslave Lab (dynamic multi-agent) — imported lazily, so list explicitly
    "octoslave.lab", "octoslave.lab.runner", "octoslave.lab.state",
    "octoslave.lab.agent_runtime", "octoslave.lab.director",
    "octoslave.lab.critic", "octoslave.lab.meeting",
    "octoslave.lab.foundry", "octoslave.lab.llm",
    # octoslave Science (web research orchestrator) — imported lazily
    "octoslave.science", "octoslave.science.session",
    "octoslave.science.orchestrator", "octoslave.science.tools",
    "octoslave.science.context",
    # macOS launcher
    "octoslave.mac_launcher",
    # FastAPI / Starlette
    "fastapi", "fastapi.routing", "fastapi.middleware",
    "starlette", "starlette.routing", "starlette.responses",
    "starlette.staticfiles", "starlette.websockets",
    # uvicorn
    "uvicorn", "uvicorn.main", "uvicorn.config",
    "uvicorn.lifespan.on", "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.protocols.http.h11_impl", "uvicorn.protocols.http.httptools_impl",
    # openai
    "openai", "openai._models", "openai.types",
    # rich / prompt_toolkit
    "rich", "rich.console", "rich.markdown",
    "prompt_toolkit", "prompt_toolkit.shortcuts",
    # misc
    "click", "requests", "bs4", "fitz",
    "openpyxl", "docx", "psutil", "multipart",
    # tkinter (bundled with Python on macOS)
    "tkinter", "tkinter.ttk", "tkinter.messagebox",
    # email / http stdlib extras required by httpx/openai
    "email.mime.multipart", "email.mime.text", "email.mime.base",
    "http.server",
]

a = Analysis(
    [str(ROOT / "installers" / "macos" / "launcher.py")],
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
    [],
    exclude_binaries=True,
    name="OctoSlave",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # windowed .app — no terminal
    disable_windowed_traceback=False,
    argv_emulation=True,    # macOS: forward Finder argv
    target_arch=None,       # None = native; set "universal2" for fat binary
    codesign_identity=None,
    entitlements_file=None,
    icon=str(OCTOSLAVE_PKG / "web" / "static" / "logo.png"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="OctoSlave",
)

app = BUNDLE(
    coll,
    name="OctoSlave.app",
    icon=str(OCTOSLAVE_PKG / "web" / "static" / "logo.png"),
    bundle_identifier="cz.einfra.octoslave",
    info_plist={
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion":            VERSION,
        "NSHighResolutionCapable":    True,
        "NSRequiresAquaSystemAppearance": False,  # respect Dark Mode
        "LSMinimumSystemVersion":     "11.0",
    },
)
