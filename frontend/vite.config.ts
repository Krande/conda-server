import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath, URL } from "node:url";

const backendTarget = process.env.VITE_BACKEND_URL || "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      // Proxy API + auth + repodata + package-download paths to the backend
      // during dev so the SPA can call the real endpoints on the same origin.
      "/api": { target: backendTarget, changeOrigin: true },
      "/health": { target: backendTarget, changeOrigin: true },
      "/ready": { target: backendTarget, changeOrigin: true },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
  },
});
