#!/usr/bin/env bash
#
# Build a single-file executable for Promotion-MTC-Procese (macOS / Linux).
# Usage: ./build.sh
#
set -euo pipefail

# Always run from the script's own directory.
cd "$(dirname "$0")"

APP_NAME="Promotion-MTC-Procese"
ENTRY="Promotion-MTC-Procese.py"

# Use python3 if available, otherwise python.
PY="$(command -v python3 || command -v python)"

echo "==> Installing build dependencies..."
"$PY" -m pip install --upgrade pip
"$PY" -m pip install -r requirements.txt pyinstaller

echo "==> Cleaning previous build artifacts..."
rm -rf build dist "${APP_NAME}.spec"

echo "==> Building single-file executable with PyInstaller..."
"$PY" -m PyInstaller \
    --onefile \
    --clean \
    --name "$APP_NAME" \
    --hidden-import pyodbc \
    --collect-submodules cryptography \
    "$ENTRY"

echo ""
echo "==> Done. Executable is at: dist/${APP_NAME}"
echo "    Place config/key/logs next to the executable when you run it."
