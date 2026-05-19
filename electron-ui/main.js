const { app, BrowserWindow } = require('electron');
const path = require('path');

// Fix GPU crashes on Linux
app.commandLine.appendSwitch('in-process-gpu');
app.commandLine.appendSwitch('disable-gpu-sandbox');

const isDev = process.argv.includes('--dev');

function createWindow() {
  const win = new BrowserWindow({
    width: 1600, height: 1000,
    minWidth: 900, minHeight: 600,
    backgroundColor: '#0d1117',
    titleBarStyle: 'hiddenInset',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  if (isDev) {
    win.loadURL('http://localhost:5173');
  } else {
    win.loadFile(path.join(__dirname, 'dist', 'index.html'));
  }
}

app.whenReady().then(createWindow);
app.on('window-all-closed', () => app.quit());
