/**
 * preload.js -- Context bridge for mini_agent Electron app.
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

  // Delete a session. Returns promise resolving to {ok, message?}.
  deleteSession: (name) => {
    return new Promise((resolve) => {
      const handler = (_event, data) => {
        ipcRenderer.removeListener('session:delete_result', handler);
        resolve(data);
      };
      ipcRenderer.on('session:delete_result', handler);
      ipcRenderer.invoke('session:delete', name);
    });
  },

  // Request status update
  getStatus: () => ipcRenderer.invoke('backend:get_status'),

  // --- Settings / API key management ---
  // Check if an API key is already configured. Returns { configured, provider }.
  getApiKeyStatus: () => ipcRenderer.invoke('settings:getApiKeyStatus'),

  // Save an API key for the chosen provider to ~/.mini_agent_env.
  // provider: 'deepseek' | 'claude' | 'xai' | 'ollama' | 'openrouter'
  saveApiKey: (provider, key) => ipcRenderer.invoke('settings:saveApiKey', provider, key),

  // Switch the LLM model on the fly (no restart needed).
  setModel: (model) => ipcRenderer.invoke('settings:setModel', model),

  // Theme persistence to disk (~/.mini_agent_theme)
  getTheme: () => ipcRenderer.invoke('settings:getTheme'),
  saveTheme: (themeId) => ipcRenderer.invoke('settings:saveTheme', themeId),

  // Kill and restart the Python backend (called after saving a new API key).
  restartBackend: () => ipcRenderer.invoke('settings:restartBackend'),

  // --- Discord bot control ---
  startBot: (script) => ipcRenderer.invoke('bot:start', script),
  stopBot: (script) => ipcRenderer.invoke('bot:stop', script),

  // --- File drop bridge ---
  // Resolve the absolute file-system path for a File object dropped from the OS.
  // Electron removed File.path in v32; use webUtils instead.
  getFilePath: (file) => {
    try { return webUtils.getPathForFile(file); } catch { return null; }
  },

  // Registers a callback that receives serializable file records whenever
  // --- Clipboard paste bridge ---
  // Listens for Ctrl+V paste of files (images or other files).
  // Image files include a base64 dataUrl for instant preview.


  onPaste: (callback) => {
    const handler = async (e) => {
      const files = e.clipboardData?.files;
      if (!files || files.length === 0) return;

      // Don't preventDefault -- let text still paste into the textarea normally.
      const results = [];
      for (let i = 0; i < files.length; i++) {
        const file = files[i];
        let path = '';
        try { path = webUtils.getPathForFile(file) || ''; } catch { /* may throw in some Electron versions */ }
        const item = {
          name: file.name,
          type: file.type,
          size: file.size,
          path,
        };
        // Read image files as base64 data URLs for instant preview
        if (file.type.startsWith('image/')) {
          try {
            item.dataUrl = await new Promise((resolve, reject) => {
              const reader = new FileReader();
              reader.onload = () => resolve(reader.result);
              reader.onerror = reject;
              reader.readAsDataURL(file);
            });
          } catch {
            // ignore read errors -- still include the file record
          }
        }
        results.push(item);
      }
      if (results.length > 0) callback(results);
    };
    document.addEventListener('paste', handler);
    return () => document.removeEventListener('paste', handler);
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
      'stream:status',
      'stream:subagent_start',
      'stream:subagent_output',
      'stream:subagent_end',
      'stream:subagent_tool_start',
      'stream:subagent_tool_end',
      'stream:subagent_thought',
      'backend:status',
      'backend:response',
      'backend:turn_start',
      'backend:idle',
      'backend:bot_status',
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
