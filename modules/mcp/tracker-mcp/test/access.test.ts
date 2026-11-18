/**
 * The allowlist, proven at every layer: a GitHub login that is not on
 * ALLOWED_GITHUB_LOGINS gets no MCP token at sign-in (layer 1), is refused at
 * the /mcp gate (layer 2), and is refused by every tool handler (layer 3).
 */
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ACCESS_DENIED_MESSAGE, gateApiHandler, isAllowedLogin, parseAllowlist } from "../src/access";
import { GitHubHandler } from "../src/github-handler";
import { registerTools, TOOL_NAMES, type ToolContext, type TrackerEnv } from "../src/tools";
import { bindStateToSession, createOAuthState } from "../src/workers-oauth-utils";
import { FIXTURE_TEXT, fakeGitHub, NOW, TOKEN } from "./helpers";

afterEach(() => {
	vi.unstubAllGlobals();
	vi.restoreAllMocks();
});

describe("allowlist parsing", () => {
	it("defaults to YOUR-GITHUB-LOGIN only when the variable is missing", () => {
		expect([...parseAllowlist(undefined)]).toEqual(["YOUR-GITHUB-LOGIN"]);
		expect(isAllowedLogin("YOUR-GITHUB-LOGIN", undefined)).toBe(true);
		expect(isAllowedLogin("mallory", undefined)).toBe(false);
	});

	it("is case-insensitive (GitHub logins are) and accepts comma or space lists", () => {
		expect(isAllowedLogin("Your-GitHub-Login", "YOUR-GITHUB-LOGIN")).toBe(true);
		expect(isAllowedLogin("bob", "YOUR-GITHUB-LOGIN, bob")).toBe(true);
		expect(isAllowedLogin("bob", "YOUR-GITHUB-LOGIN bob")).toBe(true);
	});

	it("fails closed: empty list, missing or odd logins, and look-alikes are refused", () => {
		expect(isAllowedLogin("YOUR-GITHUB-LOGIN", "")).toBe(false);
		expect(isAllowedLogin("YOUR-GITHUB-LOGIN", "  ,  ")).toBe(false);
		expect(isAllowedLogin(undefined, "YOUR-GITHUB-LOGIN")).toBe(false);
		expect(isAllowedLogin("", "YOUR-GITHUB-LOGIN")).toBe(false);
		expect(isAllowedLogin({ login: "YOUR-GITHUB-LOGIN" }, "YOUR-GITHUB-LOGIN")).toBe(false);
		expect(isAllowedLogin("YOUR-GITHUB-LOGIN2", "YOUR-GITHUB-LOGIN")).toBe(false);
		expect(isAllowedLogin("someone-else", "YOUR-GITHUB-LOGIN")).toBe(false);
	});
});

// ------------------------------------------------------------------ layer 1

function memoryKV() {
	const store = new Map<string, string>();
	return {
		delete: async (k: string) => void store.delete(k),
		get: async (k: string) => store.get(k) ?? null,
		put: async (k: string, v: string) => void store.set(k, v),
		store,
	};
}

/**
 * Drives the real /callback route: a genuine state token + session cookie,
 * GitHub's token endpoint and /user stubbed to report `login`.
 */
async function runCallback(login: string, allowlist: string | undefined) {
	const kv = memoryKV();
	const oauthReqInfo = {
		clientId: "claude-client",
		codeChallenge: "x",
		codeChallengeMethod: "S256",
		redirectUri: "https://claude.ai/api/mcp/auth_callback",
		responseType: "code",
		scope: [],
		state: "client-state",
	};
	const { stateToken } = await createOAuthState(oauthReqInfo as never, kv as never);
	const { setCookie } = await bindStateToSession(stateToken);

	const githubCalls: string[] = [];
	vi.stubGlobal("fetch", async (input: RequestInfo | URL) => {
		const url = new Request(input).url;
		githubCalls.push(url);
		if (url === "https://github.com/login/oauth/access_token") {
			return new Response("access_token=fake-user-token&scope=&token_type=bearer", {
				headers: { "Content-Type": "application/x-www-form-urlencoded" },
			});
		}
		if (url === "https://api.github.com/user") return Response.json({ id: 42, login, name: "Someone" });
		return new Response("unexpected", { status: 500 });
	});

	const completeAuthorization = vi.fn(async () => ({
		redirectTo: "https://claude.ai/api/mcp/auth_callback?code=issued&state=client-state",
	}));
	const env = {
		ALLOWED_GITHUB_LOGINS: allowlist,
		COOKIE_ENCRYPTION_KEY: "test-cookie-key",
		GITHUB_CLIENT_ID: "test-client-id",
		GITHUB_CLIENT_SECRET: "test-client-secret",
		OAUTH_KV: kv,
		OAUTH_PROVIDER: { completeAuthorization },
	};
	const request = new Request(`https://tracker-mcp.example.workers.dev/callback?code=gh-code&state=${stateToken}`, {
		headers: { Cookie: setCookie.split(";")[0] },
	});
	const response = await GitHubHandler.fetch(request, env as never);
	return { completeAuthorization, githubCalls, response };
}

