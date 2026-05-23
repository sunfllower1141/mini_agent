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
    const label = data.parallel
      ? `⚡ ${data.summary}`
      : `🔧 ${data.summary}`;
    appendLine(toolsLog, label, 'dim');
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
        appendLine(toolsLog, `    ${line}`, 'dim');
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
  appendLine(chatLog, '── You', 'msg-user');
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
