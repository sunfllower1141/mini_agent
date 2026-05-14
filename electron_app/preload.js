import { contextBridge, ipcRenderer } from "electron";

contextBridge.exposeInMainWorld("bridge", {
  /**
   * Send a message to the Python backend via stdin.
   * @param {string} message
   * @returns {Promise<{ok: boolean, error?: string}>}
   */
  send(message) {
    return ipcRenderer.invoke("bridge:send", message);
  },

  /**
   * Stop the Python bridge process.
   * @returns {Promise<{ok: boolean}>}
   */
  stop() {
    return ipcRenderer.invoke("bridge:stop");
  },

  /**
   * Subscribe to stdout lines from the Python process.
   * @param {(text: string) => void} callback
   * @returns {() => void} unsubscribe function
   */
  onStdout(callback) {
    const listener = (_event, text) => callback(text);
    ipcRenderer.on("bridge:stdout", listener);
    return () => ipcRenderer.removeListener("bridge:stdout", listener);
  },

  /**
   * Subscribe to stderr lines from the Python process.
   * @param {(text: string) => void} callback
   * @returns {() => void} unsubscribe function
   */
  onStderr(callback) {
    const listener = (_event, text) => callback(text);
    ipcRenderer.on("bridge:stderr", listener);
    return () => ipcRenderer.removeListener("bridge:stderr", listener);
  },

  /**
   * Subscribe to bridge close events.
   * @param {(code: number) => void} callback
   * @returns {() => void} unsubscribe function
   */
  onClose(callback) {
    const listener = (_event, code) => callback(code);
    ipcRenderer.on("bridge:closed", listener);
    return () => ipcRenderer.removeListener("bridge:closed", listener);
  },
});
