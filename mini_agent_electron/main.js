/**
 * main.js — Electron main process for mini_agent.
 *
 * Spawns the Python backend as a child process and bridges messages
 * between the renderer (via IPC) and the Python process (via JSON-lines
 * on stdin/stdout).
 */
const { app, BrowserWindow, ipcMain, dialog } = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');

// ---------------------------------------------------------------------------
// Resource tuning — this is a text-based chat app, not a game or browser.
// Merge GPU into main process (saves ~180 MB separate GPU process).
app.commandLine.appendSwitch('in-process-gpu');
// Cap V8 heap: 128 MB old-space, 16 MB nursery (young gen).
app.commandLine.appendSwitch('js-flags', '--max-old-space-size=128 --max-semi-space-size=16');

// ---------------------------------------------------------------------------
// Load .env file — GUI apps on macOS don't inherit shell profile vars
// ---------------------------------------------------------------------------

function loadEnvFile(filePath) {
  if (!fs.existsSync(filePath)) return;
  const lines = fs.readFileSync(filePath, 'utf-8').split('\n');
  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) continue;
    const eqIdx = trimmed.indexOf('=');
    if (eqIdx === -1) continue;
    const key = trimmed.slice(0, eqIdx).trim();
    const value = trimmed.slice(eqIdx + 1).trim().replace(/^["']|["']$/g, '');
    if (key && !process.env[key]) {
      process.env[key] = value;
    }
  }
}

// Load .env from project root (mini_agent/) and ~/.mini_agent_env
loadEnvFile(path.join(__dirname, '..', '.env'));
loadEnvFile(path.join(require('os').homedir(), '.mini_agent_env'));

// ---------------------------------------------------------------------------
// API key detection
// ---------------------------------------------------------------------------

const PROVIDER_KEY_ENV = {
  deepseek: 'DEEPSEEK_API_KEY',
  claude: 'CLAUDE_API_KEY',
  xai: 'XAI_API_KEY',
  ollama: 'OLLAMA_API_KEY',
};

function detectApiKey() {
  // Check if any provider key is set in the environment
  for (const [provider, envName] of Object.entries(PROVIDER_KEY_ENV)) {
    if (process.env[envName]) {
      return { configured: true, provider, envName };
    }
  }
  return { configured: false, provider: null, envName: null };
}

function apiKeyEnvFile() {
  return path.join(require('os').homedir(), '.mini_agent_env');
}

function readEnvFile(filePath) {
  if (!fs.existsSync(filePath)) return {};
  const entries = {};
  const lines = fs.readFileSync(filePath, 'utf-8').split('\n');
  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) continue;
    const eqIdx = trimmed.indexOf('=');
    if (eqIdx === -1) continue;
    const key = trimmed.slice(0, eqIdx).trim();
    const value = trimmed.slice(eqIdx + 1).trim().replace(/^["']|["']$/g, '');
    if (key) entries[key] = value;
  }
  return entries;
}

function writeEnvFile(filePath, entries) {
  const lines = [];
  for (const [key, value] of Object.entries(entries)) {
    lines.push(`${key}=${value}`);
  }
  fs.writeFileSync(filePath, lines.join('\n') + '\n', 'utf-8');
}

// ---------------------------------------------------------------------------
// Python backend process
// ---------------------------------------------------------------------------

let pythonProcess = null;
let pythonReady = false;
let pendingRequests = [];
let lastStatus = null; // cached status for renderer to fetch on mount
let _shuttingDown = false;  // set during window-close to prevent restart loops

