/**
 * main.js -- Electron main process for mini_agent.
 *
 * Spawns the Python backend as a child process and bridges messages
 * between the renderer (via IPC) and the Python process (via JSON-lines
 * on stdin/stdout).
 */
const { app, BrowserWindow, ipcMain, dialog, protocol, net } = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');
const os = require('os');

const HOMEDIR = os.homedir();

// ---------------------------------------------------------------------------
// Resource tuning -- this is a text-based chat app, not a game or browser.
// ---------------------------------------------------------------------------
// NOTE: Do NOT use --in-process-gpu on Windows -- it causes blank rendering
// on many GPU/driver combinations because the merged GPU thread can't get a
// valid drawing context for the BrowserWindow.
//
// Do NOT cap V8 heap at 128 MB -- React 19 + Shiki (syntax highlighting with
// ~200 language grammars) needs 300-500 MB during startup.  A tight limit
// causes silent OOM in the renderer process, killing the JS engine before
// React can mount.
//
// Keep --disable-gpu-shader-disk-cache and --disable-http-cache on Windows
// only: they prevent "Access is denied" errors when Chromium tries to write
// to %LOCALAPPDATA% disk caches.

// Windows: Chromium's disk cache can hit "Access is denied" (0x5) on some
// machines when trying to write to the default %LOCALAPPDATA% cache location.
// This is a local-first chat app that loads files via file:// -- disk caches
// are not needed.  Disable them entirely to avoid permission errors.
if (process.platform === 'win32') {
  // GPU shader disk cache (fixes: ERROR:gpu\ipc\host\gpu_disk_cache.cc:737)
  app.commandLine.appendSwitch('disable-gpu-shader-disk-cache');
  // HTTP disk cache (fixes: ERROR:net\disk_cache\disk_cache.cc:284)
  app.commandLine.appendSwitch('disable-http-cache');
  console.log('[main] Windows: disabled disk caches to avoid Access Denied errors');
}

// ---------------------------------------------------------------------------
// Custom protocol -- serves renderer files with CORS headers so ES modules
// work.  Chromium blocks <script type="module"> from file:// URLs; using a
// custom scheme with corsEnabled bypasses this.
// ---------------------------------------------------------------------------
protocol.registerSchemesAsPrivileged([
  {
    scheme: 'miniagent',
    privileges: {
      standard: true,
      secure: true,
      supportFetchAPI: true,
      corsEnabled: true,
      stream: true,
    },
  },
]);

/**
* Resolve a miniagent:// path to an absolute file-system path inside
* renderer/dist/.  Returns null if the path tries to escape the dist dir
* (path traversal prevention).
*/
function resolveDistPath(urlPath) {
  // Strip leading slash
  const rel = urlPath.replace(/^\/+/, '') || 'index.html';
  // Prevent path traversal
  if (rel.includes('..') || rel.includes(':')) return null;
  const abs = path.join(__dirname, 'renderer', 'dist', rel);
  if (!fs.existsSync(abs)) {
    // If not found, fall back to index.html (for SPA routing)
    return path.join(__dirname, 'renderer', 'dist', 'index.html');
  }
  return abs;
}

// ---------------------------------------------------------------------------
// Load .env file -- GUI apps on macOS don't inherit shell profile vars
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
loadEnvFile(path.join(HOMEDIR, '.mini_agent_env'));

// ---------------------------------------------------------------------------
// API key detection
// ---------------------------------------------------------------------------

