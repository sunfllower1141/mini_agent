# mini_agent — Windows 11 Installation Guide

This guide covers a clean install of mini_agent on **Windows 11** (also works for Windows 10, 22H2+).

## Quick Install (Automated)

If you're comfortable with the command line and have the prerequisites installed:

```bat
git clone https://github.com/YOUR_USERNAME/mini_agent.git
cd mini_agent
setup.bat
```

The script checks prerequisites, creates a virtual environment, installs all dependencies, and builds the Electron renderer. See [Manual Setup](#manual-setup) below if you prefer step-by-step.

---

## Prerequisites

| Tool | Required | Version | Install |
|------|----------|---------|---------|
| **Python** | ✅ Required | 3.10 – 3.13 | [python.org](https://www.python.org/downloads/) |
| **Node.js** | ✅ Required | 22+ (LTS) | [nodejs.org](https://nodejs.org/) |
| **npm** | ✅ Required | 9+ | Bundled with Node.js |
| **ripgrep (rg)** | ⚠️ Recommended | any | `winget install BurntSushi.ripgrep.MSVC` |
| **git** | ⚠️ Recommended | any | `winget install Git.Git` |
| **Visual C++ Redist** | ⚠️ Recommended | 2015+ | [vc_redist.x64.exe](https://aka.ms/vs/17/release/vc_redist.x64.exe) |

---

### Python (IMPORTANT — read this!)

**Do NOT use the Microsoft Store Python.** It's sandboxed and causes permission issues. Install from [python.org](https://www.python.org/downloads/).

During installation:
1. ✅ Check **"Add Python to PATH"**
2. ✅ Click **"Disable path length limit"** (if shown)

After install, **disable the Store stub** so `python` always points to the real install:
- Open **Settings → Apps → Advanced app settings → App execution aliases**
- Turn **OFF** both `python.exe` and `python3.exe`

Verify:
```bat
python --version
```
Should show `Python 3.12.x` (NOT a Microsoft Store path). If it opens the Store, the alias is still enabled.

### Node.js

Install the **LTS** version from [nodejs.org](https://nodejs.org/). Electron 42 requires Node ≥ 22.

Verify:
```bat
node --version   # should be v22.x or v24.x
npm --version    # should be 10.x
```

### ripgrep (recommended)

```bat
winget install BurntSushi.ripgrep.MSVC
```

Without ripgrep, file search falls back to slower methods — it won't block anything, just run slower.

### Git (recommended)

```bat
winget install Git.Git
```

Without git, some tools won't work (git skill, branch detection).

### Visual C++ Redistributable (recommended)

Some Python packages (sentence-transformers, PyTorch) may need this:
- Download: [vc_redist.x64.exe](https://aka.ms/vs/17/release/vc_redist.x64.exe)
- Install and reboot if prompted.

---

## Manual Setup (Step by Step)

### 1. Clone the repository

```bat
git clone https://github.com/YOUR_USERNAME/mini_agent.git
cd mini_agent
```

> **Path length warning:** Windows has a 260-character path limit by default. If you see errors about long paths, move the repo closer to the drive root (e.g., `C:\mini_agent\`) or enable long paths in Group Policy.

### 2. Create a Python virtual environment

```bat
python -m venv venv
venv\Scripts\activate
```

You should see `(venv)` in your prompt.

### 3. Install Python dependencies

```bat
pip install --upgrade pip
pip install -r requirements.txt
```

This installs ~25 packages including:
- **sentence-transformers** (pulls PyTorch ~2GB — may take a few minutes on first install)
- **playwright** (headless browser driver)
- **python-lsp-server** (Python language server)
- **pytest** + **pytest-timeout** (test runner)

> **Metered connection?** Install PyTorch CPU-only first to save bandwidth:
> ```bat
> pip install torch --index-url https://download.pytorch.org/whl/cpu
> pip install -r requirements.txt
> ```

> **Install fails?** Some packages need Visual C++ Build Tools:
> Download from https://visualstudio.microsoft.com/visual-cpp-build-tools/
> Select "Desktop development with C++" during install.

### 4. Install Playwright browser

```bat
python -m playwright install chromium
```

This downloads Chromium (~150MB). Required for browser automation tools (`open_url`, `browser_snapshot`, etc.).

### 5. Install Node.js dependencies

```bat
cd mini_agent_electron
npm install
```

This downloads Electron (~100MB) and all renderer packages. It may take a few minutes on first run.

> **Electron download fails?** Try a mirror:
> ```bat
> set ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/
> npm install
> ```

### 6. Build the renderer

```bat
npm run build
```

Builds the React frontend to `mini_agent_electron\renderer\dist\`.

### 7. Configure API keys

Create a `.env` file in the repo root (or set environment variables):

```env
# Required — at least one:
DEEPSEEK_API_KEY=sk-your-key-here
# CLAUDE_API_KEY=sk-ant-...
# XAI_API_KEY=xai-...

# Optional:
OPENAI_API_KEY=sk-...   # for GPT-4o vision
EXA_API_KEY=...          # for web search
```

Get keys from:
- **DeepSeek**: https://platform.deepseek.com/api_keys
- **Claude**: https://console.anthropic.com/
- **xAI/Grok**: https://x.ai/api

The Electron app also has an in-app settings panel where you can paste keys (persisted to `~/.mini_agent_env`).

---

## Launch

```bat
cd mini_agent_electron
npm start
```

On first launch:
- **Windows Defender Firewall** may prompt you to allow Node.js network access. Click **"Allow"** — the app needs this to communicate with the AI provider's API.
- The app auto-builds the renderer if `npm run build` wasn't run.

### Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `Enter` | Submit message |
| `Shift+Enter` | New line |
| `Escape` | Cancel streaming response |
| `Ctrl+L` | Clear chat |

---

## Running Tests

```bat
cd mini_agent
venv\Scripts\activate
python -m pytest
```

This runs 1,000+ tests. Add `-q` for quieter output, or `-v` for verbose.

> **Tests hanging?** Add `--timeout=60` (pytest-timeout). The `setup.bat` already installs `pytest-timeout`.

---

## Troubleshooting

### "python" opens the Microsoft Store

Disable App Execution Aliases:
- Settings → Apps → Advanced app settings → App execution aliases
- Turn OFF `python.exe` and `python3.exe`

### "The process cannot access the file because it is being used by another process"

Windows Defender real-time scanning is blocking file reads. Add exclusion:
- Settings → Privacy & Security → Virus & threat protection
- Manage settings → Exclusions → Add folder → `C:\path\to\mini_agent`

### Electron window shows a white screen

The renderer didn't build. Run:
```bat
cd mini_agent_electron
npm run build
npm start
```

### npm install fails with network errors

1. **Corporate proxy:**
   ```bat
   npm config set proxy http://proxy.company.com:8080
   npm config set https-proxy http://proxy.company.com:8080
   ```

2. **Electron binary download fails:**
   ```bat
   set ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/
   rmdir /s /q node_modules
   npm install
   ```

3. **Clear npm cache:**
   ```bat
   npm cache clean --force
   npm install
   ```

### pip install fails with "Microsoft Visual C++ 14.0 is required"

Install Visual C++ Build Tools:
- https://visualstudio.microsoft.com/visual-cpp-build-tools/
- Select "Desktop development with C++"
- Restart terminal and retry

### "SyntaxError: invalid syntax" when running python -c commands

Use double quotes instead of single quotes on Windows:
```bat
# Wrong:
python -c 'print("hello")'

# Right:
python -c "print('hello')"
```

### Folder path too long errors

Move the repo closer to the drive root:
```bat
move C:\Users\Name\very\long\path\mini_agent C:\mini_agent
cd C:\mini_agent
```

Or enable long paths (requires admin):
- Group Policy → Computer Configuration → Administrative Templates → System → Filesystem
- Enable "Enable Win32 long paths"

---

## Uninstall

```bat
# Deactivate virtual environment
deactivate

# Delete the repo folder
rmdir /s /q C:\path\to\mini_agent

# Optional: remove Python packages (if no other projects use them)
pip uninstall -y -r requirements.txt

# Optional: remove global tools
winget uninstall BurntSushi.ripgrep.MSVC
```

---

## Support

- **Issues**: https://github.com/GabrielMalone/mini_agent/issues
- **Original repo**: https://github.com/GabrielMalone/mini_agent
