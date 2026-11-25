import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

import { engineBridge } from "./vite-engine-bridge";

// Tauri expects a fixed port and launches the dev server itself (tauri.conf.json).
// The engine bridge is only for running the UI in a plain browser; inside
// Tauri the shell owns the engine, so the bridge is disabled there.
const insideTauri = !!process.env.TAURI_ENV_PLATFORM;

export default defineConfig({
  plugins: [react(), ...(insideTauri ? [] : [engineBridge()])],
  clearScreen: false,
  server: {
    port: 1420,
    strictPort: true,
    watch: { ignored: ["**/src-tauri/**"] },
  },
  envPrefix: ["VITE_", "TAURI_ENV_"],
  build: {
    target: "es2022",
    sourcemap: !!process.env.TAURI_ENV_DEBUG,
  },
});
