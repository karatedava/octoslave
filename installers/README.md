# OctoSlave Installers — Developer Build Docs

This directory contains everything needed to build platform-specific installers for OctoSlave.

| Platform | Output | Primary Tool |
|----------|--------|-------------|
| macOS | `.dmg` disk image | PyInstaller + `create-dmg` |
| Windows | `.exe` installer | PyInstaller + Inno Setup |
| Linux | `.AppImage` | PyInstaller + `appimagetool` |

All three share the same workflow:
1. **PyInstaller** bundles the Python app + data into a frozen binary.
2. **Platform-specific tooling** wraps that binary into the final distributable.

---

## Prerequisites (one-time setup)

### All platforms
```bash
pip install pyinstaller
git clone https://github.com/karatedava/octoslave.git
cd octoslave
pip install -e ".[all]"

# Build the Lab web UI (Vite/React) → octoslave/web/lab_static/
# Required before PyInstaller: the .spec files bundle web/lab_static.
# (Needs Node.js 18+. The built assets are committed, so this is only needed
#  when the frontend changed; CI always rebuilds them.)
npm ci --prefix frontend && npm run build --prefix frontend
```

### macOS
```bash
brew install create-dmg
```

### Windows
Download and install **Inno Setup 6** from https://jrsoftware.org/isinfo.php  
(The build script auto-detects its install path.)

### Linux
Download `appimagetool` (x86_64) and place it in `$PATH`:
```bash
wget -O /usr/local/bin/appimagetool \
  https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
chmod +x /usr/local/bin/appimagetool
sudo apt-get install -y libfuse2 || sudo apt-get install -y libfuse2t64  # Ubuntu 24.04+ renamed the package
```

---

## Build Commands

### macOS
```bash
bash installers/macos/build_dmg.sh
# Output: dist/OctoSlave-macOS.dmg
```

### Windows
```cmd
installers\windows\build_installer.bat
:: Output: dist\OctoSlave-Windows-Installer.exe
```

### Linux
```bash
bash installers/linux/build_appimage.sh
# Output: dist/OctoSlave-x86_64.AppImage
```

---

## File Reference

```
installers/
├── README.md                  ← this file
├── macos/
│   ├── launcher.py            # macOS .app entry point (wizard detection + GUI/CLI routing)
│   ├── gui_launcher.py        # (legacy alias — logic lives in octoslave/mac_launcher.py)
│   ├── octoslave.spec         # PyInstaller spec for OctoSlave.app (windowed bundle)
│   └── build_dmg.sh           # Full DMG build pipeline (PNG→ICNS conversion + create-dmg)
├── windows/
│   ├── ots_cli.spec           # PyInstaller spec for console CLI binary (ots.exe)
│   ├── ots_wizard.spec        # PyInstaller spec for windowed first-run wizard
│   ├── wizard_entry.py        # Entry point compiled into OctoSlave-Setup.exe
│   ├── installer.iss          # Inno Setup 6 script (installer UI, PATH, shortcuts)
│   ├── web_launcher.bat       # Shortcut target for "OctoSlave Web UI"
│   └── build_installer.bat    # Full Windows build pipeline (CLI + wizard + ISCC)
├── linux/
│   ├── octoslave.spec         # PyInstaller spec for single-file console binary
│   ├── octoslave.desktop      # Freedesktop .desktop entry (categories, icon, exec)
│   └── build_appimage.sh      # Full AppImage build pipeline
└── .. (shared platform files)
```

---

## How the First-Run Wizard Works

1. **Wizard file**: `octoslave/wizard.py` — a 6-step tkinter GUI (Welcome → Backend → Credentials → Model → Test → Done).
2. **Trigger**: `octoslave/main.py` checks `needs_wizard()` inside `cli()` when invoked without a subcommand. It only fires in **frozen PyInstaller bundles**, not pip installs.
3. **Re-run anytime**: `ots config --wizard` or via the platform launcher's "Configuration" button.

### Platform-specific wizard behaviour

| Platform | Wizard runs when | Re-run via |
|----------|-----------------|------------|
| macOS | `.app` opened from Finder with no config | App → "Configuration" button |
| Windows | Installer completes (post-install) | Start Menu → "Configure OctoSlave" |
| Linux | First `ots web` from AppImage with no config | Terminal: `ots config --wizard` |

---

## GitHub Actions CI

`.github/workflows/build-installers.yml` builds all three installers on every tag push (`v*`) and uploads them as release artifacts.

Manual rebuild:
```bash
gh workflow run build-installers.yml
```

---

## Version Bumping

The version has a **single source of truth: `[project] version` in `pyproject.toml`.**
Everything else derives from it automatically — `ots --version`,
`octoslave.__version__`, the macOS `.app` bundle version, the Windows installer
version, and every artifact filename (all resolved via `scripts/version.py`).

To cut a release:

1. Bump the one number in `pyproject.toml`, e.g. `version = "0.3.0"`.
2. Commit, then tag with a **matching** `vX.Y.Z` and push the tag:
   ```bash
   git commit -am "Release 0.3.0"
   git tag -a v0.3.0 -m "Release 0.3.0"
   git push origin main --tags
   ```
3. CI verifies the tag matches `pyproject.toml` (the `verify-version` job fails
   the build if they differ), then drafts a release with all three installers
   attached — named `OctoSlave-macOS-0.3.0.dmg`,
   `OctoSlave-Windows-Installer-0.3.0.exe`, `OctoSlave-0.3.0-x86_64.AppImage`.
4. The release is created as a **draft** — review it and click Publish. Older
   releases remain available on the Releases page; nothing is overwritten.

> Follow [semantic versioning](https://semver.org): MAJOR.MINOR.PATCH —
> bump PATCH for fixes, MINOR for backward-compatible features, MAJOR for
> breaking changes.

---

## Troubleshooting

**PyImportError / missing module at runtime:**  
Add the module to the `hiddenimports` list in the platform's `.spec` file. The
`octoslave.lab.*` modules are imported lazily, so they are listed explicitly in
the specs — add any new `lab` submodule there too.

**Lab web UI blank / 404 at `/lab`:**  
The frozen build is missing `octoslave/web/lab_static`. Run
`npm run build --prefix frontend` before PyInstaller (the specs bundle that
directory; CI builds it automatically).

**tkinter not found in frozen build:**  
On Linux, ensure `python3-tk` / `tk-dev` is installed at build time. tkinter is bundled natively on macOS and Windows.

**macOS Gatekeeper blocks the .app:**  
The build script performs ad-hoc codesign (`codesign --sign -`). For distribution, use a valid Apple Developer ID and set `codesign_identity` in the `.spec`.

**Inno Setup not found on Windows:**  
Install Inno Setup 6 to the default path (`C:\Program Files (x86)\Inno Setup 6\`). The `.bat` scans common locations automatically.

---

## Size & Compression

| Platform | Unpacked | Final Package |
|----------|----------|---------------|
| macOS .app | ~180 MB | ~75 MB DMG |
| Windows .exe | ~190 MB | ~85 MB installer |
| Linux AppImage | ~170 MB | ~70 MB AppImage |

Binary size is dominated by PyTorch, OpenCV, and scientific Python libraries. Consider excluding heavy deps (`rdkit`, `anndata`, etc.) in the `.spec` files if your users don't need them.
