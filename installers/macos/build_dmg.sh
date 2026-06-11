#!/usr/bin/env bash
# Build OctoSlave.dmg for macOS
#
# Requirements (install once):
#   pip install pyinstaller
#   brew install create-dmg
#
# Usage (from repo root):
#   bash installers/macos/build_dmg.sh
#
# Output: dist/OctoSlave-macOS.dmg
#
# Pass --skip-pyinstaller if the .app is already built (e.g. in CI where
# PyInstaller ran in a prior step).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
DIST_DIR="${ROOT_DIR}/dist"
APP_PATH="${DIST_DIR}/OctoSlave.app"
SKIP_BUILD=false

# Single-source the version: OTS_VERSION env (set by CI) wins, else read pyproject.
VERSION="${OTS_VERSION:-$(python3 -c "import tomllib,pathlib;print(tomllib.loads(pathlib.Path('${ROOT_DIR}/pyproject.toml').read_text())['project']['version'])" 2>/dev/null || echo 0.0.0)}"
export OTS_VERSION="${VERSION}"   # ensure the PyInstaller spec sees it too

for arg in "$@"; do
    [[ "$arg" == "--skip-pyinstaller" ]] && SKIP_BUILD=true
done

# ── 1. Install / update PyInstaller if not skipped
if [[ "${SKIP_BUILD}" == "false" ]]; then
    echo "==> Checking PyInstaller…"
    python3 -m pip install --quiet --upgrade pyinstaller

    # ── 2. Build the .app bundle
    echo "==> Building OctoSlave.app with PyInstaller…"
    cd "${ROOT_DIR}"
    python3 -m PyInstaller \
        --clean \
        --noconfirm \
        "${SCRIPT_DIR}/octoslave.spec"
fi

if [[ ! -d "${APP_PATH}" ]]; then
    echo "ERROR: OctoSlave.app not found at ${APP_PATH}"
    exit 1
fi

# ── 3. Ad-hoc codesign (stops Gatekeeper from blocking the .app)
#    For distribution replace '-' with your Apple Developer ID:
#      "Developer ID Application: Your Name (TEAMID)"
echo "==> Code-signing (ad-hoc)…"
codesign --force --deep --sign "-" "${APP_PATH}" || true

# ── 4. Convert logo.png → .icns for the DMG volume icon
ICNS_PATH="/tmp/octoslave_vol.icns"
ICONSET_DIR="/tmp/octoslave.iconset"
LOGO_PNG="${ROOT_DIR}/octoslave/web/static/logo.png"

if command -v sips &>/dev/null && command -v iconutil &>/dev/null && [[ -f "${LOGO_PNG}" ]]; then
    echo "==> Converting logo.png → .icns…"
    rm -rf "${ICONSET_DIR}"
    mkdir -p "${ICONSET_DIR}"
    for size in 16 32 64 128 256 512; do
        sips -z ${size} ${size} "${LOGO_PNG}" \
             --out "${ICONSET_DIR}/icon_${size}x${size}.png" &>/dev/null
    done
    iconutil -c icns "${ICONSET_DIR}" -o "${ICNS_PATH}"
    VOLICON_ARGS="--volicon ${ICNS_PATH}"
else
    echo "==> Skipping volume icon (sips/iconutil not available)"
    VOLICON_ARGS=""
fi

# ── 5. Create the DMG
DMG_PATH="${DIST_DIR}/OctoSlave-macOS-${VERSION}.dmg"
echo "==> Creating DMG at ${DMG_PATH}…"
rm -f "${DMG_PATH}"

# shellcheck disable=SC2086
create-dmg \
    --volname "OctoSlave" \
    ${VOLICON_ARGS} \
    --window-pos 200 120 \
    --window-size 660 420 \
    --icon-size 128 \
    --icon "OctoSlave.app" 180 200 \
    --hide-extension "OctoSlave.app" \
    --app-drop-link 480 200 \
    --no-internet-enable \
    "${DMG_PATH}" \
    "${APP_PATH}"

echo ""
echo "✓  DMG ready: ${DMG_PATH}"
echo "   Distribute this file — users just double-click, drag to Applications, done."