function spawnPythonBackend(workspacePath) {
  const backendScript = path.join(__dirname, 'backend', 'server.py');
  
  if (!fs.existsSync(backendScript)) {
    console.error(`Backend script not found: ${backendScript}`);
    return null;
  }

  const env = { ...process.env };
  if (workspacePath) {
    env.MINI_AGENT_WORKSPACE = workspacePath;
  }

  // Priority for Python backend:
  //   1. Bundled PyInstaller binary (packaged via electron-builder extraResources)
  //   2. Local venv python (dev mode)
  //   3. System python3/python (fallback)
  const isWindows = process.platform === 'win32';
  const isPackaged = app.isPackaged;

  const bundledName = isWindows ? 'mini_agent_backend.exe' : 'mini_agent_backend';
  const bundledPaths = isPackaged
    ? [path.join(process.resourcesPath, 'backend', bundledName)]
    : [path.join(__dirname, '..', 'pyinstaller_dist', bundledName)];

  const venvPython = isWindows
    ? path.join(__dirname, '..', 'venv', 'Scripts', 'python.exe')
    : path.join(__dirname, '..', 'venv', 'bin', 'python3');
  const fallback = isWindows ? 'python.exe' : 'python3';

  let pythonBin = null;
  let pythonArgs = [backendScript];

  // Try bundled binary first
  for (const bp of bundledPaths) {
    if (fs.existsSync(bp)) {
      pythonBin = bp;
      pythonArgs = [];  // bundled binary IS the script, no args needed
      break;
    }
  }

  // Fall back to venv or system python
  if (!pythonBin) {
    pythonBin = fs.existsSync(venvPython) ? venvPython : fallback;
    pythonArgs = [backendScript];
  }

  console.log(`Using Python: ${pythonBin}${pythonArgs.length ? ' ' + pythonArgs[0] : ' (bundled)'}`);
  console.log(`DEEPSEEK_API_KEY: ${env.DEEPSEEK_API_KEY ? 'set' : 'not set'}`);

  const proc = spawn(pythonBin, pythonArgs, {
    env,
    cwd: path.join(__dirname, '..'),
    stdio: ['pipe', 'pipe', 'pipe'],
  });

  // Buffer for incomplete JSON lines from stdout
  let stdoutBuffer = '';

  proc.stdout.on('data', (data) => {
    stdoutBuffer += data.toString();
    const lines = stdoutBuffer.split('\n');
    // Keep the last potentially incomplete line in the buffer
    stdoutBuffer = lines.pop() || '';
    
    for (const line of lines) {
      if (!line.trim()) continue;
      try {
        const msg = JSON.parse(line);
        handlePythonMessage(msg);
      } catch (e) {
        console.error('Failed to parse Python message:', line.slice(0, 200), e.message);
      }
    }
  });

  proc.stderr.on('data', (data) => {
    // Log Python stderr to Electron console only — not the tools panel.
    // HF warnings, tqdm bars, etc. are noise in the UI.
    const text = data.toString().trim();
    if (text) {
      process.stderr.write(`[python:stderr] ${data}`);
    }
  });

  proc.on('close', (code) => {
    console.log(`Python backend exited with code ${code}`);
    pythonReady = false;
    pythonProcess = null;
    // If we're in a shutdown sequence and the backend exited cleanly,
    // quit Electron regardless of platform (fixes macOS hang).
    if (_shuttingDown) {
      app.quit();
      return;
    }
    // Auto-restart on unexpected exit (not a clean shutdown)
    if (code !== 0 && code !== null) {
      const win = BrowserWindow.getAllWindows()[0];
      if (win) win.webContents.send('stream:error', { message: `Backend crashed (exit ${code}). Restarting...` });
      const restartWorkspace = workspacePath;
      setTimeout(() => {
        if (!pythonProcess) {
          pythonProcess = spawnPythonBackend(restartWorkspace);
          if (!pythonProcess) {
            const win = BrowserWindow.getAllWindows()[0];
            if (win) win.webContents.send('stream:error', { message: 'Backend server.py not found.' });
            console.error('Backend script not found — agent will not start.');
          }
        }
      }, 1500);
    }
  });

  proc.on('error', (err) => {
    console.error('Failed to start Python backend:', err.message);
    pythonReady = false;
    pythonProcess = null;
  });

  return proc;
}