const PROVIDER_KEY_ENV = {
  deepseek: 'DEEPSEEK_API_KEY',
  moonshot: 'MOONSHOT_API_KEY',
  claude: 'CLAUDE_API_KEY',
  xai: 'XAI_API_KEY',
  ollama: 'OLLAMA_API_KEY',
  openrouter: 'OPENROUTER_API_KEY',
  qwen: 'DASHSCOPE_API_KEY',
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
  return path.join(HOMEDIR, '.mini_agent_env');
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
let workspacePath = null;  // module-scoped so watchdog/restart closures can use it
let _shuttingDown = false;  // set during window-close to prevent restart loops
// Restart throttle: prevent infinite restart loops that spawn thousands of
// processes when the Python backend keeps crashing (e.g. hung proc.communicate()
// on Windows that can't be killed from userspace).
let _restartCount = 0;
let _restartWindowStart = 0;
let _notifyRestarted = false; // set after crash; cleared once backend sends 'ready'
const _MAX_RESTARTS = 3;
const _RESTART_WINDOW_MS = 30000;

// Watchdog: if the backend sends no messages for 120 seconds (e.g. hung
// API call, stuck subprocess, deadlocked thread), force-kill and restart.
// This prevents the "freeze mid-run" bug where the UI goes blank because
// the backend is alive (proc.on('close') never fires) but unresponsive.
let _watchdogTimer = null;
const _WATCHDOG_TIMEOUT_MS = 120_000;

function _clearWatchdog() {
  if (_watchdogTimer) { clearTimeout(_watchdogTimer); _watchdogTimer = null; }
}

function _resetWatchdog() {
  _clearWatchdog();
  if (_shuttingDown) return;
  const watchedProc = pythonProcess;  // capture the specific backend instance
  _watchdogTimer = setTimeout(() => {
    // Only act if the backend we were watching is STILL the current one
    if (pythonProcess !== watchedProc) return;
    console.error('[main] WATCHDOG: backend unresponsive for 120s -- force-killing and restarting');
    const win = BrowserWindow.getAllWindows()[0];
    if (win) win.webContents.send('stream:error', { message: 'Backend appears hung. Restarting...' });
    _killPythonProcessTree();
    pythonReady = false;
    pythonProcess = null;
    _clearWatchdog();
    if (!_shuttingDown) {
      pythonProcess = spawnPythonBackend(workspacePath);
    }
  }, _WATCHDOG_TIMEOUT_MS);
}

function _killPythonProcessTree() {
  // Kill the backend process tree on Windows (taskkill /T kills children).
  // On Unix, send SIGKILL to the process group.
  if (!pythonProcess || pythonProcess.killed) return;
  try {
    if (process.platform === 'win32') {
      require('child_process').execSync(`taskkill /F /T /PID ${pythonProcess.pid}`, { timeout: 5000 });
    } else {
      pythonProcess.kill('SIGKILL');
    }
  } catch (e) { /* best-effort */ }
}

function spawnPythonBackend(workspacePath) {
  const backendScript = path.join(__dirname, 'backend', 'server.py');
  
  if (!fs.existsSync(backendScript)) {
    console.error(`Backend script not found: ${backendScript}`);
    return null;
  }

  const env = { ...process.env };
  // Force UTF-8 on Windows to prevent 'charmap' codec errors when the
  // Python backend writes Unicode characters (->, [MOON], ...) to stdout/stderr.
  // Python's locale.getpreferredencoding() reads this at startup, so it
  // must be set before spawning the child process.
  if (process.platform === 'win32') {
    env.PYTHONUTF8 = '1';
    env.PYTHONIOENCODING = 'utf-8';
  }
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

  // Start the watchdog immediately -- the backend must produce stdout
  // (e.g. the 'ready' message) within the timeout window or it's killed.
  _resetWatchdog();

  // Buffer for incomplete JSON lines from stdout
  let stdoutBuffer = '';

  proc.stdout.on('data', (data) => {
    _resetWatchdog();  // backend is alive -- reset the hang watchdog
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
    _resetWatchdog();  // stderr output also means backend is alive
    // Log Python stderr to Electron console only -- not the tools panel.
    // HF warnings, tqdm bars, etc. are noise in the UI.
    const text = data.toString().trim();
    if (text) {
      // Suppress known-harmless multiprocess shutdown traceback (Python 3.12+)
      // multiprocess 0.70.x resource_tracker hits AttributeError on _recursion_count
      if (text.includes('multiprocess/resource_tracker.py') && text.includes('_recursion_count')) {
        return;
      }
      console.log(`[python:stderr] ${data}`);
    }
  });

  proc.on('close', (code) => {
    _clearWatchdog();  // backend exited -- clear watchdog
    console.log(`Python backend exited with code ${code}`);
    pythonReady = false;
    pythonProcess = null;
    // If we're in a shutdown sequence and the backend exited cleanly,
    // quit Electron regardless of platform (fixes macOS hang).
    if (_shuttingDown) {
      app.quit();
      return;
    }
    // Auto-restart on unexpected exit (not a clean shutdown).
    // Throttle restarts to prevent infinite spawn loops that can
    // create thousands of base.exe/conhost.exe processes on Windows.
    if (code !== 0 && code !== null) {
      const now = Date.now();
      if (now - _restartWindowStart > _RESTART_WINDOW_MS) {
        _restartCount = 0;
        _restartWindowStart = now;
      }
      _restartCount++;
      if (_restartCount > _MAX_RESTARTS) {
        const msg = `Backend crashed ${_restartCount}x in ${Math.round((now - _restartWindowStart) / 1000)}s -- giving up. Please restart the app.`;
        console.error(msg);
        const win = BrowserWindow.getAllWindows()[0];
        if (win) win.webContents.send('stream:error', { message: msg });
        return;
      }
      const win = BrowserWindow.getAllWindows()[0];
      const restartMsg = `Backend crashed (exit ${code}). Restarting (attempt ${_restartCount}/${_MAX_RESTARTS})...`;
      if (win) win.webContents.send('stream:error', { message: restartMsg });
      _notifyRestarted = true; // notify user once backend comes back
      const restartWorkspace = workspacePath;
      // Exponential backoff: 1.5s -> 3s -> 6s
      const delay = 1500 * Math.pow(2, _restartCount - 1);
      setTimeout(() => {
        if (!pythonProcess) {
          pythonProcess = spawnPythonBackend(restartWorkspace);
          if (!pythonProcess) {
            const win = BrowserWindow.getAllWindows()[0];
            if (win) win.webContents.send('stream:error', { message: 'Backend server.py not found.' });
            console.error('Backend script not found -- agent will not start.');
          }
        }
      }, delay);
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
  try {
    _stdinLock = true;
    _stdinWriteUnlocked(msg);
  } finally {
    _stdinLock = false;
  }
  // Drain any queued messages (with try/finally on each)
  while (_stdinQueue.length > 0) {
    try {
      _stdinLock = true;
      _stdinWriteUnlocked(_stdinQueue.shift());
    } finally {
      _stdinLock = false;
    }
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
      if (_notifyRestarted) {
        _notifyRestarted = false;
        win.webContents.send('stream:status', { message: 'Backend restarted successfully.' });
      }
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
      console.log('[main] Agent turn complete');
      break;

    case 'turn_start':
      win.webContents.send('backend:turn_start', data);
      break;

    case 'idle':
      win.webContents.send('backend:idle', data);
      console.log('[main] Agent idle');
      break;

    case 'heartbeat':
      // Daemon heartbeat from Python backend -- resets watchdog via stdout
      // data handler (no renderer action needed).
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
// IPC Handlers -- renderer -> main -> Python
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
    const persistedFile = path.join(HOMEDIR, '.mini_agent_workspace');
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

  ipcMain.handle('settings:setModel', async (event, model) => {
    if (!model) return { ok: false, error: 'Model name required' };
    sendToPython({ type: 'set_model', model });
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
          try {
            if (process.platform === 'win32') {
              require('child_process').execSync(`taskkill /F /T /PID ${pythonProcess.pid}`, { timeout: 5000 });
            } else {
              pythonProcess.kill();
            }
          } catch (e) { /* ignore */ }
        }
      }, 2000);
    }

    pythonReady = false;
    pendingRequests = [];
    lastStatus = null;

    // Resolve workspace (same logic as app.whenReady)
    const persistedFile = path.join(HOMEDIR, '.mini_agent_workspace');
    workspacePath = null;
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
    title: `mini_agent -- Electron`,
    backgroundColor: '#1e1e2e',  // dark base background
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      webSecurity: false,  // allow ES modules from file:// URLs
    },
    // Use dark title bar on macOS
    titleBarStyle: 'hiddenInset',
    trafficLightPosition: { x: 12, y: 12 },
  });

  const isDev = process.argv.includes('--dev');

  // Avoid loading the page until the protocol handler is registered.
  // We load after a 0-delay tick to let the event loop process any pending
  // protocol registration (belt-and-suspenders, normally not needed).

  if (isDev) {
    // Load from Vite dev server (HMR, source maps, etc.)
    const VITE_URL = 'http://localhost:5173';
    win.loadURL(VITE_URL).catch(() => {
      // Fallback: load built files directly
      const distIndex = path.join(__dirname, 'renderer', 'dist', 'index.html');
      if (fs.existsSync(distIndex)) {
        win.loadFile(distIndex);
      }
    });
  } else {
    // Production: load built files directly
    const distIndex = path.join(__dirname, 'renderer', 'dist', 'index.html');
    win.loadFile(distIndex);
  }

  // Open DevTools in dev mode
  if (isDev) {
    win.webContents.openDevTools({ mode: 'detach' });
  }

  // Block file-drop navigation -- the preload handles drag-and-drop to
  // extract file paths and feed them into the user input.  This is a
  // safety net in case the preload's preventDefault doesn't fire first.
  win.webContents.on('will-navigate', (event, url) => {
    const parsed = require('url').parse(url);
    if (parsed.protocol === 'file:') {
      event.preventDefault();
    }
  });

  // Diagnostic: log page load success/failure
  win.webContents.on('did-finish-load', () => {
    console.log('[main] renderer page loaded successfully');
  });
  win.webContents.on('did-fail-load', (_event, errorCode, errorDescription, validatedURL) => {
    console.error(`[main] renderer FAILED to load: ${errorDescription} (code ${errorCode}) URL: ${validatedURL}`);
  });
  win.webContents.on('page-title-updated', (_event, title) => {
    console.log(`[main] page title: "${title}"`);
  });

  return win;
}

