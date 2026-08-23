import path from "node:path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(import.meta.dirname, "./src"),
    },
  },
  server: {
    proxy: {
      "/api": {
        // Must match LOOKUP_SERVER_PORT / scripts/dev.sh (default 8000).
        target: `http://127.0.0.1:${process.env.LOOKUP_SERVER_PORT ?? "8000"}`,
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ""),
      },
    },
  },
  preview: {
    allowedHosts: ["lookup.nishimweprince.dev"],
  },
});
