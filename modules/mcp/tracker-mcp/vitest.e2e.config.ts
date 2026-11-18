import { defineConfig } from "vitest/config";

// End-to-end tests: start the real Worker in workerd (wrangler unstable_startWorker)
// with GitHub mocked. Slower than the unit tests, so they run separately.
export default defineConfig({
	test: {
		environment: "node",
		include: ["test-e2e/**/*.test.ts"],
		testTimeout: 30_000,
	},
});