// ---------------------------------------------------------------------------
// App lifecycle
// ---------------------------------------------------------------------------

// MIME map for serving static files from the custom protocol handler.
const MIME_MAP = {
  '.html': 'text/html; charset=utf-8',
  '.js':   'text/javascript; charset=utf-8',
  '.mjs':  'text/javascript; charset=utf-8',
  '.css':  'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.png':  'image/png',
  '.jpg':  'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.gif':  'image/gif',
  '.svg':  'image/svg+xml',
  '.ico':  'image/x-icon',
  '.woff':  'font/woff',
  '.woff2': 'font/woff2',
  '.ttf':   'font/ttf',
  '.map':  'application/json; charset=utf-8',
};

app.whenReady().then(() => {
  // Register the custom protocol handler -- serve renderer/dist files.
  // Using fs.readFileSync instead of net.fetch('file:///...') because
  // net.fetch may not support file:// URLs in protocol handlers on all
  // platforms (especially Windows).
  protocol.handle('miniagent', (request) => {
    try {
      const urlPath = new URL(request.url).pathname;
      const filePath = resolveDistPath(urlPath);
      if (!filePath) {
        return new Response('Not found', { status: 404 });
      }
      const ext = path.extname(filePath).toLowerCase();
      const mimeType = MIME_MAP[ext] || 'application/octet-stream';
      const content = fs.readFileSync(filePath);
      return new Response(content, {
        status: 200,
        headers: { 'Content-Type': mimeType },
      });
    } catch (err) {
      console.error(`[miniagent] Failed to serve ${request.url}:`, err.message);
      return new Response('Internal error', { status: 500 });
    }
  });

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
    workspacePath = null;
    if (workspaceArg) {
      workspacePath = workspaceArg.split('=')[1];
    } else {
      // Try persisted workspace from last session
      const persistedFile = path.join(HOMEDIR, '.mini_agent_workspace');
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
    // Hard timeout: if Python hasn't exited within 5s, force-kill the entire
    // process tree (on Windows this is critical -- proc.kill() only kills the
    // immediate process, leaving orphaned bash.exe/conhost.exe children).
    setTimeout(() => {
      if (pythonProcess && !pythonProcess.killed) {
        try {
          if (process.platform === 'win32') {
            // Kill entire process tree via taskkill
            require('child_process').execSync(`taskkill /F /T /PID ${pythonProcess.pid}`, { timeout: 5000 });
          } else {
            pythonProcess.kill('SIGKILL');
          }
        } catch (e) { /* ignore */ }
      }
      // On macOS, also force quit -- window-all-closed normally skips app.quit()
      // but we're shutting down the backend, not just hiding the window.
      if (process.platform === 'darwin') {
        app.quit();
      }
    }, 5000);
  } else {
    // No backend running -- quit immediately on all platforms.
    app.quit();
  }
  if (process.platform !== 'darwin') {
    app.quit();
  }
});

app.on('before-quit', () => {
  if (pythonProcess && !pythonProcess.killed) {
    try {
      if (process.platform === 'win32') {
        require('child_process').execSync(`taskkill /F /T /PID ${pythonProcess.pid}`, { timeout: 5000 });
      } else {
        pythonProcess.kill();
      }
    } catch (e) { /* ignore */ }
  }
});
