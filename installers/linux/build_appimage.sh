#!/usr/bin/env bash
# Build OctoSlave AppImage for Linux
#
# Requirements (install once):
#   pip install pyinstaller
#   Download appimagetool from https://github.com/AppImage/appimagetool/releases
#   Make it executable and place in $PATH (e.g. /usr/local/bin/appimagetool)
#
# Usage:
#   cd <repo-root>
#   bash installers/linux/build_appimage.sh
#
# Output: dist/OctoSlave-x86_64.AppImage

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
DIST_DIR="${ROOT_DIR}/dist"
BUILD_DIR="${DIST_DIR}/AppImage-build"
APPNAME="OctoSlave"

# Single-source the version: OTS_VERSION env (set by CI) wins, else read pyproject.
VERSION="${OTS_VERSION:-$(python3 -c "import tomllib,pathlib;print(tomllib.loads(pathlib.Path('${ROOT_DIR}/pyproject.toml').read_text())['project']['version'])" 2>/dev/null || echo 0.0.0)}"
export OTS_VERSION="${VERSION}"

# ── 1. Install / update PyInstaller if needed
echo "==> Checking PyInstaller…"
python3 -m pip install --quiet --upgrade pyinstaller

# ── 2. Build the single-file binary
echo "==> Building ots binary with PyInstaller…"
cd "${ROOT_DIR}"
python3 -m PyInstaller \
    --clean \
    --noconfirm \
    "${SCRIPT_DIR}/octoslave.spec"

if [[ ! -f "${DIST_DIR}/ots" ]]; then
    echo "ERROR: dist/ots not found after PyInstaller build"
    exit 1
fi

# ── 3. Prepare AppDir layout
echo "==> Preparing AppDir…"
rm -rf "${BUILD_DIR}"
mkdir -p "${BUILD_DIR}/usr/bin"
mkdir -p "${BUILD_DIR}/usr/share/applications"
mkdir -p "${BUILD_DIR}/usr/share/icons/hicolor/256x256/apps"
mkdir -p "${BUILD_DIR}/usr/share/metainfo"

# Copy binary
cp "${DIST_DIR}/ots" "${BUILD_DIR}/usr/bin/ots"
chmod +x "${BUILD_DIR}/usr/bin/ots"

# Desktop entry
cp "${SCRIPT_DIR}/octoslave.desktop" \
   "${BUILD_DIR}/usr/share/applications/octoslave.desktop"

# Symlink for AppImage root
cp "${SCRIPT_DIR}/octoslave.desktop" "${BUILD_DIR}/octoslave.desktop"

# Icon — appimagetool requires the icon at the AppDir root named after Icon= in the .desktop file
if [[ -f "${ROOT_DIR}/octoslave/web/static/logo.png" ]]; then
    cp "${ROOT_DIR}/octoslave/web/static/logo.png" \
       "${BUILD_DIR}/octoslave.png"                          # root — required by appimagetool
    cp "${ROOT_DIR}/octoslave/web/static/logo.png" \
       "${BUILD_DIR}/usr/share/icons/hicolor/256x256/apps/octoslave.png"
    cp "${ROOT_DIR}/octoslave/web/static/logo.png" \
       "${BUILD_DIR}/.DirIcon"                               # fallback volume icon
fi

# AppRun launcher — AppImage entry point
cat > "${BUILD_DIR}/AppRun" << 'EOF'
#!/bin/bash
# AppRun — AppImage entry point
# Supports both CLI (when run from terminal) and desktop launch

HERE="$(dirname "$(readlink -f "$0")")"
export PATH="${HERE}/usr/bin:${PATH}"

# If launched from desktop (no args), start the web UI
if [[ $# -eq 0 ]]; then
    exec "${HERE}/usr/bin/ots" web
else
    exec "${HERE}/usr/bin/ots" "$@"
fi
EOF
chmod +x "${BUILD_DIR}/AppRun"

# ── 4. Build AppImage
echo "==> Building AppImage…"
APPIMAGE_OUTPUT="${DIST_DIR}/OctoSlave-${VERSION}-x86_64.AppImage"
rm -f "${APPIMAGE_OUTPUT}"

if ! command -v appimagetool &> /dev/null; then
    echo "ERROR: appimagetool not found in PATH."
    echo "       Download from: https://github.com/AppImage/appimagetool/releases"
    echo "       Example: wget -O /usr/local/bin/appimagetool <url> && chmod +x /usr/local/bin/appimagetool"
    exit 1
fi

ARCH=x86_64 appimagetool \
    --appimage-extract-and-run \
    "${BUILD_DIR}" \
    "${APPIMAGE_OUTPUT}"

chmod +x "${APPIMAGE_OUTPUT}"

echo ""
echo "✓  AppImage ready: ${APPIMAGE_OUTPUT}"
echo "   Distribute this file — users just chmod +x and run."
