#!/bin/bash
# launch_electron.sh — Start the mini_agent Electron UI
# Handles nvm, correct cwd, and GPU flags automatically.
set -e

cd "$(dirname "$0")"

export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
nvm use 22 >/dev/null 2>&1

echo "Node $(node --version) | Electron $(npx electron --version 2>/dev/null || echo '...')"
echo "Starting mini_agent UI..."
npm start
