---
name: web
description: Web browsing and fetching -- search the web, fetch URLs, browser automation.
version: "1.0"
author: mini_agent
category: software-development
tools:
  - web_search
  - fetch_url
  - open_url
  - browser_navigate
  - browser_snapshot
  - browser_click
  - browser_type
  - browser_screenshot
---

# Web Skill

Web access for research, documentation, and browser automation. Use for:

- **web_search** -- search the web with Exa; returns titles, URLs, and excerpts
- **fetch_url** -- fetch and parse content from a specific URL
- **open_url** -- open a URL in Playwright browser
- **browser_navigate** -- navigate within an open browser session
- **browser_snapshot** -- capture accessibility tree + interactive elements
- **browser_click** -- click an interactive element by label or selector
- **browser_type** -- type text into a form field
- **browser_screenshot** -- capture a visible screenshot

## Knowledge Confidence Scale
The agent uses a 1-10 confidence scale for external knowledge:
- **1-3**: Guessing -- DON'T answer, use `web_search` FIRST
- **4-6**: Uncertain -- strongly prefer `web_search` before answering
- **7-8**: Fairly sure -- verify if consequences are high
- **9-10**: Know it well -- answer directly

## Best Practices
- Search BEFORE answering questions about APIs, libraries, or frameworks
- Use specific technical terms in search queries for best results
- When stuck after 2+ turns of failed codebase search, switch to `web_search`
- Prefer `web_search` over `fetch_url` unless you need a specific page's content
