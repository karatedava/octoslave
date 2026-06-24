import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Builds the Lab UI into octoslave/web/lab_static and serves it under /lab/.
// In dev (`npm run dev`) /ws and /api are proxied to the FastAPI server.
export default defineConfig({
  plugins: [react()],
  base: "/lab/",
  build: {
    outDir: "../octoslave/web/lab_static",
    emptyOutDir: true,
  },
  server: {
    proxy: {
      "/ws": { target: "ws://localhost:8000", ws: true },
      "/api": "http://localhost:8000",
    },
  },
});
