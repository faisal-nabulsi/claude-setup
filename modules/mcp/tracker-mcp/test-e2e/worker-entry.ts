/**
 * Test-only entry point for the end-to-end test. It is the real Worker
 * (src/index.ts), with the global fetch() swapped for a stand-in GitHub, so
 * no request leaves the machine. Production never loads this file.
 */
import { type Row, serializeRows } from "../src/deadlines";
import { encodeBase64Utf8 } from "../src/github-contents";
import fixtureRows from "../test/fixtures/deadlines.json";

export { TrackerMCP } from "../src/index";
import worker from "../src/index";

const REPO_PATH = "/repos/YOUR-GITHUB-LOGIN/semester-heartbeat/contents/deadlines.json";
const FIXTURE_TEXT = serializeRows(fixtureRows as Row[], 2); // byte-identical to the fixture file (see unit tests)
const state = { login: "nobody", outbound: [] as string[] };

globalThis.fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
	const req = new Request(input, init);
	const url = new URL(req.url);
	state.outbound.push(`${req.method} ${url.origin}${url.pathname} auth=${req.headers.get("authorization") ?? "-"}`);
	if (req.method === "POST" && url.href === "https://github.com/login/oauth/access_token") {
		return new Response("access_token=fake-user-token&scope=&token_type=bearer", {
			headers: { "content-type": "application/x-www-form-urlencoded" },
		});
	}
	if (req.method === "GET" && url.href === "https://api.github.com/user") {
		return Response.json({ id: 1, login: state.login, name: state.login });
	}
	if (url.origin === "https://api.github.com" && url.pathname === REPO_PATH) {
		if (req.method === "GET") {
			return Response.json({ content: encodeBase64Utf8(FIXTURE_TEXT), encoding: "base64", sha: "s1", type: "file" });
		}
		if (req.method === "PUT") {
			const body = (await req.json()) as { sha?: string };
			return Response.json({ commit: { sha: `e2e-put-with-${body.sha}` }, content: {} });
		}
	}
	return new Response(`unmocked outbound request: ${req.method} ${url.href}`, { status: 599 });
};

export default {
	async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
		const url = new URL(request.url);
		// Test controls, reachable only in this test build.
		if (url.pathname === "/__test/login") {
			state.login = url.searchParams.get("as") ?? "nobody";
			return new Response("ok");
		}
		if (url.pathname === "/__test/outbound") return Response.json(state.outbound);
		return worker.fetch(request, env, ctx);
	},
};
