import { readFileSync } from "node:fs";
import { encodeBase64Utf8 } from "../src/github-contents";

export const FIXTURE_TEXT = readFileSync(new URL("./fixtures/deadlines.json", import.meta.url), "utf8");

/** Fri Oct 9 2026, 10:15am Pacific (PDT, UTC-7). */
export const NOW = new Date("2026-10-09T17:15:00Z");
export const NOW_LOCAL = "2026-10-09T10:15";

export const TOKEN = "fake-repo-token-for-tests";

export type Call = { method: string; url: string; auth: string | null; body?: Record<string, string> };

/** GitHub returns base64 wrapped at 60 columns. */
function wrap(b64: string): string {
	return b64.replace(/(.{60})/g, "$1\n");
}

/**
 * A fake GitHub Contents API. `gets` are served in order (the last one
 * repeats); `puts` are HTTP statuses served in order (default 200).
 */
export function fakeGitHub(opts: { gets: { text: string; sha: string }[]; puts?: number[] }) {
	const calls: Call[] = [];
	let g = 0;
	let p = 0;
	const fetchFn = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
		const req = new Request(input, init);
		const call: Call = { auth: req.headers.get("Authorization"), method: req.method, url: req.url };
		if (req.method === "PUT") call.body = (await req.json()) as Record<string, string>;
		calls.push(call);
		if (req.method === "GET") {
			const f = opts.gets[Math.min(g++, opts.gets.length - 1)];
			return Response.json({ content: wrap(encodeBase64Utf8(f.text)), encoding: "base64", sha: f.sha, type: "file" });
		}
		if (req.method === "PUT") {
			const status = opts.puts?.[p++] ?? 200;
			if (status === 200) return Response.json({ commit: { sha: "c0ffee1234567" }, content: {} });
			return Response.json({ message: status === 409 ? "deadlines.json does not match s1" : "Server Error" }, { status });
		}
		return new Response("unexpected", { status: 500 });
	};
	return {
		calls,
		fetch: fetchFn as typeof fetch,
		gets: () => calls.filter((c) => c.method === "GET"),
		puts: () => calls.filter((c) => c.method === "PUT"),
	};
}
