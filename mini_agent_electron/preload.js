/**
 * preload.js — Context bridge for mini_agent Electron app.
 *
 * Exposes a safe `miniAgent` API to the renderer via contextBridge.
 * All Python communication goes through IPC to the main process.
 */
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('miniAgent', {
  // Send user message to agent
  submit: (text) => ipcRenderer.invoke('backend:submit', text),

  // Send slash command
  command: (cmd) => ipcRenderer.invoke('backend:command', cmd),

  // Cancel current turn
  cancel: () => ipcRenderer.invoke('backend:cancel'),

  // Open native directory picker for workspace selection
  openWorkspace: () => ipcRenderer.invoke('dialog:openWorkspace'),

  // Request status update
  getStatus: () => ipcRenderer.invoke('backend:get_status'),

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
