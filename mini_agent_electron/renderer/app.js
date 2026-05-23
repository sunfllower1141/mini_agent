/**
 * app.js — Renderer logic for mini_agent Electron.
 *
 * Manages the four log areas (tools, thinking, sub-agents, chat),
 * handles user input /slash commands, and listens for streaming events
 * from the main process via the miniAgent context-bridge API.
 */

// ---------------------------------------------------------------------------
// DOM references
// ---------------------------------------------------------------------------

const $ = (sel) => document.querySelector(sel);

// ---------------------------------------------------------------------------
// Lucide SVG icons (MIT) — minimal inline line-art
// ---------------------------------------------------------------------------

const ICON_TOOL = `<svg class="tool-icon" xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>`;

const ICON_PARALLEL = `<svg class="tool-icon" xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 0-1.83Z"/><path d="M2.6 12.08l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.91"/><path d="M2.6 18.08l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.91"/></svg>`;

// ---------------------------------------------------------------------------

const toolsLog      = $('#tools-log');
const thinkingLog   = $('#thinking-log');
const subagentsLog  = $('#subagents-log');
const chatLog       = $('#chat-log');
const userInput     = $('#user-input');
const headerModel   = $('#header-model');
const gitStatus     = $('#git-status');
const liveIndicator = $('#live-indicator');
const turnCounter   = $('#turn-counter');
const turnCount     = $('#turn-count');
const tokenCounter  = $('#token-counter');
const tokenCount    = $('#token-count');
const workspaceInfo = $('#workspace-info');
const restoredInfo  = $('#restored-info');
const headerSession = $('#header-session');

// ---------------------------------------------------------------------------
// Auto-scroll helpers
// ---------------------------------------------------------------------------

function scrollToBottom(el) {
  el.scrollTop = el.scrollHeight;
}

// ---------------------------------------------------------------------------
// Append helpers — each appends a line (with optional CSS class) + auto-scroll
// ---------------------------------------------------------------------------

function appendLine(el, text, cssClass) {
  if (!text && text !== '') return;
  const div = document.createElement('div');
  div.textContent = text;
  if (cssClass) div.className = cssClass;
  el.appendChild(div);
  scrollToBottom(el);
}

function appendLastLine(el, text, cssClass) {
  // Append to last child if same class (for streaming tokens)
  const last = el.lastElementChild;
  if (last && last.className === (cssClass || '') && last.textContent !== null) {
    last.textContent += text;
  } else {
    appendLine(el, text, cssClass);
  }
  scrollToBottom(el);
}

function appendIconLine(el, iconSVG, text, cssClass) {
  if (!text && text !== '') return;
  const div = document.createElement('div');
  div.innerHTML = `${iconSVG} ${text}`;
  if (cssClass) div.className = cssClass;
  el.appendChild(div);
  scrollToBottom(el);
}

// ---------------------------------------------------------------------------
// Syntax highlighting — lightweight regex-based colorizer
// ---------------------------------------------------------------------------

