import { defineConfig } from "vite";
export default defineConfig({
  base: "./",
  build: {
    outDir: "../management",
    emptyOutDir: true,
  },
});
