/**
 * Preload script for Electron
 *
 * This file runs in a sandboxed context with access to both Node.js and the DOM.
 * It's used to safely expose Node.js functionality to renderer processes.
 *
 * For this menu bar app, we don't currently use a renderer process,
 * but this file is included for future extensibility.
 */

const { contextBridge, ipcRenderer } = require('electron');

// Expose protected methods to renderer process
contextBridge.exposeInMainWorld('electronAPI', {
  // Placeholder for future IPC methods
  // Example: sendMessage: (channel, data) => ipcRenderer.send(channel, data)
});