describe("layer 1: OAuth callback", () => {
	it("refuses a GitHub login that is not on the allowlist and issues no token", async () => {
		const { response, completeAuthorization, githubCalls } = await runCallback("mallory", "YOUR-GITHUB-LOGIN");
		expect(response.status).toBe(403);
		expect(await response.text()).toContain("Access denied");
		expect(response.headers.get("Location")).toBeNull();
		expect(completeAuthorization).not.toHaveBeenCalled(); // no grant, no code, no MCP token
		expect(githubCalls).toEqual(["https://github.com/login/oauth/access_token", "https://api.github.com/user"]);
	});

	it("refuses everyone, the user included, when the allowlist is empty", async () => {
		const { response, completeAuthorization } = await runCallback("YOUR-GITHUB-LOGIN", "");
		expect(response.status).toBe(403);
		expect(completeAuthorization).not.toHaveBeenCalled();
	});

	it("lets the allowlisted login through, without storing a GitHub token in the grant", async () => {
		const { response, completeAuthorization } = await runCallback("YOUR-GITHUB-LOGIN", "YOUR-GITHUB-LOGIN");
		expect(response.status).toBe(302);
		expect(response.headers.get("Location")).toBe("https://claude.ai/api/mcp/auth_callback?code=issued&state=client-state");
		expect(completeAuthorization).toHaveBeenCalledTimes(1);
		const opts = (completeAuthorization.mock.calls[0] as unknown[])[0] as { props: Record<string, unknown>; userId: string };
		expect(opts.userId).toBe("YOUR-GITHUB-LOGIN");
		expect(opts.props).toStrictEqual({ login: "YOUR-GITHUB-LOGIN", name: "Someone" });
	});
});

// ------------------------------------------------------------------ layer 2

describe("layer 2: /mcp gate", () => {
	const inner = () => ({ fetch: vi.fn(async () => new Response("mcp ok")) });
	const ctxWith = (props: unknown) => ({ props }) as unknown as ExecutionContext;
	const req = () => new Request("https://tracker-mcp.example.workers.dev/mcp", { method: "POST" });

	it("refuses a token whose login is not allowlisted, before the MCP server sees it", async () => {
		const h = inner();
		const res = await gateApiHandler(h).fetch(req(), { ALLOWED_GITHUB_LOGINS: "YOUR-GITHUB-LOGIN" }, ctxWith({ login: "mallory" }));
		expect(res.status).toBe(403);
		expect(await res.text()).toBe(ACCESS_DENIED_MESSAGE);
		expect(h.fetch).not.toHaveBeenCalled();
	});

	it("refuses a missing login, and an old token once the allowlist is emptied", async () => {
		const h = inner();
		const gate = gateApiHandler(h);
		expect((await gate.fetch(req(), { ALLOWED_GITHUB_LOGINS: "YOUR-GITHUB-LOGIN" }, ctxWith(undefined))).status).toBe(403);
		expect((await gate.fetch(req(), { ALLOWED_GITHUB_LOGINS: "" }, ctxWith({ login: "YOUR-GITHUB-LOGIN" }))).status).toBe(403);
		expect(h.fetch).not.toHaveBeenCalled();
	});

	it("passes the allowlisted login through", async () => {
		const h = inner();
		const res = await gateApiHandler(h).fetch(req(), { ALLOWED_GITHUB_LOGINS: "YOUR-GITHUB-LOGIN" }, ctxWith({ login: "YOUR-GITHUB-LOGIN" }));
		expect(await res.text()).toBe("mcp ok");
		expect(h.fetch).toHaveBeenCalledTimes(1);
	});
});

// ------------------------------------------------------------------ layer 3

