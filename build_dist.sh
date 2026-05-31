#!/usr/bin/env bash
# build_dist.sh — full distributable build for mini_agent Electron app.
#
# Stages:
#   1. PyInstaller: freeze Python backend → standalone binary
#   2. Vite: build Electron renderer (React → static HTML/JS/CSS)
#   3. electron-builder: package everything → .dmg / .exe / .AppImage
#
# Prerequisites:
#   - Node.js 18+  (for electron-builder, vite)
#   - Python 3.10+ with venv (for PyInstaller)
#   - pip packages: pyinstaller + all from requirements.txt
#
# Usage:
#   bash build_dist.sh              # build for current platform
#   bash build_dist.sh --mac        # build macOS .dmg
#   bash build_dist.sh --win        # build Windows .exe (cross-compile limited)
#   bash build_dist.sh --linux      # build Linux .AppImage
#
# Output: mini_agent_electron/dist-electron/

set -euo pipefail

cd "$(dirname "$0")"

RED='\033[0;31m'
GREEN='\033[0;32m'
BOLD='\033[1m'
NC='\033[0m'

PLATFORM="${1:-}"

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║     mini_agent — distributable build         ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════════╝${NC}"
echo ""

# ----------------------------------------------------------------------
# Stage 0: Check prerequisites
# ----------------------------------------------------------------------

echo -e "${BOLD}[0/3] Checking prerequisites...${NC}"

ERRORS=0

# Python 3
if command -v python3 &>/dev/null; then
    PY_VER=$(python3 --version 2>&1 | awk '{print $2}')
    echo -e "  ${GREEN}✓${NC} Python 3  (${PY_VER})"
else
    echo -e "  ${RED}✗${NC} Python 3 not found"
    ERRORS=$((ERRORS + 1))
fi

# Node.js
if command -v node &>/dev/null; then
    NODE_VER=$(node --version)
    echo -e "  ${GREEN}✓${NC} Node.js   (${NODE_VER})"
else
    echo -e "  ${RED}✗${NC} Node.js not found"
    ERRORS=$((ERRORS + 1))
fi

# PyInstaller
if python3 -c "import PyInstaller" 2>/dev/null; then
    echo -e "  ${GREEN}✓${NC} PyInstaller"
else
    echo -e "  ${RED}✗${NC} PyInstaller not installed. Run: pip install pyinstaller"
    ERRORS=$((ERRORS + 1))
fi

if [ $ERRORS -gt 0 ]; then
    echo ""
    echo -e "${RED}Missing ${ERRORS} prerequisite(s). Please install them and retry.${NC}"
    exit 1
fi

echo ""

# ----------------------------------------------------------------------
# Stage 1: PyInstaller — freeze Python backend
# ----------------------------------------------------------------------

echo -e "${BOLD}[1/3] Freezing Python backend (PyInstaller)...${NC}"

rm -rf pyinstaller_dist build 2>/dev/null || true

python3 -m PyInstaller pyinstaller_backend.spec --clean --noconfirm --log-level=WARN

# Verify the binary was produced
BINARY_NAME="mini_agent_backend"
if [[ "$(uname -s)" == "MINGW"* ]] || [[ "$(uname -s)" == "MSYS"* ]] || [[ "$(uname -s)" == "CYGWIN"* ]]; then
    BINARY_NAME="mini_agent_backend.exe"
fi

if [ -f "pyinstaller_dist/${BINARY_NAME}" ]; then
    BIN_SIZE=$(ls -lh "pyinstaller_dist/${BINARY_NAME}" | awk '{print $5}')
    echo -e "  ${GREEN}✓${NC} Backend binary: pyinstaller_dist/${BINARY_NAME}  (${BIN_SIZE})"
else
    echo -e "  ${RED}✗${NC} PyInstaller failed — binary not found at pyinstaller_dist/${BINARY_NAME}"
    echo "  Check pyinstaller_backend.spec for errors."
    exit 1
fi

echo ""

# ----------------------------------------------------------------------
# Stage 2: Build Electron renderer (Vite)
# ----------------------------------------------------------------------

echo -e "${BOLD}[2/3] Building Electron renderer (Vite)...${NC}"

cd mini_agent_electron

# Install node deps if needed
if [ ! -d "node_modules" ]; then
    echo "  Installing npm dependencies..."
    npm install --silent
fi

# Install electron-builder if not present
if [ ! -d "node_modules/electron-builder" ]; then
    echo "  Installing electron-builder..."
    npm install --save-dev electron-builder --silent
fi

# Build renderer
npx vite build --log-level warn
echo -e "  ${GREEN}✓${NC} Renderer built → mini_agent_electron/renderer/dist/"

echo ""

# ----------------------------------------------------------------------
# Stage 3: electron-builder — package app
# ----------------------------------------------------------------------

echo -e "${BOLD}[3/3] Packaging Electron app (electron-builder)...${NC}"

if [ -n "$PLATFORM" ]; then
    case "$PLATFORM" in
        --mac)    PLATFORM_FLAG="--mac" ;;
        --win)    PLATFORM_FLAG="--win" ;;
        --linux)  PLATFORM_FLAG="--linux" ;;
        *)
            echo -e "  ${RED}✗${NC} Unknown platform: $PLATFORM"
            echo "  Use: --mac, --win, or --linux (or omit for current platform)"
            exit 1
            ;;
    esac
else
    PLATFORM_FLAG=""
fi

npx electron-builder $PLATFORM_FLAG

echo ""
echo -e "${GREEN}${BOLD}╔══════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}${BOLD}║     Build complete! 🚀                       ║${NC}"
echo -e "${GREEN}${BOLD}╚══════════════════════════════════════════════╝${NC}"
echo ""
echo "Distributable(s) in: mini_agent_electron/dist-electron/"
ls -lh mini_agent_electron/dist-electron/*.{dmg,exe,AppImage} 2>/dev/null || \
  ls -lh mini_agent_electron/dist-electron/ 2>/dev/null || true
echo ""
