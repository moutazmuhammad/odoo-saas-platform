import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath, URL } from "node:url";

// The app is served by Odoo from the saas_website module's static dir.
// `base` makes every built asset URL resolve under that static path, and
// `outDir` writes the build straight into the module so an Odoo restart
// picks it up. Override with VITE_STANDALONE=1 to build/dev at root "/".
//
// Path note (monorepo layout): this package lives at frontend/veltnex/, and
// saas_website is a sibling component at control-plane/saas_website/ — two
// levels up, not one, since the repo reorg moved veltnex out of the
// control-plane folder. See ../../README.md for the full layout.
const standalone = process.env.VITE_STANDALONE === "1";

export default defineConfig({
  base: standalone ? "/" : "/saas_website/static/spa/",
  plugins: [react()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  build: {
    outDir: fileURLToPath(
      new URL("../../control-plane/saas_website/static/spa", import.meta.url),
    ),
    emptyOutDir: true,
  },
  server: {
    // Local dev: proxy API + Odoo web endpoints to the running Odoo
    // server (port 8018) so the SPA can talk to real data without CORS.
    proxy: {
      "/saas/api": "http://localhost:8018",
      "/saas": "http://localhost:8018",
      "/web": "http://localhost:8018",
      "/my/invoices": "http://localhost:8018",
    },
  },
});
