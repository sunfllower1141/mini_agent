/**
 * main.ts -- Electron main process for mini_agent.
 *
 * Spawns the Python backend as a child process and bridges messages
 * between the renderer (via IPC) and the Python process (via JSON-lines
 * on stdin/stdout).
 */
import { app, BrowserWindow, ipcMain, dialog } from 'electron';
import { spawn, exec, execSync } from 'child_process';
import * as path from 'path';
import * as fs from 'fs';
import * as os from 'os';

const HOMEDIR = os.homedir();

// ---------------------------------------------------------------------------
// Resource tuning -- this is a text-based chat app, not a game or browser.
// ---------------------------------------------------------------------------

// Windows: Chromium's disk cache can hit "Access is denied" (0x5) on some
// machines when trying to write to the default %LOCALAPPDATA% cache location.
if (process.platform === 'win32') {
  app.commandLine.appendSwitch('disable-gpu-shader-disk-cache');
  app.commandLine.appendSwitch('disable-http-cache');
}

// ---------------------------------------------------------------------------
// App config
// ---------------------------------------------------------------------------

let mainWindow: BrowserWindow | null = null;

const PROVIDER_KEY_ENV: Record<string, string> = {
  deepseek: 'DEEPSEEK_API_KEY',
  openai: 'OPENAI_API_KEY',
  claude: 'CLAUDE_API_KEY',
  gemini: 'GEMINI_API_KEY',
  xai: 'XAI_API_KEY',
  moonshot: 'MOONSHOT_API_KEY',
  groq: 'GROQ_API_KEY',
  openrouter: 'OPENROUTER_API_KEY',
  cerebras: 'CEREBRAS_API_KEY',
  together: 'TOGETHER_API_KEY',
};

interface ApiKeyResult {
  configured: boolean;
  provider: string | null;
  envName: string | null;
}

function detectApiKey(): ApiKeyResult {
  for (const [provider, envName] of Object.entries(PROVIDER_KEY_ENV)) {
    if (process.env[envName]) {
      return { configured: true, provider, envName };
    }
  }
  return { configured: false, provider: null, envName: null };
}

function apiKeyEnvFile(): string {
  return path.join(HOMEDIR, '.mini_agent_env');
}

