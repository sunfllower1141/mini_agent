#!/usr/bin/env bash
# setup.sh — full bootstrap for mini_agent (Electron desktop app)
# Run: bash setup.sh
#
# This script:
#   1. Checks for required system tools (Node.js, Python, ripgrep)
#   2. Creates a Python virtual environment and installs dependencies
#   3. Installs Node.js packages and builds the Electron renderer
#   4. Gets you ready to launch with a single command
set -euo pipefail

cd "$(dirname "$0")"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
NC='\033[0m' # No Color

echo ""
echo -e "${BOLD}╔══════════════════════════════════╗${NC}"
echo -e "${BOLD}║     mini_agent — setup           ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════╝${NC}"
echo ""

# ------------------------------------------------------------------    
# 0. Prerequisite checks
# ------------------------------------------------------------------    

ERRORS=0

echo -e "${BOLD}[0/5] Checking prerequisites...${NC}"

# Python 3
if command -v python3 &> /dev/null; then
    PY_VER=$(python3 --version 2>&1 | awk '{print $2}')
    echo -e "  ${GREEN}✓${NC} Python 3  (${PY_VER})"
else
    echo -e "  ${RED}✗${NC} Python 3 not found. Install from https://www.python.org/downloads/"
    ERRORS=$((ERRORS + 1))
fi

# Node.js — check common paths before giving up (nvm, homebrew, etc.)
NODE_FOUND=false
for candidate in "/usr/local/bin/node" "/opt/homebrew/bin/node"; do
    if [ -x "$candidate" ]; then
        NODE_VER=$("$candidate" --version 2>/dev/null)
        echo -e "  ${GREEN}✓${NC} Node.js   (${NODE_VER}) [${candidate}]"
        NODE_FOUND=true
        break
    fi
done
# also check nvm
if [ "$NODE_FOUND" = false ] && [ -s "$HOME/.nvm/nvm.sh" ]; then
    NODE_VER=$(bash -c "source $HOME/.nvm/nvm.sh 2>/dev/null && node --version" 2>/dev/null)
    if [ -n "$NODE_VER" ]; then
        echo -e "  ${GREEN}✓${NC} Node.js   (${NODE_VER}) [nvm]"
        NODE_FOUND=true
    fi
fi
# fallback: PATH
if [ "$NODE_FOUND" = false ] && command -v node &>/dev/null; then
    NODE_VER=$(node --version 2>/dev/null)
    echo -e "  ${GREEN}✓${NC} Node.js   (${NODE_VER}) [PATH]"
    NODE_FOUND=true
fi
if [ "$NODE_FOUND" = false ]; then
    echo -e "  ${RED}✗${NC} Node.js not found."
    echo "     Install: https://nodejs.org (v18+ LTS) or: brew install node"
    echo "     If using nvm, run: source ~/.nvm/nvm.sh  first"
    ERRORS=$((ERRORS + 1))
fi

# npm
if "$NODE_FOUND" && command -v npm &>/dev/null; then
    NPM_VER=$(npm --version 2>/dev/null)
    echo -e "  ${GREEN}✓${NC} npm       (v${NPM_VER})"
elif command -v npm &>/dev/null; then
    NPM_VER=$(npm --version 2>/dev/null)
    echo -e "  ${GREEN}✓${NC} npm       (v${NPM_VER}) [PATH]"
else
    echo -e "  ${RED}✗${NC} npm not found (bundled with Node.js)"
    ERRORS=$((ERRORS + 1))
fi

# ripgrep (strongly recommended)
if command -v rg &> /dev/null; then
    RG_VER=$(rg --version | head -1)
    echo -e "  ${GREEN}✓${NC} ripgrep   (${RG_VER})"
else
    echo -e "  ${YELLOW}⚠${NC} ripgrep (rg) not found. Install: ${BOLD}brew install ripgrep${NC} (macOS) or ${BOLD}apt install ripgrep${NC} (Linux)"
    echo "         Without it, file search will fall back to slower methods."
fi

if [ $ERRORS -gt 0 ]; then
    echo ""
    echo -e "${RED}Missing ${ERRORS} required tool(s). Please install them and re-run setup.${NC}"
    exit 1
fi

echo ""

# ------------------------------------------------------------------    
# 1. Python virtual environment
# ------------------------------------------------------------------    

echo -e "${BOLD}[1/5] Python virtual environment...${NC}"
if [ ! -d "venv" ]; then
    python3 -m venv venv
    echo -e "  ${GREEN}✓${NC} Created venv/"
else
    echo -e "  ${GREEN}✓${NC} venv/ already exists, skipping"
fi

# ------------------------------------------------------------------    
# 2. Python dependencies
# ------------------------------------------------------------------    

echo -e "${BOLD}[2/5] Python dependencies...${NC}"
source venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q
echo -e "  ${GREEN}✓${NC} Installed Python packages"

# ------------------------------------------------------------------    
# 3. Node.js dependencies
# ------------------------------------------------------------------    

echo -e "${BOLD}[3/5] Node.js dependencies...${NC}"
cd mini_agent_electron
npm install --silent
echo -e "  ${GREEN}✓${NC} Installed npm packages"

# ------------------------------------------------------------------    
# 4. Build Electron renderer
# ------------------------------------------------------------------    

echo -e "${BOLD}[4/5] Building renderer...${NC}"
npm run build --silent
echo -e "  ${GREEN}✓${NC} Renderer built → mini_agent_electron/renderer/dist/"
cd ..

# ------------------------------------------------------------------    
# 5. API key check
# ------------------------------------------------------------------    

echo -e "${BOLD}[5/5] API key check...${NC}"
KEY_FOUND=false
for VAR in DEEPSEEK_API_KEY CLAUDE_API_KEY XAI_API_KEY OLLAMA_API_KEY; do
    if [ -n "${!VAR:-}" ]; then
        echo -e "  ${GREEN}✓${NC} ${VAR} is set"
        KEY_FOUND=true
    fi
done

if [ "$KEY_FOUND" = false ]; then
    # Check ~/.mini_agent_env
    if [ -f "$HOME/.mini_agent_env" ] && grep -qE '^(DEEPSEEK|CLAUDE|XAI|OLLAMA)_API_KEY=' "$HOME/.mini_agent_env" 2>/dev/null; then
        echo -e "  ${GREEN}✓${NC} API key found in ~/.mini_agent_env"
    else
        echo ""
        echo -e "  ${YELLOW}⚠${NC} No API key detected."
        echo ""
        echo "  The app will show a settings panel on first launch where you can"
        echo "  enter your key. Supported providers: DeepSeek, Claude, xAI, Ollama."
        echo ""
        echo "  Alternatively, set one now:"
        echo "    export DEEPSEEK_API_KEY=sk-..."
        echo ""
    fi
else
    echo ""
fi

# ------------------------------------------------------------------    
# Done
# ------------------------------------------------------------------    

echo -e "${GREEN}${BOLD}╔══════════════════════════════════╗${NC}"
echo -e "${GREEN}${BOLD}║     Setup complete! 🚀           ║${NC}"
echo -e "${GREEN}${BOLD}╚══════════════════════════════════╝${NC}"
echo ""
echo -e "To launch the desktop app:"
echo ""
echo -e "  ${BOLD}cd mini_agent_electron && npm start${NC}"
echo ""
echo "For development mode (hot-reload renderer + DevTools):"
echo ""
echo "  cd mini_agent_electron && npm run dev"
echo ""
