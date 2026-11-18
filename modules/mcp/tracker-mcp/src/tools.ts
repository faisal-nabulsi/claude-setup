/**
 * The three MCP tools. Kept few and narrow on purpose: read the list, search
 * it, and mark one row done by exact id. There is no add, edit or delete tool.
 *
 * Every handler goes through guarded(), which re-checks the caller's GitHub
 * login against ALLOWED_GITHUB_LOGINS on every call before doing anything
 * else (layer 3 of the access check; see access.ts).
 */
import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { ACCESS_DENIED_MESSAGE, isAllowedLogin } from "./access";
import {
	cleanText,
	formatWhen,
	keyRows,
	localNow,
	Refusal,
	renderList,
	renderSearch,
	SafetyCheckFailed,
	searchRows,
	selectForList,
} from "./deadlines";
import { GitHubError, loadRows, markDone, type RepoConfig } from "./github-contents";

export type TrackerEnv = {
	ALLOWED_GITHUB_LOGINS?: string;
	GITHUB_TRACKER_TOKEN?: string;
	TRACKER_REPO?: string;
	TRACKER_BRANCH?: string;
	TRACKER_PATH?: string;
};

export type ToolContext = {
	/** GitHub login from the OAuth grant (this.props.login). */
	login: unknown;
	env: TrackerEnv;
	/** Test hooks. */
	fetch?: typeof fetch;
	clock?: () => Date;
};

type TextResult = { content: { type: "text"; text: string }[]; isError?: boolean };

const text = (t: string, isError = false): TextResult => ({
	content: [{ text: t, type: "text" }],
	...(isError ? { isError: true } : {}),
});

function repoConfig(ctx: ToolContext): RepoConfig {
	const token = ctx.env.GITHUB_TRACKER_TOKEN;
	if (!token) throw new Refusal("Server not configured: the GITHUB_TRACKER_TOKEN secret is missing.");
	return {
		branch: ctx.env.TRACKER_BRANCH || "main",
		fetch: ctx.fetch,
		path: ctx.env.TRACKER_PATH || "deadlines.json",
		repo: ctx.env.TRACKER_REPO || "YOUR-GITHUB-LOGIN/semester-heartbeat",
		token,
	};
}

/**
 * Wraps a tool body with the per-call allowlist check and uniform error
 * handling. registerTools only ever registers wrapped handlers.
 */
function guarded<A>(getCtx: () => ToolContext, body: (ctx: ToolContext, args: A) => Promise<string>) {
	return async (args: A): Promise<TextResult> => {
		const ctx = getCtx();
		if (!isAllowedLogin(ctx.login, ctx.env.ALLOWED_GITHUB_LOGINS)) {
			console.warn(`tool call refused for login ${JSON.stringify(cleanText(ctx.login ?? "(none)", 40))}`);
			return text(ACCESS_DENIED_MESSAGE, true);
		}
		try {
			return text(await body(ctx, args));
		} catch (e) {
			if (e instanceof Refusal || e instanceof SafetyCheckFailed || e instanceof GitHubError) {
				return text(e.message, true);
			}
			console.error("tool failed", e instanceof Error ? e.message : e);
			return text("Unexpected server error; nothing was changed. Check `wrangler tail` for details.", true);
		}
	};
}

export const TOOL_NAMES = ["todo_list", "todo_search", "todo_mark_done"] as const;

export function registerTools(server: McpServer, getCtx: () => ToolContext): void {
	server.registerTool(
		"todo_list",
		{
			annotations: { openWorldHint: false, readOnlyHint: true, title: "To-do list" },
			description:
				"the user's to-do list from their Berkeley tracker: items not yet done that are due in the next `days` days (default 7, max 30), plus anything that went past due in the last 3 days without being marked done. Soft targets (self-imposed, not real deadlines) are hidden unless include_soft is true. Each line shows the item's exact id, which todo_mark_done needs.",
			inputSchema: {
				days: z.number().int().min(1).max(30).optional().describe("How many days ahead to include (1-30, default 7)."),
				include_soft: z.boolean().optional().describe("Also show soft, self-imposed targets (default false)."),
			},
		},
		guarded(getCtx, async (ctx, args: { days?: number; include_soft?: boolean }) => {
			const rows = await loadRows(repoConfig(ctx));
			const opts = {
				days: args.days ?? 7,
				includeSoft: args.include_soft ?? false,
				now: localNow(ctx.clock?.() ?? new Date()),
			};
			return renderList(selectForList(await keyRows(rows), opts), opts);
		}),
	);

	server.registerTool(
		"todo_search",
		{
			annotations: { openWorldHint: false, readOnlyHint: true, title: "Search to-dos" },
			description:
				"Search every row of the tracker, done or not, by text in its label, its id, or its date (YYYY-MM-DD). Case-insensitive. Returns id, when, done/open and label for up to 25 matches. Use it to find the exact id before calling todo_mark_done.",
			inputSchema: {
				query: z.string().trim().min(2).max(100).describe("Text to look for, e.g. 'journal', 'C104', '2026-10-14'."),
			},
		},
		guarded(getCtx, async (ctx, args: { query: string }) => {
			const rows = await loadRows(repoConfig(ctx));
			return renderSearch(args.query, searchRows(await keyRows(rows), args.query));
		}),
	);

	server.registerTool(
		"todo_mark_done",
		{
			annotations: {
				destructiveHint: false,
				idempotentHint: true,
				openWorldHint: false,
				readOnlyHint: false,
				title: "Mark a to-do done",
			},
			description:
				"Mark ONE tracker item as done, by its exact id (from todo_list or todo_search). No fuzzy matching: an id that does not match exactly changes nothing. Done is permanent: it cannot be undone from here and the item will never be re-added. Refuses items that are already done. Only call this after the user says the item is finished.",
			inputSchema: {
				id: z
					.string()
					.trim()
					.min(1)
					.max(100)
					.regex(/^[\x21-\x7e]+$/, "ids are printable ASCII with no spaces")
					.describe("Exact id, e.g. 'assignment_9147805' or 'd-760a9021d9'."),
			},
		},
		guarded(getCtx, async (ctx, args: { id: string }) => {
			const result = await markDone(repoConfig(ctx), args.id, () => localNow(ctx.clock?.() ?? new Date()));
			return [
				`Marked done: ${cleanText(args.id, 80)} (${formatWhen(result.before.when)})`,
				`Label is now: ${cleanText(result.after.label)}`,
				`Committed to ${cleanText(ctx.env.TRACKER_REPO || "YOUR-GITHUB-LOGIN/semester-heartbeat", 80)}${result.commit ? ` as ${result.commit.slice(0, 7)}` : ""}${result.attempts > 1 ? " (after one retry: the file changed mid-write)" : ""}.`,
				"The Mac copy picks this up the next time semester-heartbeat is pulled and verify/sync_deadlines.py runs.",
			].join("\n");
		}),
	);
}
