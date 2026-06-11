# mini_agent — Windows Setup

A terminal coding agent with 76+ tools. Powered by DeepSeek, Claude, or xAI/Grok.
Multi-agent orchestration, SQLite memory, headless browser, desktop automation, and an Electron desktop app.

## Prerequisites

Install these before running setup:

| Tool | Version | Download |
|------|---------|----------|
| **Python** | 3.10–3.13 | [python.org](https://www.python.org/downloads/) — check "Add Python to PATH" |
| **Node.js** | 22+ (LTS) | [nodejs.org](https://nodejs.org/) |

**After installing Python**, disable the Microsoft Store stub so `python` works correctly:
- Open **Settings → Apps → Advanced app settings → App execution aliases**
- Turn **OFF** both `python.exe` and `python3.exe`

Optional but recommended:
```bat
winget install BurntSushi.ripgrep.MSVC     # faster file search
winget install Git.Git                       # git tools
```

## One-Shot Setup

```bat
git clone https://github.com/YOUR_USERNAME/mini_agent.git
cd mini_agent
setup.bat
```

> `setup.bat` checks prerequisites, creates a Python virtual environment, installs all dependencies (Python + Node.js + Playwright), and builds the Electron renderer. Takes 5–10 minutes on first run.

## Launch

```bat
cd mini_agent_electron
npm start
```

> On first launch, Windows Defender Firewall may ask to allow Node.js network access — click **Allow**.

### Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `Enter` | Submit message |
| `Shift+Enter` | New line |
| `Escape` | Cancel streaming response |

## API Keys

Create a `.env` file in the repo root with at least one key:

```env
DEEPSEEK_API_KEY=sk-your-key-here
```

Get a key from [platform.deepseek.com/api_keys](https://platform.deepseek.com/api_keys).

You can also enter keys in the app's settings panel on first launch (persisted to `~/.mini_agent_env`).

## Troubleshooting

Full guide: [`WINDOWS_INSTALL.md`](WINDOWS_INSTALL.md)

| Problem | Fix |
|---------|-----|
| `python` opens Microsoft Store | Disable app execution aliases (see Prerequisites above) |
| White screen on launch | `cd mini_agent_electron && npm run build && npm start` |
| Defender blocks file reads | Add repo folder to Defender exclusions |
| `npm install` network errors | `set ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/` then retry |
