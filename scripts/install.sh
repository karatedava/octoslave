#!/usr/bin/env bash
#
# OctoSlave one-shot installer.
#
# Usage:
#   bash scripts/install.sh                 # from inside a fresh clone
#   curl -fsSL <hosted url> | bash          # standalone (when published)
#
# Strategy (in order):
#   1. Pick a Python ≥ 3.10 (prefers python3.13/3.12/3.11/3.10).
#   2. Force temp/cache dirs onto $HOME — many HPC nodes have a tiny
#      /dev/shm (uv/pipx default) that fills up, killing installs.
#   3. Detect whether we're running from inside a cloned octoslave repo.
#      If yes → install from the local path (fastest, no network).
#   4. Otherwise → try PyPI, then git+main.
#   5. If pipx fails (broken on HPC, sandboxed Python, etc.), fall back
#      to a plain `python -m venv` + `pip install` and symlink `ots`
#      into ~/.local/bin/ — that works on every UNIX with Python 3.10+.
#
# Environment overrides:
#   OCTOSLAVE_REF        git ref to install (default: tagged release; fallback: main)
#   OCTOSLAVE_REPO       git URL  (default: https://github.com/karatedava/octoslave)
#   OCTOSLAVE_EXTRAS     pip extras spec (default: [all]; use "" to skip extras)
#   OCTOSLAVE_NO_PIPX    set to 1 to skip pipx and go straight to venv install
#   OCTOSLAVE_VENV_DIR   where the fallback venv lives (default: ~/.local/share/octoslave-venv)
#   OCTOSLAVE_NO_CODAG   set to 1 to skip installing the codag CLI
#                        (used by the `compress_log` tool — optional)
#

set -euo pipefail

# ── Configurable bits ─────────────────────────────────────────────────────
OCTOSLAVE_REPO="${OCTOSLAVE_REPO:-https://github.com/karatedava/octoslave}"
OCTOSLAVE_REF="${OCTOSLAVE_REF:-}"
OCTOSLAVE_EXTRAS="${OCTOSLAVE_EXTRAS-[all]}"
OCTOSLAVE_NO_PIPX="${OCTOSLAVE_NO_PIPX:-0}"
OCTOSLAVE_VENV_DIR="${OCTOSLAVE_VENV_DIR:-$HOME/.local/share/octoslave-venv}"
OCTOSLAVE_NO_CODAG="${OCTOSLAVE_NO_CODAG:-0}"

# ── Pretty output helpers ─────────────────────────────────────────────────
bold()   { printf "\033[1m%s\033[0m\n" "$*"; }
green()  { printf "\033[32m%s\033[0m\n" "$*"; }
yellow() { printf "\033[33m%s\033[0m\n" "$*"; }
red()    { printf "\033[31m%s\033[0m\n" "$*" >&2; }
info()   { printf "  \033[2m%s\033[0m\n" "$*"; }

bold "🐙  OctoSlave installer"
echo

# ── 1. Redirect tmp / cache dirs away from /dev/shm ───────────────────────
#
# uv (used by recent pipx releases) writes interpreter-introspection
# scripts to TMPDIR. Many HPC nodes set TMPDIR=/dev/shm with very little
# space — installs fail mid-way with "No space left on device".
# Pinning these to ~/.cache/* avoids the problem on every machine we
# tested, including squashfs / network-home setups.
#
mkdir -p "$HOME/.cache/octoslave-tmp" "$HOME/.cache/uv" 2>/dev/null || true
export TMPDIR="$HOME/.cache/octoslave-tmp"
export UV_CACHE_DIR="$HOME/.cache/uv"

