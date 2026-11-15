import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";

export default defineConfig({
  plugins: [svelte()],
  server: {
    port: 5173,
    proxy: {
      "/api/stage": {
        target: "http://127.0.0.1:8001",
        changeOrigin: true,
      },
      "/api/presets": {
        target: "http://127.0.0.1:8001",
        changeOrigin: true,
      },
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
});

