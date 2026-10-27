import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Built assets are served by the FastAPI app from app/static/dist.
export default defineConfig({
  plugins: [react()],
  build: { outDir: "../app/static/dist", emptyOutDir: true, chunkSizeWarningLimit: 1600 },
  server: { proxy: { "/api": "http://127.0.0.1:8011" } },
});
