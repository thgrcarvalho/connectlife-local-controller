"use strict";

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("tray", {
  settings: () => ipcRenderer.invoke("get-settings"),
  publish: (snapshot) => ipcRenderer.send("snapshot", snapshot),
  hide: () => ipcRenderer.send("hide-popup"),
  log: (line) => ipcRenderer.send("log", line),
  resize: (height) => ipcRenderer.send("resize", height),
  openLog: () => ipcRenderer.send("open-log"),
  quit: () => ipcRenderer.send("quit"),
  onShown: (callback) => ipcRenderer.on("popup-shown", callback),
});