// Lock to prevent concurrent stdin writes (flushPending + IPC handler race).
// Without this, two write() calls can interleave, producing a corrupt JSON
// line that the Python backend fails to parse with "Expecting value...".
let _stdinLock = false;
let _stdinQueue = [];

function _stdinWriteUnlocked(msg) {
  try {
    pythonProcess.stdin.write(JSON.stringify(msg) + '\n');
  } catch (e) {
    pendingRequests.push(msg);
  }
}

function sendToPython(msg) {
  if (!pythonProcess || !pythonProcess.stdin || !pythonProcess.stdin.writable) {
    pendingRequests.push(msg);
    return;
  }
  if (_stdinLock) {
    _stdinQueue.push(msg);
    return;
  }
  _stdinLock = true;
  _stdinWriteUnlocked(msg);
  _stdinLock = false;
  // Drain any queued messages
  while (_stdinQueue.length > 0) {
    _stdinLock = true;
    _stdinWriteUnlocked(_stdinQueue.shift());
    _stdinLock = false;
  }
}

function flushPending() {
  while (pendingRequests.length > 0) {
    const msg = pendingRequests.shift();
    sendToPython(msg);
  }
}

function handlePythonMessage(msg) {
  const win = BrowserWindow.getAllWindows()[0];
  if (!win) return;

  const { type, ...data } = msg;

  switch (type) {
    case 'ready':
      pythonReady = true;
      flushPending();
      lastStatus = { ...lastStatus, ready: true, model: data.model };
      win.webContents.send('backend:status', { ready: true, model: data.model });
      break;

    case 'token':
      win.webContents.send('stream:token', data);
      break;

    case 'tool_start':
      win.webContents.send('stream:tool_start', data);
      break;

    case 'tool_end':
      win.webContents.send('stream:tool_end', data);
      break;

    case 'tool_output':
      win.webContents.send('stream:tool_output', data);
      break;

    case 'thinking_start':
      win.webContents.send('stream:thinking_start', data);
      break;

    case 'thinking_end':
      win.webContents.send('stream:thinking_end', data);
      break;

    case 'turn_complete':
      win.webContents.send('stream:turn_complete', data);
      break;

    case 'error':
      win.webContents.send('stream:error', data);
      break;

    case 'subagent_start':
      win.webContents.send('stream:subagent_start', data);
      break;

    case 'subagent_output':
      win.webContents.send('stream:subagent_output', data);
      break;

    case 'subagent_end':
      win.webContents.send('stream:subagent_end', data);
      break;

    case 'subagent_tool_start':
      win.webContents.send('stream:subagent_tool_start', data);
      break;

    case 'subagent_tool_end':
      win.webContents.send('stream:subagent_tool_end', data);
      break;

    case 'subagent_thought':
      win.webContents.send('stream:subagent_thought', data);
      break;

    case 'status':
      lastStatus = { ...data, ready: pythonReady };
      win.webContents.send('backend:status', data);
      break;

    case 'response':
      // Generic response for slash commands etc.
      win.webContents.send('backend:response', data);
      break;

    case 'session_list_result':
      win.webContents.send('session:list_result', data);
      break;

    case 'session_delete_result':
      win.webContents.send('session:delete_result', data);
      break;

    default:
      console.log('Unknown message type from Python:', type);
  }
}

// ---------------------------------------------------------------------------
// IPC Handlers — renderer → main → Python
// ---------------------------------------------------------------------------

