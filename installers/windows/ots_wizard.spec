# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec — Windows first-run wizard  (OctoSlave-Setup.exe)
#
# Build:
#   cd <repo-root>
#   pyinstaller installers\windows\ots_wizard.spec
#
# Output: dist\OctoSlave-Setup.exe

import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent.parent
OCTOSLAVE_PKG = ROOT / "octoslave"

block_cipher = None

datas = [
    (str(OCTOSLAVE_PKG / "prompt_profiles"), "octoslave/prompt_profiles"),
    (str(OCTOSLAVE_PKG / "constitution.md"), "octoslave"),
    (str(OCTOSLAVE_PKG / "web" / "static"),  "octoslave/web/static"),
]

hidden = [
    "octoslave.config", "octoslave.wizard",
    "openai", "openai._models",
    "tkinter", "tkinter.ttk", "tkinter.messagebox",
    "requests",
]

a = Analysis(
    [str(ROOT / "installers" / "windows" / "wizard_entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["rdkit", "anndata", "scipy", "pytesseract",
              "fastapi", "uvicorn", "rich", "prompt_toolkit"],
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
    name="OctoSlave-Setup",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # windowed — no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(OCTOSLAVE_PKG / "web" / "static" / "logo.png"),
)
