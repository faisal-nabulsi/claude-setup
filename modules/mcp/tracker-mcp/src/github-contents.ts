/**
 * Reads and writes deadlines.json through the GitHub Contents API, using a
 * fine-grained token that can only touch the one tracker repo.
 */
import {
	detectIndent,
	keyRows,
	parseRows,
	planMarkDone,
	type Row,
	SafetyCheckFailed,
	serializeRows,
	verifyMarkDone,
} from "./deadlines";

export type RepoConfig = {
	token: string;
	repo: string; // "owner/name"
	branch: string;
	path: string;
	fetch?: typeof fetch;
};

export class GitHubError extends Error {
	constructor(
		public status: number,
		message: string,
	) {
		super(message);
	}
}

const API = "https://api.github.com";

function contentsUrl(cfg: RepoConfig): string {
	if (!/^[A-Za-z0-9-]+\/[A-Za-z0-9._-]+$/.test(cfg.repo)) throw new Error("TRACKER_REPO must look like owner/name");
	const path = cfg.path.split("/").map(encodeURIComponent).join("/");
	return `${API}/repos/${cfg.repo}/contents/${path}`;
}

function headers(cfg: RepoConfig): Record<string, string> {
	return {
		Accept: "application/vnd.github+json",
		Authorization: `Bearer ${cfg.token}`,
		"User-Agent": "tracker-mcp",
		"X-GitHub-Api-Version": "2022-11-28",
	};
}

async function failure(resp: Response, what: string): Promise<GitHubError> {
	let detail = "";
	try {
		const body = (await resp.json()) as { message?: unknown };
		if (typeof body.message === "string") detail = `: ${body.message.slice(0, 160)}`;
	} catch {
		/* no JSON body */
	}
	return new GitHubError(resp.status, `GitHub ${what} failed (HTTP ${resp.status}${detail})`);
}

export function decodeBase64Utf8(b64: string): string {
	const bin = atob(b64.replace(/\s/g, ""));
	const bytes = new Uint8Array(bin.length);
	for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
	return new TextDecoder("utf-8", { fatal: true, ignoreBOM: false }).decode(bytes);
}

export function encodeBase64Utf8(text: string): string {
	const bytes = new TextEncoder().encode(text);
	let bin = "";
	for (let i = 0; i < bytes.length; i += 0x8000) {
		bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
	}
	return btoa(bin);
}

export async function getFile(cfg: RepoConfig): Promise<{ text: string; sha: string }> {
	const f = cfg.fetch ?? fetch;
	const resp = await f(`${contentsUrl(cfg)}?ref=${encodeURIComponent(cfg.branch)}`, { headers: headers(cfg) });
	if (!resp.ok) throw await failure(resp, "read");
	const body = (await resp.json()) as { type?: string; encoding?: string; content?: string; sha?: string };
	if (body.type !== "file" || body.encoding !== "base64" || typeof body.content !== "string" || !body.sha) {
		throw new GitHubError(resp.status, "GitHub returned something other than a base64 file for deadlines.json");
	}
	return { sha: body.sha, text: decodeBase64Utf8(body.content) };
}

export async function putFile(
	cfg: RepoConfig,
	text: string,
	sha: string,
	message: string,
): Promise<{ commit: string }> {
	const f = cfg.fetch ?? fetch;
	const resp = await f(contentsUrl(cfg), {
		body: JSON.stringify({ branch: cfg.branch, content: encodeBase64Utf8(text), message, sha }),
		headers: { ...headers(cfg), "Content-Type": "application/json" },
		method: "PUT",
	});
	if (!resp.ok) throw await failure(resp, "write");
	const body = (await resp.json()) as { commit?: { sha?: string } };
	return { commit: body.commit?.sha ?? "" };
}

export async function loadRows(cfg: RepoConfig): Promise<Row[]> {
	return parseRows((await getFile(cfg)).text);
}

export type MarkDoneResult = { before: Row; after: Row; commit: string; attempts: number };

/**
 * Marks exactly one row done and commits it. Fetches the file and its sha,
 * plans the change, re-verifies it, then PUTs with that sha so GitHub rejects
 * the write (409) if the file changed in between. On a 409 it refetches and
 * tries once more from scratch; a second 409 is reported, not retried.
 */
export async function markDone(cfg: RepoConfig, id: string, now: () => string): Promise<MarkDoneResult> {
	for (let attempt = 1; ; attempt++) {
		const { text, sha } = await getFile(cfg);
		const rows = parseRows(text);
		const plan = planMarkDone(rows, await keyRows(rows), id, now());

		const problems = verifyMarkDone(rows, plan.rows, plan.index, id);
		const out = serializeRows(plan.rows, detectIndent(text));
		if (JSON.stringify(JSON.parse(out)) !== JSON.stringify(plan.rows)) problems.push("serialized file does not round-trip");
		if (problems.length) throw new SafetyCheckFailed(problems);

		try {
			const { commit } = await putFile(cfg, out, sha, `tracker-mcp: mark done ${id}`);
			return { after: plan.after, attempts: attempt, before: plan.before, commit };
		} catch (e) {
			if (e instanceof GitHubError && e.status === 409 && attempt === 1) continue;
			throw e;
		}
	}
}
