/**
 * main.js — Electron main process for mini_agent.
 *
 * Spawns the Python backend as a child process and bridges messages
 * between the renderer (via IPC) and the Python process (via JSON-lines
 * on stdin/stdout).
 */
const { app, BrowserWindow, ipcMain } = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');

// ---------------------------------------------------------------------------
// Python backend process
// ---------------------------------------------------------------------------

let pythonProcess = null;
let pythonReady = false;
let pendingRequests = [];

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

  const proc = spawn('python3', [backendScript], {
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
    // Python stderr goes to console for debugging
    process.stderr.write(`[python:stderr] ${data}`);
  });

  proc.on('close', (code) => {
    console.log(`Python backend exited with code ${code}`);
    pythonReady = false;
    pythonProcess = null;
  });

  proc.on('error', (err) => {
    console.error('Failed to start Python backend:', err.message);
    pythonReady = false;
    pythonProcess = null;
  });

  return proc;
}

function sendToPython(msg) {
  if (!pythonProcess || !pythonProcess.stdin.writable) {
    pendingRequests.push(msg);
    return;
  }
  pythonProcess.stdin.write(JSON.stringify(msg) + '\n');
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
      win.webContents.send('backend:status', { ready: true });
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

    case 'status':
      win.webContents.send('backend:status', data);
      break;

    case 'response':
      // Generic response for slash commands etc.
      win.webContents.send('backend:response', data);
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
    backgroundColor: '#1e1e2e',  // Catppuccin Mocha bg
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
    // Use dark title bar on macOS
    titleBarStyle: 'hiddenInset',
    trafficLightPosition: { x: 12, y: 12 },
  });

  win.loadFile(path.join(__dirname, 'renderer', 'index.html'));

  // Open DevTools in dev mode
  if (process.argv.includes('--dev')) {
    win.webContents.openDevTools({ mode: 'detach' });
  }

  return win;
}

// ---------------------------------------------------------------------------
// App lifecycle
// ---------------------------------------------------------------------------

app.whenReady().then(() => {
  setupIPC();
  createWindow();

  // Resolve workspace: command-line arg, env var, or cwd
  const workspaceArg = process.argv.find(a => a.startsWith('--workspace='));
  let workspacePath = null;
  if (workspaceArg) {
    workspacePath = workspaceArg.split('=')[1];
  } else if (process.env.MINI_AGENT_WORKSPACE) {
    workspacePath = process.env.MINI_AGENT_WORKSPACE;
  } else {
    workspacePath = process.cwd();
  }

  pythonProcess = spawnPythonBackend(workspacePath);

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });
});

app.on('window-all-closed', () => {
  if (pythonProcess) {
    pythonProcess.stdin.write(JSON.stringify({ type: 'shutdown' }) + '\n');
    pythonProcess.stdin.end();
    setTimeout(() => {
      if (pythonProcess) pythonProcess.kill();
    }, 2000);
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