# ── 2. Pick a Python ───────────────────────────────────────────────────────
PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
        version=$("$candidate" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "")
        if [ -n "$version" ]; then
            major=${version%%.*}
            minor=${version#*.}
            if [ "$major" -ge 3 ] && [ "$minor" -ge 10 ]; then
                PYTHON="$candidate"
                break
            fi
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    red "No Python ≥ 3.10 found on PATH."
    echo
    echo "  macOS:  brew install python@3.12"
    echo "  Linux:  sudo apt install python3.12 python3.12-venv pipx"
    exit 1
fi

green "✓ using $($PYTHON --version) at $(command -v "$PYTHON")"

# ── 3. Detect local clone (so we don't fetch from the network needlessly) ──
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LOCAL_REPO=""
if [ -f "$REPO_ROOT/pyproject.toml" ] && \
   grep -q '^name = "octoslave"' "$REPO_ROOT/pyproject.toml" 2>/dev/null; then
    LOCAL_REPO="$REPO_ROOT"
    info "found local clone at $LOCAL_REPO — installing from there"
fi

# ── 4. Build install spec (priority: local clone → PyPI → git@main) ───────
build_specs() {
    # Echoes one or more pip-installable specs, in the order to try them.
    if [ -n "$OCTOSLAVE_REF" ]; then
        echo "git+${OCTOSLAVE_REPO}@${OCTOSLAVE_REF}#egg=octoslave${OCTOSLAVE_EXTRAS}"
        return
    fi
    if [ -n "$LOCAL_REPO" ]; then
        echo "${LOCAL_REPO}${OCTOSLAVE_EXTRAS}"
        return
    fi
    echo "octoslave${OCTOSLAVE_EXTRAS}"
    echo "git+${OCTOSLAVE_REPO}@main#egg=octoslave${OCTOSLAVE_EXTRAS}"
}

mapfile -t SPECS < <(build_specs)

# ── 5. Try pipx, falling back to a plain venv if anything goes wrong ──────
install_via_pipx() {
    if [ "$OCTOSLAVE_NO_PIPX" = "1" ]; then
        return 1
    fi

    if ! command -v pipx >/dev/null 2>&1; then
        yellow "pipx not found — installing it for you"
        if ! "$PYTHON" -m pip install --user --quiet --upgrade pipx; then
            red "  could not install pipx with $PYTHON -m pip"
            return 1
        fi
        "$PYTHON" -m pipx ensurepath >/dev/null 2>&1 || true
        local PIPX_BIN_DIR
        PIPX_BIN_DIR="$("$PYTHON" -m pipx environment --value PIPX_BIN_DIR 2>/dev/null || echo "$HOME/.local/bin")"
        export PATH="$PIPX_BIN_DIR:$PATH"
    fi
    green "✓ pipx ready"

    if pipx list 2>/dev/null | grep -q "package octoslave "; then
        for SPEC in "${SPECS[@]}"; do
            info "upgrading via pipx upgrade (or reinstall): $SPEC"
            if pipx upgrade octoslave >/dev/null 2>&1; then
                return 0
            fi
            if pipx install --force "$SPEC"; then
                return 0
            fi
        done
        return 1
    fi

    for SPEC in "${SPECS[@]}"; do
        info "pipx install $SPEC"
        if pipx install "$SPEC"; then
            return 0
        fi
        yellow "  pipx install of '$SPEC' failed — trying next spec"
    done
    return 1
}

install_via_venv() {
    yellow "Falling back to a plain venv at $OCTOSLAVE_VENV_DIR"
    if [ -d "$OCTOSLAVE_VENV_DIR" ]; then
        info "removing existing venv"
        rm -rf "$OCTOSLAVE_VENV_DIR"
    fi
    if ! "$PYTHON" -m venv "$OCTOSLAVE_VENV_DIR"; then
        red "could not create venv at $OCTOSLAVE_VENV_DIR"
        return 1
    fi
    # Use the venv's pip, not pipx, not uv — most predictable.
    "$OCTOSLAVE_VENV_DIR/bin/pip" install --quiet --upgrade pip wheel
    local installed=0
    for SPEC in "${SPECS[@]}"; do
        info "pip install $SPEC"
        if "$OCTOSLAVE_VENV_DIR/bin/pip" install "$SPEC"; then
            installed=1
            break
        fi
        yellow "  pip install of '$SPEC' failed — trying next spec"
    done
    if [ "$installed" != "1" ]; then
        red "All install attempts failed."
        return 1
    fi
    mkdir -p "$HOME/.local/bin"
    ln -sf "$OCTOSLAVE_VENV_DIR/bin/ots"       "$HOME/.local/bin/ots"
    ln -sf "$OCTOSLAVE_VENV_DIR/bin/octoslave" "$HOME/.local/bin/octoslave"
    green "✓ symlinked ots → $HOME/.local/bin/ots"
    return 0
}

if install_via_pipx; then
    INSTALL_PATH="$(command -v ots 2>/dev/null || echo "(via pipx)")"
elif install_via_venv; then
    INSTALL_PATH="$HOME/.local/bin/ots"
else
    red "Installation failed via both pipx and plain venv."
    echo
    echo "  Re-run with OCTOSLAVE_NO_PIPX=1 to skip pipx entirely:"
    echo "    OCTOSLAVE_NO_PIPX=1 bash scripts/install.sh"
    echo
    echo "  Or open an issue with the output above:"
    echo "    https://github.com/karatedava/octoslave/issues"
    exit 1
fi

echo
green "✓ octoslave installed"
echo

# ── 6. Install codag CLI (optional — powers the `compress_log` tool) ──────
#
# codag compresses noisy log streams into compact summaries so the agent
# can read them without blowing the context window. Free `compact` mode
# needs no account. Skip with OCTOSLAVE_NO_CODAG=1.
#
install_codag() {
    if [ "$OCTOSLAVE_NO_CODAG" = "1" ]; then
        info "skipping codag install (OCTOSLAVE_NO_CODAG=1)"
        return 0
    fi
    if command -v codag >/dev/null 2>&1; then
        green "✓ codag already installed at $(command -v codag)"
        return 0
    fi
    if [ -x "$HOME/.local/bin/codag" ]; then
        green "✓ codag already installed at $HOME/.local/bin/codag"
        return 0
    fi
    info "installing codag CLI (used by the compress_log tool)"
    if ! command -v curl >/dev/null 2>&1; then
        yellow "  curl not found — skipping codag install"
        return 0
    fi
    if curl -fsSL https://codag.ai/install.sh | sh >/dev/null 2>&1; then
        green "✓ codag installed (free 'compact' mode needs no account)"
    else
        yellow "  codag install failed — compress_log will be unavailable."
        yellow "  Re-run later with: curl -fsSL https://codag.ai/install.sh | sh"
    fi
}
install_codag

echo

# ── 7. Next steps ──────────────────────────────────────────────────────────
bold "Next steps:"
echo
echo "  1.  Configure a backend (e-INFRA CZ / NVIDIA NIM / Ollama):"
echo "        ots config"
echo
echo "  2.  Try it:"
echo "        ots                         # interactive TUI"
echo "        ots web                     # browser UI"
echo "        ots run \"hello world\"       # one-shot task"
echo
echo "  3.  Multi-agent mode:"
echo "        ots run \"refactor X\" --parallel 3 --strategy best"
echo

if ! command -v ots >/dev/null 2>&1; then
    yellow "Note: 'ots' is not on your PATH yet — start a new shell, or run:"
    echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

echo
info "Installed to: $INSTALL_PATH"
info "Docs:         https://github.com/karatedava/octoslave"
