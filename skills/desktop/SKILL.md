---
name: desktop
description: Desktop automation -- snapshot, click, type, find elements, launch apps, clipboard.
version: "1.0"
author: mini_agent
category: software-development
tools:
  - desktop_snapshot
  - desktop_click
  - desktop_type
  - desktop_find
  - desktop_screenshot
  - desktop_apps
  - desktop_launch
  - desktop_quit
  - desktop_focus
  - desktop_clipboard
  - desktop_windows
  - desktop_system_info
  - desktop_key
  - desktop_open
  - desktop_reveal
  - desktop_notify
---

# Desktop Skill

Cross-platform desktop automation (macOS, Windows, Linux). Use for:

## UI Interaction
- **desktop_snapshot** -- capture accessibility tree of current app/window
- **desktop_click** -- click a UI element
- **desktop_type** -- type text into a focused field
- **desktop_find** -- find a UI element by label, role, or text

## App Management
- **desktop_apps** -- list running applications
- **desktop_launch** -- launch an application by name
- **desktop_quit** -- quit a running application
- **desktop_focus** -- bring an application to focus

## Utilities
- **desktop_clipboard** -- read/write clipboard content
- **desktop_windows** -- list open windows
- **desktop_system_info** -- get system information (OS, CPU, memory)
- **desktop_screenshot** -- capture screen or window screenshot
- **desktop_key** -- press keyboard shortcuts
- **desktop_open** -- open file/folder/URL with default handler
- **desktop_reveal** -- reveal file in file manager (Finder/Explorer)
- **desktop_notify** -- send a desktop notification

## Best Practices
- Use `desktop_snapshot` before interacting to understand the UI state
- Prefer accessibility-based interaction over coordinate-based
- On macOS, uses AppleScript + atomacos; on Windows, uses UI Automation
