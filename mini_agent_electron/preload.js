/**
 * preload.js — Context bridge for mini_agent Electron app.
 *
 * Exposes a safe `miniAgent` API to the renderer via contextBridge.
 * All Python communication goes through IPC to the main process.
 */
const { contextBridge, ipcRenderer, webUtils } = require('electron');

contextBridge.exposeInMainWorld('miniAgent', {
  // Send user message to agent
  submit: (text) => ipcRenderer.invoke('backend:submit', text),

  // Send slash command
  command: (cmd) => ipcRenderer.invoke('backend:command', cmd),

  // Cancel current turn
  cancel: () => ipcRenderer.invoke('backend:cancel'),

  // Open native directory picker for workspace selection
  openWorkspace: () => ipcRenderer.invoke('dialog:openWorkspace'),

  // Persist workspace across restarts
  saveWorkspace: (path) => ipcRenderer.invoke('workspace:save', path),

  // --- Session management ---
  // List sessions in current workspace. Returns promise resolving to {sessions, current, error?}.
  listSessions: () => {
    return new Promise((resolve) => {
      const handler = (_event, data) => {
        ipcRenderer.removeListener('session:list_result', handler);
        resolve(data);
      };
      ipcRenderer.on('session:list_result', handler);
      ipcRenderer.invoke('session:list');
    });
  },

  // Switch to an existing session
  switchSession: (name) => ipcRenderer.invoke('session:switch', name),

  // Create a new session
  newSession: (name) => ipcRenderer.invoke('session:new', name),

  // Request status update
  getStatus: () => ipcRenderer.invoke('backend:get_status'),

  // --- File drop bridge ---
  // Registers a callback that receives an array of absolute file paths
  // whenever the user drops files from the OS onto the window.
  // Returns an unsubscribe function.
  onFileDrop: (callback) => {
    const inputFrame = () => document.getElementById('input-frame');
    const handler = (e) => {
      const frame = inputFrame();
      if (frame) frame.classList.remove('drag-over');
      // Must preventDefault BEFORE reading paths — Electron's default
      // is to navigate to / open the dropped file.
      e.preventDefault();
      e.stopPropagation();
      const files = e.dataTransfer?.files;
      if (!files || files.length === 0) return;
      const paths = [];
      for (let i = 0; i < files.length; i++) {
        // File.path was removed in Electron 32; use webUtils instead
        const p = webUtils.getPathForFile(files[i]);
        if (p) paths.push(p);
      }
      if (paths.length === 0) return;
      callback(paths);
    };
    const dragOver = (e) => {
      const files = e.dataTransfer?.files;
      if (!files || files.length === 0) return;
      // Always prevent default for file drags — do NOT gate on file.path,
      // because file.path may only be populated on drop, not dragover.
      e.preventDefault();
      e.stopPropagation();
      e.dataTransfer.dropEffect = 'copy';
      const frame = inputFrame();
      if (frame) frame.classList.add('drag-over');
    };
    const dragLeave = (e) => {
      // Only remove when actually leaving the document
      if (e.target === document.documentElement || e.target === document.body) {
        const frame = inputFrame();
        if (frame) frame.classList.remove('drag-over');
      }
    };
    document.addEventListener('dragover', dragOver);
    document.addEventListener('dragleave', dragLeave);
    document.addEventListener('drop', handler);
    return () => {
      document.removeEventListener('dragover', dragOver);
      document.removeEventListener('dragleave', dragLeave);
      document.removeEventListener('drop', handler);
    };
  },

  // --- Event listeners (renderer subscribes) ---
  on: (channel, callback) => {
    const validChannels = [
      'stream:token',
      'stream:tool_start',
      'stream:tool_end',
      'stream:tool_output',
      'stream:thinking_start',
      'stream:thinking_end',
      'stream:turn_complete',
      'stream:error',
      'backend:status',
      'backend:response',
    ];
    if (validChannels.includes(channel)) {
      const subscription = (_event, data) => callback(data);
      ipcRenderer.on(channel, subscription);
      // Return an unsubscribe function
      return () => ipcRenderer.removeListener(channel, subscription);
    }
    return () => {};
  },

  // Remove all listeners for a channel
  removeAllListeners: (channel) => {
    ipcRenderer.removeAllListeners(channel);
  },
});
