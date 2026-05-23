# mini_agent Electron

Electron desktop GUI for [mini_agent](../) — same Catppuccin Mocha theme, same layout.

## Layout

```
┌─ Header: "mini_agent — {model}" ──────────────────────────┐
│────────────────────────────────────────────────────────────│
│ ╭─ Tools & Thinking (40%) ─╮ │ ╭─ Chat (60%) ──────────╮ │
│ │ tools log                 │ │ │ chat messages          │ │
│ │───────────────────────────│ │ │                        │ │
│ │ thinking (max 10 lines)   │ │ │                        │ │
│ │───────────────────────────│ │ │                        │ │
│ │ Sub-agents                │ │ │                        │ │
│ │ subagents (max 10 lines)  │ │ │                        │ │
│ ╰───────────────────────────╯ │ ╰────────────────────────╯ │
│ ╭─ Input ───────────────────────────────────────────────╮ │
│ │ > _                                                   │ │
│ ╰───────────────────────────────────────────────────────╯ │
│────────────────────────────────────────────────────────────│
│ ⎇ main*  ●  ↻ turn 1  ⊙ 1.2k tok                         │
└────────────────────────────────────────────────────────────┘
```

## Architecture

```
┌────────────────────┐     IPC      ┌──────────────┐     JSON-lines     ┌─────────────────┐
│  Renderer Process  │◄────────────►│  Main Process │◄─────────────────►│  Python Backend │
│  (HTML/CSS/JS)     │ contextBridge│  (Electron)   │  stdin/stdout      │  (server.py)    │
└────────────────────┘              └──────────────┘                    └─────────────────┘
```

- **Renderer**: Vanilla HTML/CSS/JS — no framework. Uses CSS Grid/Flexbox with Catppuccin Mocha colours.
- **Main**: Electron main process spawns Python backend as a child process.
- **Backend**: Reuses all existing mini_agent Python code (`llm.py`, `config.py`, etc.) via JSON-lines protocol over stdin/stdout.

## Quick Start

```bash
# From this directory:
npm install
npm start

# With a specific workspace:
npm start -- --workspace=/path/to/project

# Dev mode (opens DevTools):
npm run dev
```

## Files

| File | Purpose |
|------|---------|
| `package.json` | Electron dependencies + scripts |
| `main.js` | Electron main process, spawns Python, bridges IPC |
| `preload.js` | Context bridge exposing `window.miniAgent` API |
| `renderer/index.html` | Layout — header, two-pane body, input, status bar |
| `renderer/style.css` | Catppuccin Mocha theme, rounded frames, log areas |
| `renderer/app.js` | Streaming display, input handling, slash commands |
| `backend/server.py` | JSON-lines server reusing existing agent code |

## Theme

Catppuccin Mocha — same palette as the prompt-toolkit TUI.

| Role | Hex |
|------|-----|
| Background | `#1e1e2e` |
| Surface | `#313244` |
| Border | `#45475a` |
| Accent (user msgs) | `#89b4fa` |
| Text | `#cdd6f4` |
| Dim | `#6c7086` |
| Thinking | `#585b70` |
| Red (errors) | `#f38ba8` |
| Green (OK) | `#a6e3a1` |
| Yellow | `#f9e2af` |

## Keybindings

| Key | Action |
|-----|--------|
| Enter | Submit message |
| Ctrl+C / Ctrl+Q | Cancel turn / Quit |

## Slash Commands

Same as the TUI: `/clear`, `/help`, `/stats`, `/session`, `/export`, `/init`, `/workspace`, `/theme`
