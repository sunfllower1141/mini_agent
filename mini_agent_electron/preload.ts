/**
 * preload.ts -- Context bridge for mini_agent Electron app.
 *
 * Exposes a safe `miniAgent` API to the renderer via contextBridge.
 * All Python communication goes through IPC to the main process.
 */
import { contextBridge, ipcRenderer, webUtils } from 'electron';

contextBridge.exposeInMainWorld('miniAgent', {
  // Send user message to agent
  submit: (text: string) => ipcRenderer.invoke('backend:submit', text),

  // Send slash command
  command: (cmd: string) => ipcRenderer.invoke('backend:command', cmd),

  // Cancel current turn
  cancel: () => ipcRenderer.invoke('backend:cancel'),

  // Open native directory picker for workspace selection
  openWorkspace: () => ipcRenderer.invoke('dialog:openWorkspace'),

  // Persist workspace across restarts
  saveWorkspace: (path: string) => ipcRenderer.invoke('workspace:save', path),

  // --- Session management ---
  newSession: (name: string) => ipcRenderer.invoke('session:new', name),
  switchSession: (name: string) => ipcRenderer.invoke('session:switch', name),
  deleteSession: (name: string) => ipcRenderer.invoke('session:delete', name),
  listSessions: () => ipcRenderer.invoke('session:list'),

  // Fetch initial status (used on mount to detect startup state)
  getStatus: () => ipcRenderer.invoke('backend:status'),

  // Switch the LLM model on the fly (no restart needed).
  setModel: (model: string) => ipcRenderer.invoke('settings:setModel', model),

  // Theme persistence to disk (~/.mini_agent_theme)
  getTheme: () => ipcRenderer.invoke('settings:getTheme'),
  saveTheme: (themeId: string) => ipcRenderer.invoke('settings:saveTheme', themeId),

  // Kill and restart the Python backend (called after saving a new API key).
  restartBackend: () => ipcRenderer.invoke('settings:restartBackend'),

  // --- Discord bot control ---
  startBot: (script: string) => ipcRenderer.invoke('bot:start', script),
  stopBot: (script: string) => ipcRenderer.invoke('bot:stop', script),

  // --- File drop bridge ---
  // Resolve the absolute file-system path for a File object dropped from the OS.
  getFilePath: (file: File) => {
    try { return webUtils.getPathForFile(file); } catch { return null; }
  },

  // Registers a callback that receives serializable file records whenever
  // files are pasted from the clipboard.
  onPaste: (callback: (records: any[]) => void) => {
    const handler = async (e: ClipboardEvent) => {
      const files = e.clipboardData?.files;
      if (!files || files.length === 0) return;

      const results: any[] = [];
      for (let i = 0; i < files.length; i++) {
        const file = files[i];
        let path = '';
        try { path = webUtils.getPathForFile(file) || ''; } catch { /* may throw */ }
        const item: any = {
          name: file.name,
          type: file.type,
          size: file.size,
          path,
        };
        if (file.type.startsWith('image/')) {
          try {
            item.dataUrl = await new Promise<string>((resolve, reject) => {
              const reader = new FileReader();
              reader.onload = () => resolve(reader.result as string);
              reader.onerror = reject;
              reader.readAsDataURL(file);
            });
          } catch {
            // ignore read errors
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
  on: (channel: string, callback: (data: any) => void) => {
    const validChannels = [
      'stream:token',
      'stream:tool_start',
      'stream:tool_end',
      'stream:tool_output',
      'stream:thinking_start',
      'stream:thinking_end',
      'stream:turn_complete',
      'stream:stats',
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
      const subscription = (_event: Electron.IpcRendererEvent, data: any) => callback(data);
      ipcRenderer.on(channel, subscription);
      return () => ipcRenderer.removeListener(channel, subscription);
    }
    return () => {};
  },

  // Remove all listeners for a channel
  removeAllListeners: (channel: string) => {
    ipcRenderer.removeAllListeners(channel);
  },
});
