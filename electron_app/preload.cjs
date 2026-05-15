const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("bridge", {
  send(message) {
    return ipcRenderer.invoke("bridge:send", message);
  },

  stop() {
    return ipcRenderer.invoke("bridge:stop");
  },

  onStdout(callback) {
    const listener = (_event, text) => callback(text);
    ipcRenderer.on("bridge:stdout", listener);
    return () => ipcRenderer.removeListener("bridge:stdout", listener);
  },

  onStderr(callback) {
    const listener = (_event, text) => callback(text);
    ipcRenderer.on("bridge:stderr", listener);
    return () => ipcRenderer.removeListener("bridge:stderr", listener);
  },

  onClose(callback) {
    const listener = (_event, code) => callback(code);
    ipcRenderer.on("bridge:closed", listener);
    return () => ipcRenderer.removeListener("bridge:closed", listener);
  },
});