const BASE_ENV: TrackerEnv = {
	ALLOWED_GITHUB_LOGINS: "YOUR-GITHUB-LOGIN",
	GITHUB_TRACKER_TOKEN: TOKEN,
	TRACKER_BRANCH: "main",
	TRACKER_PATH: "deadlines.json",
	TRACKER_REPO: "YOUR-GITHUB-LOGIN/semester-heartbeat",
};

/** A real MCP client talking to the real registered tools over an in-memory transport. */
async function connect(ctx: Omit<ToolContext, "fetch" | "clock">) {
	const gh = fakeGitHub({ gets: [{ sha: "s1", text: FIXTURE_TEXT }] });
	const server = new McpServer({ name: "test", version: "0" });
	registerTools(server, () => ({ ...ctx, clock: () => NOW, fetch: gh.fetch }));
	const [a, b] = InMemoryTransport.createLinkedPair();
	const client = new Client({ name: "test-client", version: "0" });
	await Promise.all([server.connect(a), client.connect(b)]);
	return { client, gh };
}

const VALID_ARGS: Record<(typeof TOOL_NAMES)[number], Record<string, unknown>> = {
	todo_list: { days: 7 },
	todo_mark_done: { id: "assignment_1" },
	todo_search: { query: "C104" },
};

const textOf = (r: unknown) => ((r as { content: { text: string }[] }).content[0]?.text ?? "") as string;

describe("layer 3: every tool handler", () => {
	it("exposes exactly three tools: no add, edit or delete", async () => {
		const { client } = await connect({ env: BASE_ENV, login: "YOUR-GITHUB-LOGIN" });
		const { tools } = await client.listTools();
		expect(tools.map((t) => t.name).sort()).toEqual([...TOOL_NAMES].sort());
	});

	for (const [who, login, env] of [
		["a non-allowlisted login", "mallory", BASE_ENV],
		["a missing login", undefined, BASE_ENV],
		["the user after the allowlist is emptied", "YOUR-GITHUB-LOGIN", { ...BASE_ENV, ALLOWED_GITHUB_LOGINS: "" }],
	] as const) {
		it(`refuses ${who} in every tool, without touching GitHub`, async () => {
			const { client, gh } = await connect({ env, login });
			const { tools } = await client.listTools();
			for (const tool of tools) {
				const res = await client.callTool({
					arguments: VALID_ARGS[tool.name as keyof typeof VALID_ARGS],
					name: tool.name,
				});
				expect(res.isError, tool.name).toBe(true);
				expect(textOf(res), tool.name).toBe(ACCESS_DENIED_MESSAGE);
			}
			expect(gh.calls).toHaveLength(0);
		});
	}

	it("serves the allowlisted login", async () => {
		const { client, gh } = await connect({ env: BASE_ENV, login: "YOUR-GITHUB-LOGIN" });
		const res = await client.callTool({ arguments: { days: 7 }, name: "todo_list" });
		expect(res.isError).toBeFalsy();
		expect(textOf(res)).toContain("| id: assignment_1 | DATA C104 HW 4");
		expect(gh.gets()).toHaveLength(1);
		expect(gh.puts()).toHaveLength(0);
	});

	it("marks done through the tool and reports a refusal as an error, not a crash", async () => {
		const { client, gh } = await connect({ env: BASE_ENV, login: "YOUR-GITHUB-LOGIN" });
		const ok = await client.callTool({ arguments: { id: "assignment_1" }, name: "todo_mark_done" });
		expect(ok.isError).toBeFalsy();
		expect(textOf(ok)).toContain("Marked done: assignment_1 (Sat 10/10 11:59pm)");
		expect(gh.puts()).toHaveLength(1);
		const fuzzy = await client.callTool({ arguments: { id: "C104" }, name: "todo_mark_done" });
		expect(fuzzy.isError).toBe(true);
		expect(textOf(fuzzy)).toContain("No row has the exact id");
		expect(gh.puts()).toHaveLength(1);
	});

	it("rejects out-of-range arguments at the schema", async () => {
		const { client, gh } = await connect({ env: BASE_ENV, login: "YOUR-GITHUB-LOGIN" });
		const res = await client.callTool({ arguments: { days: 31 }, name: "todo_list" });
		expect(res.isError).toBe(true);
		const spaced = await client.callTool({ arguments: { id: "assignment 1" }, name: "todo_mark_done" });
		expect(spaced.isError).toBe(true);
		expect(gh.calls).toHaveLength(0);
	});
});
