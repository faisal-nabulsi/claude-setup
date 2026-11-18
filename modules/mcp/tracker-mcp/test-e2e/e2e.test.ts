/**
 * End-to-end in the real Workers runtime (local workerd via wrangler's
 * unstable_startWorker, using wrangler.jsonc): the full OAuth dance a Claude
 * client does (register, consent, GitHub sign-in, PKCE token exchange), then
 * real MCP calls through the OAuth provider, the /mcp gate and the Durable
 * Object. test-e2e/worker-entry.ts swaps the global fetch() for a stand-in
 * GitHub, so nothing leaves the machine and no Cloudflare or GitHub account
 * is touched.
 *
 * Run: npm run test:e2e
 */
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { unstable_startWorker } from "wrangler";

const CLIENT_REDIRECT = "https://client.example/callback";
let worker: Awaited<ReturnType<typeof unstable_startWorker>>;

beforeAll(async () => {
	const plain = (value: string) => ({ type: "plain_text" as const, value });
	worker = await unstable_startWorker({
		bindings: {
			COOKIE_ENCRYPTION_KEY: plain("e2e-cookie-key-0123456789abcdef"),
			GITHUB_CLIENT_ID: plain("e2e-client-id"),
			GITHUB_CLIENT_SECRET: plain("e2e-client-secret"),
			GITHUB_TRACKER_TOKEN: plain("e2e-repo-token"),
		},
		config: "wrangler.jsonc",
		dev: { inspector: false, persist: false, server: { port: 0 } },
		entrypoint: "test-e2e/worker-entry.ts",
	});
	await worker.ready;
}, 120_000);

afterAll(async () => {
	await worker?.dispose();
});

async function mockGitHubSignIn(login: string) {
	await worker.fetch(`https://tracker-mcp.example.workers.dev/__test/login?as=${encodeURIComponent(login)}`);
}

const b64url = (bytes: ArrayBuffer | Uint8Array) =>
	btoa(String.fromCharCode(...new Uint8Array(bytes)))
		.replace(/\+/g, "-")
		.replace(/\//g, "_")
		.replace(/=+$/, "");
const cookiePair = (setCookie: string | null) => (setCookie ?? "").split(";")[0];
const ORIGIN = "https://tracker-mcp.example.workers.dev";

/** Register a client, consent, bounce through (mocked) GitHub. Returns the /callback response. */
async function signIn(login: string) {
	const reg = await worker.fetch(`${ORIGIN}/register`, {
		body: JSON.stringify({ client_name: "e2e client", redirect_uris: [CLIENT_REDIRECT], token_endpoint_auth_method: "none" }),
		headers: { "content-type": "application/json" },
		method: "POST",
	});
	expect(reg.status).toBe(201);
	const { client_id } = (await reg.json()) as { client_id: string };

	const verifier = b64url(crypto.getRandomValues(new Uint8Array(32)));
	const challenge = b64url(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)));
	const authorize = new URL(`${ORIGIN}/authorize`);
	for (const [k, v] of Object.entries({
		client_id,
		code_challenge: challenge,
		code_challenge_method: "S256",
		redirect_uri: CLIENT_REDIRECT,
		response_type: "code",
		state: "client-state",
	}))
		authorize.searchParams.set(k, v);

	const dialog = await worker.fetch(authorize.href);
	expect(dialog.status).toBe(200);
	const html = await dialog.text();
	expect(html).toContain("the user&#039;s tracker");
	const csrf = /name="csrf_token" value="([^"]+)"/.exec(html)?.[1];
	const state = /name="state" value="([^"]+)"/.exec(html)?.[1];
	expect(csrf && state).toBeTruthy();

	const approve = await worker.fetch(`${ORIGIN}/authorize`, {
		body: new URLSearchParams({ csrf_token: csrf!, state: state! }),
		headers: { cookie: cookiePair(dialog.headers.get("set-cookie")) },
		method: "POST",
		redirect: "manual",
	});
	expect(approve.status).toBe(302);
	const toGitHub = new URL(approve.headers.get("location")!);
	expect(toGitHub.origin + toGitHub.pathname).toBe("https://github.com/login/oauth/authorize");
	expect(toGitHub.searchParams.has("scope")).toBe(false); // identity only, no GitHub scopes
	const consented = approve.headers
		.getSetCookie()
		.map(cookiePair)
		.find((c) => c.startsWith("__Host-CONSENTED_STATE="))!;

	await mockGitHubSignIn(login);
	const callback = await worker.fetch(
		`${ORIGIN}/callback?code=gh-code&state=${toGitHub.searchParams.get("state")}`,
		{ headers: { cookie: consented }, redirect: "manual" },
	);
	return { callback, client_id, verifier };
}

async function exchange(code: string, client_id: string, verifier: string) {
	const res = await worker.fetch(`${ORIGIN}/token`, {
		body: new URLSearchParams({ client_id, code, code_verifier: verifier, grant_type: "authorization_code", redirect_uri: CLIENT_REDIRECT }),
		headers: { "content-type": "application/x-www-form-urlencoded" },
		method: "POST",
	});
	expect(res.status).toBe(200);
	return ((await res.json()) as { access_token: string }).access_token;
}

