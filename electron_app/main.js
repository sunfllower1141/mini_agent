import { app, BrowserWindow, ipcMain } from "electron";
import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

/** @type {BrowserWindow | null} */
let mainWindow = null;
/** @type {import('child_process').ChildProcess | null} */
let pythonProcess = null;

// ---------- Python bridge ----------
function startPythonBridge() {
  const projectRoot = path.resolve(__dirname, "..");
  pythonProcess = spawn("python3", ["electron_bridge.py"], {
    cwd: projectRoot,
    stdio: ["pipe", "pipe", "pipe"],
  });

  pythonProcess.stdout?.on("data", (data) => {
    const text = data.toString().trim();
    if (!text) return;
    // Forward stdout lines to the renderer
    mainWindow?.webContents.send("bridge:stdout", text);
  });

  pythonProcess.stderr?.on("data", (data) => {
    const text = data.toString().trim();
    if (!text) return;
    mainWindow?.webContents.send("bridge:stderr", text);
  });

  pythonProcess.on("close", (code) => {
    mainWindow?.webContents.send("bridge:closed", code ?? -1);
  });
}

function stopPythonBridge() {
  if (pythonProcess && !pythonProcess.killed) {
    pythonProcess.kill();
    pythonProcess = null;
  }
}

// ---------- Window ----------
function createWindow() {
  mainWindow = new BrowserWindow({
    width: 900,
    height: 700,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  mainWindow.loadFile(path.join(__dirname, "index.html"));
  mainWindow.on("closed", () => {
    mainWindow = null;
  });
}

// ---------- IPC handlers ----------
ipcMain.handle("bridge:send", (_event, message) => {
  if (pythonProcess?.stdin?.writable) {
    pythonProcess.stdin.write(message + "\n");
    return { ok: true };
  }
  return { ok: false, error: "Python bridge not ready" };
});

ipcMain.handle("bridge:stop", () => {
  stopPythonBridge();
  return { ok: true };
});

// ---------- App lifecycle ----------
app.whenReady().then(() => {
  createWindow();
  startPythonBridge();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });
});

app.on("window-all-closed", () => {
  stopPythonBridge();
  app.quit();
});
