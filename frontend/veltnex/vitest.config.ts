import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { fileURLToPath, URL } from "node:url";

// Separate from vite.config.ts (which sets a non-root `base` and an
// outDir that writes into the Odoo module — neither makes sense for a
// test run) but reuses the same "@" -> ./src alias so imports resolve
// identically to the app build.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: false,
    coverage: {
      // B.5: report a number in CI, not gate on one yet.
      provider: "v8",
      reporter: ["text", "text-summary"],
      include: ["src/**/*.{ts,tsx}"],
      exclude: ["src/**/*.test.{ts,tsx}", "src/test/**"],
    },
  },
});