/** One JSON-RPC call over streamable HTTP; handles JSON or SSE replies. */
async function rpc(token: string, session: string | null, body: object) {
	const res = await worker.fetch(`${ORIGIN}/mcp`, {
		body: JSON.stringify(body),
		headers: {
			accept: "application/json, text/event-stream",
			authorization: `Bearer ${token}`,
			"content-type": "application/json",
			...(session ? { "mcp-session-id": session } : {}),
		},
		method: "POST",
	});
	const raw = await res.text();
	let data: any;
	if (res.headers.get("content-type")?.includes("text/event-stream")) {
		data = raw
			.split("\n")
			.filter((l) => l.startsWith("data:"))
			.map((l) => JSON.parse(l.slice(5)))
			.find((m) => "result" in m || "error" in m);
	} else if (res.headers.get("content-type")?.includes("application/json")) {
		data = JSON.parse(raw);
	}
	return { data, raw, res, session: res.headers.get("mcp-session-id") ?? session };
}

describe("end to end in workerd", () => {
	it("serves OAuth metadata and refuses /mcp without a token", async () => {
		const meta = await worker.fetch(`${ORIGIN}/.well-known/oauth-authorization-server`);
		expect(meta.status).toBe(200);
		const m = (await meta.json()) as Record<string, unknown>;
		expect(String(m.registration_endpoint)).toMatch(/\/register$/);
		expect(m.code_challenge_methods_supported).toEqual(["S256"]);
		// Claude's default connector identity (a Client ID Metadata Document) is accepted.
		expect(m.client_id_metadata_document_supported).toBe(true);
		const anon = await worker.fetch(`${ORIGIN}/mcp`, { body: "{}", method: "POST" });
		expect(anon.status).toBe(401);
		const bogus = await worker.fetch(`${ORIGIN}/mcp`, { body: "{}", headers: { authorization: "Bearer nope" }, method: "POST" });
		expect(bogus.status).toBe(401);
	});

	it("a non-allowlisted GitHub login is stopped at the callback with no code issued", async () => {
		const { callback } = await signIn("mallory");
		expect(callback.status).toBe(403);
		expect(callback.headers.get("location")).toBeNull();
		expect(await callback.text()).toContain("Access denied");
	});

	it("the allowlisted login gets a token and can list, search and mark done", async () => {
		const { callback, client_id, verifier } = await signIn("YOUR-GITHUB-LOGIN");
		expect(callback.status).toBe(302);
		const back = new URL(callback.headers.get("location")!);
		expect(back.origin + back.pathname).toBe(CLIENT_REDIRECT);
		expect(back.searchParams.get("state")).toBe("client-state");
		const token = await exchange(back.searchParams.get("code")!, client_id, verifier);

		let r = await rpc(token, null, {
			id: 1,
			jsonrpc: "2.0",
			method: "initialize",
			params: { capabilities: {}, clientInfo: { name: "e2e", version: "0" }, protocolVersion: "2025-06-18" },
		});
		expect(r.res.status).toBe(200);
		expect(r.data.result.serverInfo.name).toBe("the user's tracker");
		const session = r.session;
		await rpc(token, session, { jsonrpc: "2.0", method: "notifications/initialized" });

		r = await rpc(token, session, { id: 2, jsonrpc: "2.0", method: "tools/list" });
		expect(r.data.result.tools.map((t: { name: string }) => t.name).sort()).toEqual([
			"todo_list",
			"todo_mark_done",
			"todo_search",
		]);

		r = await rpc(token, session, { id: 3, jsonrpc: "2.0", method: "tools/call", params: { arguments: { days: 7 }, name: "todo_list" } });
		expect(r.data.result.isError).toBeFalsy();
		expect(r.data.result.content[0].text).toContain("| id: assignment_1 | DATA C104 HW 4");

		r = await rpc(token, session, { id: 5, jsonrpc: "2.0", method: "tools/call", params: { arguments: { query: "c104" }, name: "todo_search" } });
		expect(r.data.result.content[0].text).toContain("- id: d-far0000001 | 2026-10-20T11:00");

		r = await rpc(token, session, {
			id: 4,
			jsonrpc: "2.0",
			method: "tools/call",
			params: { arguments: { id: "assignment_1" }, name: "todo_mark_done" },
		});
		expect(r.data.result.isError).toBeFalsy();
		expect(r.data.result.content[0].text).toContain("Marked done: assignment_1");
		expect(r.data.result.content[0].text).toContain("as e2e-put"); // the PUT reached the GitHub stand-in

		// Nothing went anywhere but the GitHub stand-in, and repo calls used the repo token.
		const outbound = (await (await worker.fetch(`${ORIGIN}/__test/outbound`)).json()) as string[];
		expect(outbound.every((l) => / https:\/\/(api\.)?github\.com\//.test(l))).toBe(true);
		const repoCalls = outbound.filter((l) => l.includes("/contents/"));
		expect(repoCalls.length).toBeGreaterThanOrEqual(3);
		expect(repoCalls.every((l) => l.endsWith("auth=Bearer e2e-repo-token"))).toBe(true);
		expect(outbound.filter((l) => l.includes("/contents/") && l.startsWith("PUT"))).toHaveLength(1);

		// Emptying ALLOWED_GITHUB_LOGINS locks out this already-issued token at the /mcp gate.
		await worker.patchConfig({ bindings: { ALLOWED_GITHUB_LOGINS: { type: "plain_text", value: "" } } });
		r = await rpc(token, session, { id: 6, jsonrpc: "2.0", method: "tools/list" });
		expect(r.res.status).toBe(403);
		expect(r.raw).toContain("Access denied");
	}, 30_000);
});