function readEnvFile(filePath: string): Record<string, string> {
  if (!fs.existsSync(filePath)) return {};
  const entries: Record<string, string> = {};
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

function writeEnvFile(filePath: string, entries: Record<string, string>): void {
  const lines: string[] = [];
  for (const [key, value] of Object.entries(entries)) {
    lines.push(`${key}=${value}`);
  }
  fs.writeFileSync(filePath, lines.join('\n') + '\n', 'utf-8');
}

// ---------------------------------------------------------------------------
// Python backend process
// ---------------------------------------------------------------------------

let pythonProcess: ReturnType<typeof spawn> | null = null;
let pythonReady = false;
let pendingRequests: Array<{ msg: string; resolve: (value: any) => void; reject: (reason?: any) => void }> = [];
let lastStatus: Record<string, any> | null = null;
let workspacePath: string | null = null;
let _shuttingDown = false;

// Discord bot status tracking
let _botStatusTimer: ReturnType<typeof setInterval> | null = null;
const _botProcesses: Record<string, ReturnType<typeof spawn>> = {};

interface BotDef {
  name: string;
  label: string;
  script: string;
  pattern: string;
}

const _BOTS: BotDef[] = [
  { name: 'emotion-game',   label: 'discord_bot',   script: 'discord_bot.py',   pattern: 'discord_bot\\.py' },
  { name: 'mini-agent',     label: 'workspace_bot', script: 'workspace_bot.py', pattern: 'workspace_bot\\.py' },
];

function _sendBotStatus(name: string, alive: boolean, error?: string): void {
  const bot = _BOTS.find(b => b.name === name);
  if (!bot) return;
  const win = BrowserWindow.getAllWindows()[0];
  if (win) {
    win.webContents.send('backend:bot_status', { name: bot.name, label: bot.label, alive, error });
  }
}

function _pollBotStatus(): void {
  for (const bot of _BOTS) {
    const proc = _botProcesses[bot.name];
    if (proc && !proc.killed) {
      _sendBotStatus(bot.name, true);
      continue;
    }
    exec(`pgrep -q -f "${bot.pattern}"`, (err) => {
      _sendBotStatus(bot.name, !err);
    });
  }
}

function _startBot(script: string): { ok: boolean; error?: string; alreadyRunning?: boolean } {
  const bot = _BOTS.find(b => b.script === script);
  if (!bot) return { ok: false, error: 'unknown bot' };
  if (_botProcesses[bot.name] && !_botProcesses[bot.name].killed) {
    _sendBotStatus(bot.name, true);
    return { ok: true, alreadyRunning: true };
  }
  const wsPath = workspacePath || process.cwd();
  const isWindows = process.platform === 'win32';
  const venvPython = isWindows
    ? path.join(__dirname, '..', 'venv', 'Scripts', 'python.exe')
    : path.join(__dirname, '..', 'venv', 'bin', 'python3');
  const pythonBin = fs.existsSync(venvPython) ? venvPython : (isWindows ? 'python' : 'python3');
  const envTokens: Record<string, string> = {};
  for (const k of ['WORKSPACE_BOT_TOKEN','DISCORD_BOT_TOKEN','AGENT_WORKSPACE','MINI_AGENT_UI','DEEPSEEK_API_KEY']) {
    envTokens[k] = process.env[k] ? `${process.env[k]!.slice(0,8)}...` : '(not set)';
  }
  console.log(`[bot] starting ${bot.name}: python=${pythonBin}, cwd=${wsPath}, tokens=${JSON.stringify(envTokens)}`);
  const proc = spawn(pythonBin, ['-u', script], {
    cwd: wsPath,
    detached: true,
    windowsHide: true,
    stdio: ['ignore', 'pipe', 'pipe'] as const,
    env: { ...process.env },
  });
  let _stderr = '';
  let _stdout = '';
  proc.stdout?.on('data', (chunk: Buffer) => { _stdout += chunk.toString(); });
  proc.stderr?.on('data', (chunk: Buffer) => { _stderr += chunk.toString(); });
  proc.on('error', (err: Error) => {
    console.error(`[bot] ${bot.name} spawn error:`, err.message);
    delete _botProcesses[bot.name];
    _sendBotStatus(bot.name, false);
  });
  proc.on('exit', (code: number | null) => {
    if (code !== 0) {
      const errText = (_stderr || _stdout).slice(0, 500).trim();
      console.error(`[bot] ${bot.name} exited (code ${code})`, errText ? '|' : '', errText);
      const win = BrowserWindow.getAllWindows()[0];
      if (win) win.webContents.send('backend:bot_status', { name: bot.name, label: bot.label, alive: false, error: errText });
    } else {
      console.log(`[bot] ${bot.name} exited (code ${code})`);
      if (_stdout.trim()) console.log(`[bot] ${bot.name} stdout:`, _stdout.slice(0, 300).trim());
    }
    delete _botProcesses[bot.name];
    _sendBotStatus(bot.name, false);
  });
  proc.unref();
  _botProcesses[bot.name] = proc;
  _sendBotStatus(bot.name, true);
  return { ok: true };
}

function _stopBot(script: string): { ok: boolean; error?: string; alreadyStopped?: boolean } {
  const bot = _BOTS.find(b => b.script === script);
  if (!bot) return { ok: false, error: 'unknown bot' };
  const proc = _botProcesses[bot.name];
  if (proc && !proc.killed) {
    proc.kill();
    delete _botProcesses[bot.name];
    _sendBotStatus(bot.name, false);
    return { ok: true };
  }
  try {
    const pid = execSync(`pgrep -f "${bot.pattern}"`, { encoding: 'utf-8' }).trim();
    if (pid) {
      process.kill(parseInt(pid, 10), 'SIGTERM');
      _sendBotStatus(bot.name, false);
      return { ok: true };
    }
  } catch (_) { /* not found */ }
  _sendBotStatus(bot.name, false);
  return { ok: true, alreadyStopped: true };
}

function _startBotPolling(): void {
  _stopBotPolling();
  _pollBotStatus();
  _botStatusTimer = setInterval(_pollBotStatus, 30_000);
}

function _stopBotPolling(): void {
  if (_botStatusTimer) { clearInterval(_botStatusTimer); _botStatusTimer = null; }
}

// Zebar status file
const STATUS_FILE = path.join(HOMEDIR, '.glzr', 'zebar', 'mini_agent_status');

function _writeAgentStatus(status: string): void {
  try {
    const dir = path.dirname(STATUS_FILE);
    if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(STATUS_FILE, status, 'utf-8');
  } catch (_) { /* non-critical */ }
}

// ---------------------------------------------------------------------------
// Python backend management
// ---------------------------------------------------------------------------

function _resolveBackendPath(): { cmd: string; cwd: string } {
  const isDev = !app.isPackaged;
  const isWindows = process.platform === 'win32';

  if (isDev) {
    // Development: use source Python
    const venvPython = isWindows
      ? path.join(__dirname, '..', 'venv', 'Scripts', 'python.exe')
      : path.join(__dirname, '..', 'venv', 'bin', 'python3');
    const pythonBin = fs.existsSync(venvPython) ? venvPython : (isWindows ? 'python' : 'python3');
    return { cmd: pythonBin, cwd: path.join(__dirname, '..') };
  }

  // Production: use bundled PyInstaller binary
  const resourcesPath = process.resourcesPath!;
  const exeName = isWindows ? 'mini_agent_backend.exe' : 'mini_agent_backend';
  const backendExe = path.join(resourcesPath, 'backend', exeName);
  return { cmd: backendExe, cwd: resourcesPath };
}

function _spawnBackend(): void {
  if (pythonProcess) {
    pythonProcess.kill();
    pythonProcess = null;
  }
  pythonReady = false;

  const { cmd, cwd } = _resolveBackendPath();
  console.log(`[main] Starting backend: cmd=${cmd}, cwd=${cwd}`);

  pythonProcess = spawn(cmd, [], {
    cwd,
    stdio: ['pipe', 'pipe', 'pipe'] as const,
    env: { ...process.env },
  });

  // Forward stdout (JSON lines from backend)
  pythonProcess.stdout!.on('data', (data: Buffer) => {
    const text = data.toString();
    console.log(`[backend] ${text.trim()}`);
    // Parse JSON line(s) and forward to renderer
    const lines = text.split('\n').filter((l: string) => l.trim());
    for (const line of lines) {
      try {
        const msg = JSON.parse(line);
        if (msg.type === 'ready') {
          pythonReady = true;
          // Flush pending requests
          for (const pending of pendingRequests) {
            pythonProcess!.stdin!.write(pending.msg + '\n');
          }
          pendingRequests = [];
          // Send status to window
          if (mainWindow) {
            mainWindow.webContents.send('backend:status', { ...lastStatus, ready: true });
          }
        } else {
          const win = BrowserWindow.getAllWindows()[0];
          if (win) {
            win.webContents.send(msg.channel || msg.type, msg.data || msg);
          }
        }
      } catch (_) {
        // Non-JSON output -- log it
        console.log(`[backend:raw] ${line}`);
      }
    }
  });

  pythonProcess.stderr!.on('data', (data: Buffer) => {
    console.error(`[backend:err] ${data.toString().trim()}`);
  });

  pythonProcess.on('close', (code: number | null) => {
    console.log(`[main] Backend exited (code ${code})`);
    pythonProcess = null;
    pythonReady = false;
    if (!_shuttingDown) {
      // Attempt restart with backoff
      setTimeout(_spawnBackend, 2000);
    }
  });

  pythonProcess.on('error', (err: Error) => {
    console.error(`[main] Backend spawn error:`, err.message);
    pythonProcess = null;
    pythonReady = false;
    if (!_shuttingDown) {
      setTimeout(_spawnBackend, 5000);
    }
  });
}

function _sendToBackend(msg: string): void {
  if (pythonProcess && pythonReady) {
    pythonProcess.stdin!.write(msg + '\n');
  } else {
    pendingRequests.push({ msg, resolve: () => {}, reject: () => {} });
  }
}

// ---------------------------------------------------------------------------
// IPC handlers
// ---------------------------------------------------------------------------

function setupIPC(): void {
  ipcMain.handle('backend:submit', (_event, text: string) => {
    _sendToBackend(JSON.stringify({ type: 'submit', text }));
  });

  ipcMain.handle('backend:command', (_event, cmd: string) => {
    _sendToBackend(JSON.stringify({ type: 'command', cmd }));
  });

  ipcMain.handle('backend:cancel', () => {
    _sendToBackend(JSON.stringify({ type: 'cancel' }));
  });

  ipcMain.handle('dialog:openWorkspace', async () => {
    const result = await dialog.showOpenDialog(mainWindow!, {
      properties: ['openDirectory'],
    });
    return result.canceled ? null : result.filePaths[0];
  });

  ipcMain.handle('workspace:save', (_event, ws: string) => {
    workspacePath = ws;
    const configPath = path.join(HOMEDIR, '.mini_agent_workspace');
    fs.writeFileSync(configPath, ws, 'utf-8');
    // Also save to backend env file
    const envFile = apiKeyEnvFile();
    if (fs.existsSync(envFile)) {
      const entries = readEnvFile(envFile);
      entries['AGENT_WORKSPACE'] = ws;
      writeEnvFile(envFile, entries);
    }
    return { ok: true };
  });

  ipcMain.handle('settings:setModel', (_event, model: string) => {
    _sendToBackend(JSON.stringify({ type: 'set_model', model }));
  });

  ipcMain.handle('settings:restartBackend', () => {
    _spawnBackend();
    lastStatus = { ...lastStatus, ready: false };
    return { ok: true };
  });

  ipcMain.handle('settings:getTheme', () => {
    const themeFile = path.join(HOMEDIR, '.mini_agent_theme');
    if (fs.existsSync(themeFile)) {
      return fs.readFileSync(themeFile, 'utf-8').trim();
    }
    return null;
  });

  ipcMain.handle('settings:saveTheme', (_event, themeId: string) => {
    const themeFile = path.join(HOMEDIR, '.mini_agent_theme');
    fs.writeFileSync(themeFile, themeId, 'utf-8');
    return { ok: true };
  });

  ipcMain.handle('backend:status', () => {
    return lastStatus || { ready: false };
  });

  // Session management
  ipcMain.handle('session:new', (_event, name: string) => {
    _sendToBackend(JSON.stringify({ type: 'session_new', name }));
  });

  ipcMain.handle('session:switch', (_event, name: string) => {
    _sendToBackend(JSON.stringify({ type: 'session_switch', name }));
  });

  ipcMain.handle('session:list', () => {
    _sendToBackend(JSON.stringify({ type: 'session_list' }));
  });

  ipcMain.handle('session:delete', (_event, name: string) => {
    _sendToBackend(JSON.stringify({ type: 'session_delete', name }));
  });

  // Bot management
  ipcMain.handle('bot:start', (_event, script: string) => {
    return _startBot(script);
  });

  ipcMain.handle('bot:stop', (_event, script: string) => {
    return _stopBot(script);
  });

  ipcMain.handle('bot:status', () => {
    const status: Record<string, boolean> = {};
    for (const bot of _BOTS) {
      const proc = _botProcesses[bot.name];
      status[bot.name] = !!(proc && !proc.killed);
    }
    return status;
  });
}

// ---------------------------------------------------------------------------
// Window creation
// ---------------------------------------------------------------------------

function createWindow(): void {
  mainWindow = new BrowserWindow({
    width: 1200,
    height: 800,
    minWidth: 800,
    minHeight: 500,
    title: 'mini_agent',
    backgroundColor: '#0d1117',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
    show: false,
  });

  // Load the renderer
  if (process.env.VITE_DEV_SERVER_URL) {
    mainWindow.loadURL(process.env.VITE_DEV_SERVER_URL);
  } else {
    const distPath = path.join(__dirname, 'renderer', 'dist', 'index.html');
    mainWindow.loadFile(distPath);
  }

  mainWindow.once('ready-to-show', () => {
    mainWindow!.show();
  });

  mainWindow.on('closed', () => {
    mainWindow = null;
  });
}

// ---------------------------------------------------------------------------
// App lifecycle
// ---------------------------------------------------------------------------

app.whenReady().then(() => {
  setupIPC();
  createWindow();
  _spawnBackend();
  _startBotPolling();

  // Load persisted workspace
  const configPath = path.join(HOMEDIR, '.mini_agent_workspace');
  if (fs.existsSync(configPath)) {
    workspacePath = fs.readFileSync(configPath, 'utf-8').trim();
  }

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });
});

app.on('window-all-closed', () => {
  _shuttingDown = true;
  _stopBotPolling();
  if (pythonProcess) {
    pythonProcess.kill();
    pythonProcess = null;
  }
  if (process.platform !== 'darwin') {
    app.quit();
  }
});
