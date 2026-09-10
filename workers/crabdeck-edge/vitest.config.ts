import { cloudflareTest } from "@cloudflare/vitest-plugin";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [
    cloudflareTest({
      wrangler: { configPath: "./wrangler.jsonc" },
      miniflare: {
        bindings: {
          EDGE_TOKEN: "test-edge-token",
          ORIGIN_URL: "http://127.0.0.1:9",
          VAULT_ORIGIN_URL: "http://127.0.0.1:9",
          ORCHESTRATOR_ORIGIN_URL: "http://127.0.0.1:9",
        },
      },
    }),
  ],
});
