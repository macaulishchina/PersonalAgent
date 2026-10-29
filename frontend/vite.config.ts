import { defineConfig } from "vite";
export default defineConfig({
  base: "/chat/",
  build: {
    outDir: "../src/assistant_runtime/plugins/web/static",
    emptyOutDir: true,
  },
  server: { proxy: { "/chat/api": "http://127.0.0.1:8047" } },
});