const SYN_PATTERNS = [
  // Comments (must come before strings to avoid matching inside)
  { re: /#[^\n]*/g,              cls: 'syn-comment' },
  { re: /\/\/[^\n]*/g,           cls: 'syn-comment' },
  // Strings
  { re: /"[^"]*"/g,              cls: 'syn-string' },
  { re: /'[^']*'/g,              cls: 'syn-string' },
  { re: /`[^`]*`/g,              cls: 'syn-string' },
  // Decorators
  { re: /@\w+/g,                 cls: 'syn-decorator' },
  // Python/JS keywords
  { re: /\b(def|class|return|import|from|if|else|elif|try|except|finally|with|as|for|while|in|not|and|or|is|lambda|yield|raise|pass|break|continue|async|await|function|const|let|var|export|default|new|throw|catch|typeof|instanceof)\b/g, cls: 'syn-keyword' },
  // Booleans / nil
  { re: /\b(True|False|None|true|false|null|undefined|NaN|Infinity)\b/g, cls: 'syn-boolean' },
  // Numbers
  { re: /\b\d+\.?\d*\b/g,        cls: 'syn-number' },
  // File paths
  { re: /(?:^|\s)([~/][^\s,:;]*\/[^\s,:;]+)/g, cls: 'syn-path' },
];

function highlightSyntax(text) {
  // Escape HTML first
  let html = text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');

  // Apply each pattern — use placeholder tokens to avoid overlap
  const tokens = [];
  SYN_PATTERNS.forEach(({ re, cls }) => {
    html = html.replace(re, (match) => {
      const idx = tokens.length;
      tokens.push(`<span class="${cls}">${match}</span>`);
      return `\x00${idx}\x00`;
    });
  });

  // Restore tokens (unescape the null bytes we used as placeholders)
  html = html.replace(/\x00(\d+)\x00/g, (_, i) => tokens[+i]);

  return html;
}

function appendHighlighted(el, text, cssClass) {
  if (!text && text !== '') return;
  const div = document.createElement('div');
  div.innerHTML = highlightSyntax(text);
  if (cssClass) div.className = cssClass;
  el.appendChild(div);
  scrollToBottom(el);
}

function highlightElement(el) {
  // Convert all child divs from textContent to highlighted innerHTML
  for (const child of el.children) {
    if (child.textContent && !child.querySelector('span')) {
      child.innerHTML = highlightSyntax(child.textContent);
    }
  }
}

// ---------------------------------------------------------------------------
// Streaming state
// ---------------------------------------------------------------------------

let inThinking = false;
let needsChatNewline = false;
let currentToolCount = 0;
const _shownStatus = {};  // dedupe startup status lines

// ---------------------------------------------------------------------------
// Backend event handlers
// ---------------------------------------------------------------------------

function setupListeners() {
  window.miniAgent.on('backend:status', (data) => {
    // Header — update model name from any message that carries it
    if (data.model) {
      headerModel.textContent = data.model;
    }
    // Session name → header center
    if (data.session_name) {
      headerSession.textContent = data.session_name;
    }
    // Startup line — only once
    if (data.ready && !_shownStatus.ready) {
      appendLine(toolsLog, 'backend ready', 'dim');
      _shownStatus.ready = true;
    }
    // Workspace → footer right
    if (data.workspace) {
      workspaceInfo.textContent = data.workspace;
    }
    // Git branch → footer left
    if (data.git_branch) {
      const dirty = data.git_dirty ? '*' : '';
      gitStatus.textContent = `⎇ ${data.git_branch}${dirty}`;
    }
    // Restored count → footer right
    if (data.restored_count) {
      restoredInfo.textContent = `restored ${data.restored_count} msgs`;
      restoredInfo.classList.remove('hidden');
    }
  });

  window.miniAgent.on('stream:token', (data) => {
    if (inThinking) {
      appendLastLine(thinkingLog, data.text, 'msg-thinking');
    } else {
      if (needsChatNewline) {
        appendLine(chatLog, '');
        needsChatNewline = false;
      }
      appendLastLine(chatLog, data.text, 'msg-agent');
    }
  });

  window.miniAgent.on('stream:thinking_start', () => {
    inThinking = true;
  });

  window.miniAgent.on('stream:thinking_end', () => {
    inThinking = false;
    needsChatNewline = true;
  });

  window.miniAgent.on('stream:tool_start', (data) => {
    currentToolCount++;
    const icon = data.parallel ? ICON_PARALLEL : ICON_TOOL;
    appendIconLine(toolsLog, icon, data.summary, 'dim');
  });

  window.miniAgent.on('stream:tool_end', (data) => {
    const status = data.ok ? 'OK' : 'ERR';
    const cssClass = data.ok ? 'msg-tool-ok' : 'msg-tool-err';
    appendLine(toolsLog, `  ${status} ${data.detail}`, cssClass);
  });

  window.miniAgent.on('stream:tool_output', (data) => {
    const lines = data.line.split('\n');
    for (const line of lines) {
      if (line.trim()) {
        appendHighlighted(toolsLog, `    ${line}`, 'dim');
      }
    }
  });

  window.miniAgent.on('stream:turn_complete', (data) => {
    clearTimeout(submitTimeout);
    if (data.usage) {
      const tok = data.usage.total_tokens || 0;
      if (tok) {
        tokenCounter.classList.remove('hidden');
        const tokStr = tok >= 1000 ? `${(tok / 1000).toFixed(1)}k` : String(tok);
        tokenCount.textContent = tokStr;
      }
    }
    if (data.turn_count) {
      turnCounter.classList.remove('hidden');
      turnCount.textContent = data.turn_count;
    }
    liveIndicator.classList.add('hidden');
    userInput.disabled = false;
    userInput.focus();
  });

  window.miniAgent.on('stream:error', (data) => {
    clearTimeout(submitTimeout);
    appendLine(chatLog, `Error: ${data.message}`, 'msg-error');
    liveIndicator.classList.add('hidden');
    userInput.disabled = false;
    userInput.focus();
  });

  // Sub-agent events come through the same channels
  window.miniAgent.on('backend:response', (data) => {
    if (data.lines) {
      for (const line of data.lines) {
        appendLine(toolsLog, line, 'dim');
      }
    }
    if (data.target === 'chat' && data.lines) {
      for (const line of data.lines) {
        appendLine(chatLog, line, 'msg-status');
      }
    }
  });
}

let submitTimeout = null;

// ---------------------------------------------------------------------------
// Input handling
// ---------------------------------------------------------------------------

function handleSubmit(text) {
  if (!text) return;

  // Slash commands
  if (text.startsWith('/')) {
    handleCommand(text);
    return;
  }

  // Show user message in chat
  appendLine(chatLog, text, 'msg-user');
  appendLine(chatLog, '', '');

  // Show live indicator
  liveIndicator.classList.remove('hidden');
  userInput.disabled = true;

  // Safety: re-enable input after 120s even if backend never responds
  clearTimeout(submitTimeout);
  submitTimeout = setTimeout(() => {
    if (userInput.disabled) {
      liveIndicator.classList.add('hidden');
      userInput.disabled = false;
      userInput.focus();
      appendLine(toolsLog, 'Timed out waiting for backend response', 'red');
    }
  }, 120_000);

  // Send to backend
  window.miniAgent.submit(text);
}

function handleCommand(cmd) {
  const lower = cmd.toLowerCase().trim();

  // Local commands (don't need backend)
  if (lower === '/clear') {
    // Clear all logs
    toolsLog.innerHTML = '';
    thinkingLog.innerHTML = '';
    subagentsLog.innerHTML = '';
    chatLog.innerHTML = '';
    window.miniAgent.command('/clear');
    return;
  }

  if (lower === '/help') {
    const lines = [
      '/clear     Reset conversation memory',
      '/export    Write conversation to markdown',
      '/help      Show this help',
      '/stats     Show session stats',
      '/session   new | switch | delete | list',
      '/theme     Show theme info',
      '/workspace Switch workspace',
    ];
    for (const line of lines) {
      appendLine(toolsLog, line, 'dim');
    }
    return;
  }

  if (lower === '/theme') {
    appendLine(toolsLog, 'Theme: Catppuccin Mocha', 'dim');
    return;
  }

  // Send other commands to backend
  appendLine(toolsLog, `> ${cmd}`, 'dim');
  window.miniAgent.command(cmd);
}

// ---------------------------------------------------------------------------
// Keyboard shortcuts
// ---------------------------------------------------------------------------

userInput.addEventListener('keydown', (e) => {
  // Enter to submit
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    const text = userInput.value.trim();
    userInput.value = '';
    if (text) handleSubmit(text);
  }
});

// Global Escape key — cancel current turn (works even when input is disabled)
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && userInput.disabled) {
    e.preventDefault();
    doCancel();
  }
});

// Click on live indicator to cancel
liveIndicator.addEventListener('click', () => {
  if (userInput.disabled) {
    doCancel();
  }
});

function doCancel() {
  clearTimeout(submitTimeout);
  window.miniAgent.cancel();
  appendLine(toolsLog, '--- cancelled ---', 'red');
  liveIndicator.classList.add('hidden');
  userInput.disabled = false;
  userInput.focus();
}

// ---------------------------------------------------------------------------
// Startup
// ---------------------------------------------------------------------------

function init() {
  setupListeners();
  appendLine(toolsLog, 'mini_agent — starting...', 'dim');
  userInput.focus();

  // Request initial status
  window.miniAgent.getStatus();

  // Keep focus on input when clicking anywhere in the app
  document.addEventListener('click', (e) => {
    // Don't steal focus from actual inputs
    if (e.target.tagName !== 'INPUT' && e.target.tagName !== 'TEXTAREA') {
      userInput.focus();
    }
  });
}

// Wait for DOM
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
