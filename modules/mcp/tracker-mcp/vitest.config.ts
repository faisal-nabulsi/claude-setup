import { defineConfig } from "vitest/config";

// Tests run in plain Node: the tested modules (deadlines, GitHub client,
// access checks, tool registration, OAuth callback) use only Web APIs.
// src/index.ts (Durable Object + OAuthProvider) needs the Workers runtime and
// is covered by `wrangler deploy --dry-run` instead.
export default defineConfig({
	test: {
		environment: "node",
		include: ["test/**/*.test.ts"],
	},
});
