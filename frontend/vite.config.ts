import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// `npm run dev` serves the page with hot reload and forwards the API to a running
// `wavescribe serve` (WAVESCRIBE_URL, default http://127.0.0.1:8765).
declare const process: { env: Record<string, string | undefined> };
const backend = process.env.WAVESCRIBE_URL ?? "http://127.0.0.1:8765";

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../src/wavescribe/web/dist",
    emptyOutDir: true,
    assetsInlineLimit: 0,
    chunkSizeWarningLimit: 900,
  },
  server: {
    proxy: {
      "/api": { target: backend, changeOrigin: false },
      "/audio": { target: backend },
      "/feed": { target: backend },
    },
  },
});