function setupIPC() {
  ipcMain.handle('backend:submit', async (event, text) => {
    sendToPython({ type: 'submit', text });
    return { ok: true };
  });

  ipcMain.handle('backend:command', async (event, command) => {
    sendToPython({ type: 'command', command });
    return { ok: true };
  });

  ipcMain.handle('backend:cancel', async () => {
    sendToPython({ type: 'cancel' });
    return { ok: true };
  });

  ipcMain.handle('backend:get_status', async () => {
    sendToPython({ type: 'get_status' });
    // Return cached status immediately so the renderer never starts blank.
    // The async response will update via backend:status event when it arrives.
    return lastStatus || { ready: false };
  });

  ipcMain.handle('session:list', async () => {
    sendToPython({ type: 'session_list' });
  });

  ipcMain.handle('session:switch', async (event, name) => {
    sendToPython({ type: 'session_switch', name });
  });

  ipcMain.handle('session:new', async (event, name) => {
    sendToPython({ type: 'session_new', name });
  });

  ipcMain.handle('session:delete', async (event, name) => {
    sendToPython({ type: 'session_delete', name });
  });

  ipcMain.handle('workspace:save', async (event, workspacePath) => {
    const persistedFile = path.join(require('os').homedir(), '.mini_agent_workspace');
    fs.writeFileSync(persistedFile, workspacePath, 'utf-8');
    return { ok: true };
  });

  ipcMain.handle('dialog:openWorkspace', async () => {
    const win = BrowserWindow.getAllWindows()[0];
    if (!win) return null;
    const result = await dialog.showOpenDialog(win, {
      title: 'Select Workspace',
      properties: ['openDirectory', 'createDirectory'],
    });
    if (result.canceled || !result.filePaths.length) return null;
    return result.filePaths[0];
  });

  // --- Settings / API key ---

  ipcMain.handle('settings:getApiKeyStatus', async () => {
    const keyInfo = detectApiKey();
    return { configured: keyInfo.configured, provider: keyInfo.provider };
  });

  ipcMain.handle('settings:saveApiKey', async (event, provider, key) => {
    const envName = PROVIDER_KEY_ENV[provider];
    if (!envName) return { ok: false, error: `Unknown provider: ${provider}` };

    const envFile = apiKeyEnvFile();
    const entries = readEnvFile(envFile);

    // Clear all existing provider keys so switching works cleanly
    for (const name of Object.values(PROVIDER_KEY_ENV)) {
      delete entries[name];
    }

    // Set the new key (empty key is valid for ollama)
    if (key) {
      entries[envName] = key;
    }

    writeEnvFile(envFile, entries);

    // Also set in current process so respawn picks it up
    process.env[envName] = key || '';
    // Clear other provider keys from current process
    for (const [p, name] of Object.entries(PROVIDER_KEY_ENV)) {
      if (p !== provider) delete process.env[name];
    }

    return { ok: true };
  });

  ipcMain.handle('settings:restartBackend', async () => {
    // Kill existing backend if running
    if (pythonProcess && !pythonProcess.killed) {
      try {
        if (pythonProcess.stdin && pythonProcess.stdin.writable) {
          pythonProcess.stdin.write(JSON.stringify({ type: 'shutdown' }) + '\n');
          pythonProcess.stdin.end();
        }
      } catch (e) { /* ignore */ }
      setTimeout(() => {
        if (pythonProcess && !pythonProcess.killed) {
          try { pythonProcess.kill(); } catch (e) { /* ignore */ }
        }
      }, 2000);
    }

    pythonReady = false;
    pendingRequests = [];
    lastStatus = null;

    // Resolve workspace (same logic as app.whenReady)
    const persistedFile = path.join(require('os').homedir(), '.mini_agent_workspace');
    let workspacePath = null;
    if (fs.existsSync(persistedFile)) {
      const persisted = fs.readFileSync(persistedFile, 'utf-8').trim();
      if (persisted && fs.existsSync(persisted)) {
        workspacePath = persisted;
      }
    }
    if (!workspacePath) {
      workspacePath = process.env.MINI_AGENT_WORKSPACE || process.cwd();
    }

    pythonProcess = spawnPythonBackend(workspacePath);
    return { ok: true };
  });
}

// ---------------------------------------------------------------------------
// Window
// ---------------------------------------------------------------------------

