#!/usr/bin/env bash
# Build ClickSpeak.app using py2app and install to /Applications.
#
# Usage:
#   bash scripts/build_app.sh          # alias mode (dev — fast, symlinks source)
#   bash scripts/build_app.sh release  # standalone bundle (slow, self-contained)
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "${PROJECT_DIR}"

APP_BUNDLE="/Applications/ClickSpeak.app"
PYTHON="${PROJECT_DIR}/.venv/bin/python"
MODE="${1:-alias}"

if [ ! -x "${PYTHON}" ]; then
    echo "[build] No venv python found. Run: uv sync"
    exit 1
fi

# Ensure py2app is installed
"${PYTHON}" -c "import py2app" 2>/dev/null || {
    echo "[build] Installing py2app..."
    uv pip install py2app
}

# Clean previous build
rm -rf "${PROJECT_DIR}/dist" "${PROJECT_DIR}/build"

# py2app chokes on pyproject.toml [project].dependencies → hide it
mv pyproject.toml pyproject.toml.bak
trap 'mv "${PROJECT_DIR}/pyproject.toml.bak" "${PROJECT_DIR}/pyproject.toml"' EXIT

if [ "${MODE}" = "release" ]; then
    echo "[build] Building standalone .app bundle..."
    PYTHONPATH=src "${PYTHON}" setup.py py2app
else
    echo "[build] Building alias-mode .app (dev)..."
    PYTHONPATH=src "${PYTHON}" setup.py py2app -A
fi

# Remove stale bundle and install
if [ -d "${APP_BUNDLE}" ]; then
    echo "[build] Removing old ${APP_BUNDLE}"
    rm -rf "${APP_BUNDLE}"
fi

cp -R dist/ClickSpeak.app "${APP_BUNDLE}"

# Ad-hoc code sign
codesign --force --deep --sign - "${APP_BUNDLE}"

echo "[build] Installed ${APP_BUNDLE}"
echo "[build] Binary: $(file "${APP_BUNDLE}/Contents/MacOS/ClickSpeak")"
echo "[build] Bundle ID: $(codesign -dvv "${APP_BUNDLE}" 2>&1 | grep Identifier= | head -1)"
echo ""
echo "[build] Done. Launch from Spotlight or:"
echo "  open /Applications/ClickSpeak.app"
