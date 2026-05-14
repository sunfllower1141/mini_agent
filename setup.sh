#!/usr/bin/env bash
# setup.sh — portable setup for mini_agent
# Run: bash setup.sh
set -euo pipefail

cd "$(dirname "$0")"

echo "=== mini_agent setup ==="

# 1. Create venv if missing
if [ ! -d "venv" ]; then
    echo "[1/3] Creating virtual environment..."
    python3 -m venv venv
else
    echo "[1/3] venv already exists, skipping"
fi

# 2. Activate and install
echo "[2/3] Installing dependencies..."
source venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q

# 3. Done
echo "[3/3] Done!"
echo ""
echo "To start:"
echo "  source venv/bin/activate"
echo "  python mini_agent.py            # terminal REPL"
echo "  python tui.py                   # TUI (Textual)"
echo ""
echo "Optional flags:"
echo "  --unrestricted   allow read/write outside workspace"
echo "  --stream         stream responses token-by-token"
echo "  --approve        ask before write/destructive ops"