function createWindow() {
  const win = new BrowserWindow({
    width: 1200,
    height: 800,
    minWidth: 800,
    minHeight: 500,
    title: `mini_agent — Electron`,
    backgroundColor: '#1e1e2e',  // dark base background
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
    // Use dark title bar on macOS
    titleBarStyle: 'hiddenInset',
    trafficLightPosition: { x: 12, y: 12 },
  });

  const isDev = process.argv.includes('--dev');
  if (isDev) {
    // Load from Vite dev server
    const VITE_URL = 'http://localhost:5173';
    win.loadURL(VITE_URL).catch(() => {
      // Fallback: load built files if dev server isn't running
      win.loadFile(path.join(__dirname, 'renderer', 'dist', 'index.html'));
    });
  } else {
    // Production: load built files
    win.loadFile(path.join(__dirname, 'renderer', 'dist', 'index.html'));
  }

  // Open DevTools in dev mode
  if (isDev) {
    win.webContents.openDevTools({ mode: 'detach' });
  }

  // Block file-drop navigation — the preload handles drag-and-drop to
  // extract file paths and feed them into the user input.  This is a
  // safety net in case the preload's preventDefault doesn't fire first.
  win.webContents.on('will-navigate', (event, url) => {
    const parsed = require('url').parse(url);
    if (parsed.protocol === 'file:') {
      event.preventDefault();
    }
  });

  return win;
}

// ---------------------------------------------------------------------------
// App lifecycle
// ---------------------------------------------------------------------------

app.whenReady().then(() => {
  setupIPC();
  createWindow();

  const keyInfo = detectApiKey();

  // If no API key is configured, don't spawn the backend yet.
  // The renderer will show SettingsPanel and the user can provide one.
  // settings:restartBackend will spawn it after the key is saved.
  if (!keyInfo.configured) {
    // Tell the renderer to show the settings panel
    lastStatus = { ready: false, reason: 'no_api_key' };
    // Send after a short delay so the renderer's listener is registered
    setTimeout(() => {
      const win = BrowserWindow.getAllWindows()[0];
      if (win) win.webContents.send('backend:status', { ready: false, reason: 'no_api_key' });
    }, 500);
  } else {
    // Resolve workspace: CLI flag > persisted file > env var > cwd
    const workspaceArg = process.argv.find(a => a.startsWith('--workspace='));
    let workspacePath = null;
    if (workspaceArg) {
      workspacePath = workspaceArg.split('=')[1];
    } else {
      // Try persisted workspace from last session
      const persistedFile = path.join(require('os').homedir(), '.mini_agent_workspace');
      if (fs.existsSync(persistedFile)) {
        const persisted = fs.readFileSync(persistedFile, 'utf-8').trim();
        if (persisted && fs.existsSync(persisted)) {
          workspacePath = persisted;
        }
      }
    }
    if (!workspacePath) {
      workspacePath = process.env.MINI_AGENT_WORKSPACE || process.cwd();
    }

    pythonProcess = spawnPythonBackend(workspacePath);
  }

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });
});

app.on('window-all-closed', () => {
  _shuttingDown = true;
  if (pythonProcess && pythonProcess.stdin && !pythonProcess.killed) {
    try {
      pythonProcess.stdin.write(JSON.stringify({ type: 'shutdown' }) + '\n');
      pythonProcess.stdin.end();
    } catch (e) { /* ignore */ }
    // Hard timeout: if Python hasn't exited within 5s, force-kill and quit.
    setTimeout(() => {
      if (pythonProcess && !pythonProcess.killed) {
        try { pythonProcess.kill('SIGKILL'); } catch (e) { /* ignore */ }
      }
      // On macOS, also force quit — window-all-closed normally skips app.quit()
      // but we're shutting down the backend, not just hiding the window.
      if (process.platform === 'darwin') {
        app.quit();
      }
    }, 5000);
  } else {
    // No backend running — quit immediately on all platforms.
    app.quit();
  }
  if (process.platform !== 'darwin') {
    app.quit();
  }
});

app.on('before-quit', () => {
  if (pythonProcess) {
    try { pythonProcess.kill(); } catch (e) { /* ignore */ }
  }
});
