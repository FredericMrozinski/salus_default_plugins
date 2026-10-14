import { resolve } from "node:path";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// One page per frontend component, one shared source folder (src/shared).
// base "./" makes every asset URL relative: Salus serves plugins under /plugins/<absolute path>/.
export default defineConfig({
  base: "./",
  plugins: [react()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
    target: "es2022",
    rollupOptions: {
      input: {
        browser: resolve(__dirname, "browser/index.html"),
        "dataset-viewer": resolve(__dirname, "dataset-viewer/index.html"),
        "entry-editor": resolve(__dirname, "entry-editor/index.html"),
      },
    },
  },
});
