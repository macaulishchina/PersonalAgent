import { defineConfig } from "vite";
export default defineConfig({
  base: "./",
  build: {
    outDir: "../src/pa_configuration_demo/management",
    emptyOutDir: true,
  },
});
