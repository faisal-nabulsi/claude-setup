import { describe, expect, it } from "vitest";
import {
	doneLabel,
	keyRows,
	parseRows,
	planMarkDone,
	type Row,
	Refusal,
	serializeRows,
	verifyMarkDone,
} from "../src/deadlines";
import { decodeBase64Utf8, GitHubError, markDone, type RepoConfig } from "../src/github-contents";
import { FIXTURE_TEXT, fakeGitHub, NOW_LOCAL, TOKEN } from "./helpers";

const cfg = (f: typeof fetch): RepoConfig => ({
	branch: "main",
	fetch: f,
	path: "deadlines.json",
	repo: "YOUR-GITHUB-LOGIN/semester-heartbeat",
	token: TOKEN,
});
const clock = () => NOW_LOCAL;
const original = () => parseRows(FIXTURE_TEXT);
const putRows = (body: Record<string, string> | undefined): Row[] => parseRows(decodeBase64Utf8(body!.content));

/** The one contiguous block of lines that differs between two texts. */
function hunk(a: string, b: string) {
	const x = a.split("\n");
	const y = b.split("\n");
	let pre = 0;
	while (pre < x.length && x[pre] === y[pre]) pre++;
	let suf = 0;
	while (suf < x.length - pre && suf < y.length - pre && x[x.length - 1 - suf] === y[y.length - 1 - suf]) suf++;
	return { added: y.slice(pre, y.length - suf), removed: x.slice(pre, x.length - suf) };
}

/** Every row except `index` must be identical, and the count unchanged. */
function expectOnlyRowChanged(before: Row[], after: Row[], index: number) {
	expect(after).toHaveLength(before.length);
	before.forEach((r, i) => {
		if (i !== index) expect(after[i]).toStrictEqual(r);
	});
}

describe("done label (same rule as mark_done.py)", () => {
	it("prefixes DONE m/d and appends DO NOT RE-ADD once", () => {
		expect(doneLabel("DATA C104 HW 4", NOW_LOCAL)).toBe("DONE 10/9 — DATA C104 HW 4 DO NOT RE-ADD.");
		expect(doneLabel("DONE early — x", NOW_LOCAL)).toBe("DONE early — x DO NOT RE-ADD.");
		expect(doneLabel("x DO NOT RE-ADD.", NOW_LOCAL)).toBe("DONE 10/9 — x DO NOT RE-ADD.");
		expect(doneLabel("trailing   ", NOW_LOCAL)).toBe("DONE 10/9 — trailing DO NOT RE-ADD.");
	});
});

describe("planMarkDone / verifyMarkDone (merge safety)", () => {
	it("changes exactly one row and keeps the count", async () => {
		const rows = original();
		const plan = planMarkDone(rows, await keyRows(rows), "assignment_1", NOW_LOCAL);
		expectOnlyRowChanged(rows, plan.rows, plan.index);
		expect(plan.after).toStrictEqual({
			canvas_id: "assignment_1",
			done: true,
			done_at: "2026-10-09T10:15",
			id: "assignment_1",
			label: "DONE 10/9 — DATA C104 HW 4 DO NOT RE-ADD.",
			when: "2026-10-10T23:59",
		});
		expect(verifyMarkDone(rows, plan.rows, plan.index, "assignment_1")).toEqual([]);
		// key order matches Python's {**r, "label": ..., "done": True, "done_at": ...}
		expect(Object.keys(plan.after)).toEqual(["when", "label", "canvas_id", "id", "done", "done_at"]);
	});

	it("never flips a done row back, whichever open row is marked", async () => {
		const rows = original();
		const keyed = await keyRows(rows);
		const doneBefore = rows.map((r) => Boolean(r.done));
		for (const k of keyed.filter((x) => !x.row.done)) {
			const plan = planMarkDone(rows, keyed, k.id, NOW_LOCAL);
			plan.rows.forEach((r, i) => {
				if (doneBefore[i]) expect(r.done).toBe(true);
			});
			expectOnlyRowChanged(rows, plan.rows, plan.index);
		}
	});

	it("writes the derived id into a row that had none (so the Mac merge matches it)", async () => {
		const rows = original();
		const plan = planMarkDone(rows, await keyRows(rows), "d-f79da81707", NOW_LOCAL);
		expect(plan.after.id).toBe("d-f79da81707");
		expect(plan.after.label).toBe("DONE 10/9 — SOCIOL journal — entry #3 ✍️ DO NOT RE-ADD.");
		expectOnlyRowChanged(rows, plan.rows, plan.index);
	});

	it("refuses unknown, partial, label-text and already-done ids (no fuzzy writes)", async () => {
		const rows = original();
		const keyed = await keyRows(rows);
		for (const bad of ["assignment", "assignment_", "C104", "DATA C104 HW 4", "ASSIGNMENT_1", "nope"]) {
			expect(() => planMarkDone(rows, keyed, bad, NOW_LOCAL)).toThrow(/No row has the exact id/);
		}
		expect(() => planMarkDone(rows, keyed, "assignment_9147805", NOW_LOCAL)).toThrow(/Already done/);
	});

	it("refuses an id shared by two rows", async () => {
		const rows = [...original(), { id: "assignment_1", label: "dup", when: "2026-10-30T09:00" }];
		await expect(keyRows(rows).then((k) => planMarkDone(rows, k, "assignment_1", NOW_LOCAL))).rejects.toThrow(
			/2 rows share the id/,
		);
	});

	it("catches a removed row, a changed row, and an un-done row", () => {
		const rows = original();
		const marked = rows.map((r, i) => (i === 4 ? { ...r, done: true, done_at: NOW_LOCAL } : r));
		expect(verifyMarkDone(rows, marked.slice(0, -1), 4, "assignment_1")).toContain("row count changed (10 -> 9)");
		const changed = marked.map((r, i) => (i === 2 ? { ...r, label: "edited" } : r));
		expect(verifyMarkDone(rows, changed, 4, "assignment_1")).toContain("row 2 would change");
		const undone = marked.map((r, i) => (i === 1 ? { ...r, done: false } : r));
		expect(verifyMarkDone(rows, undone, 4, "assignment_1")).toContain("row 1 would flip from done to not-done");
	});
});

describe("markDone against the GitHub Contents API", () => {
	it("GETs the file, PUTs it back with the same sha, one row changed, nothing else", async () => {
		const gh = fakeGitHub({ gets: [{ sha: "s1", text: FIXTURE_TEXT }] });
		const res = await markDone(cfg(gh.fetch), "assignment_1", clock);

		expect(gh.gets()).toHaveLength(1);
		expect(gh.gets()[0].url).toBe(
			"https://api.github.com/repos/YOUR-GITHUB-LOGIN/semester-heartbeat/contents/deadlines.json?ref=main",
		);
		expect(gh.puts()).toHaveLength(1);
		const put = gh.puts()[0];
		expect(put.body?.sha).toBe("s1");
		expect(put.body?.branch).toBe("main");
		expect(put.body?.message).toBe("tracker-mcp: mark done assignment_1");
		expect(gh.calls.every((c) => c.auth === `Bearer ${TOKEN}`)).toBe(true);

		const before = original();
		const after = putRows(put.body);
		expectOnlyRowChanged(before, after, 4);
		expect(after[4].done).toBe(true);
		// The written file is the original file with only that row's lines different.
		const written = decodeBase64Utf8(put.body!.content);
		expect(written).toBe(serializeRows(after, 2));
		expect(hunk(FIXTURE_TEXT, written)).toEqual({
			added: [
				'    "label": "DONE 10/9 — DATA C104 HW 4 DO NOT RE-ADD.",',
				'    "canvas_id": "assignment_1",',
				'    "id": "assignment_1",',
				'    "done": true,',
				'    "done_at": "2026-10-09T10:15"',
			],
			removed: ['    "label": "DATA C104 HW 4",', '    "canvas_id": "assignment_1",', '    "id": "assignment_1"'],
		});
		expect(res).toMatchObject({ attempts: 1, commit: "c0ffee1234567" });
	});

	it("on 409, refetches and retries once with the new sha, keeping rows added meanwhile", async () => {
		const meanwhile = [...original(), { canvas_id: "assignment_3", id: "assignment_3", label: "New Canvas row", when: "2026-10-15T23:59" }];
		const v2 = serializeRows(meanwhile, 2);
		const gh = fakeGitHub({ gets: [{ sha: "s1", text: FIXTURE_TEXT }, { sha: "s2", text: v2 }], puts: [409, 200] });

		const res = await markDone(cfg(gh.fetch), "assignment_1", clock);

		expect(res.attempts).toBe(2);
		expect(gh.gets()).toHaveLength(2);
		expect(gh.puts().map((p) => p.body?.sha)).toEqual(["s1", "s2"]);
		const after = putRows(gh.puts()[1].body);
		expectOnlyRowChanged(meanwhile, after, 4);
		expect(after).toHaveLength(11);
		expect(after.at(-1)?.label).toBe("New Canvas row");
	});

	it("gives up after a second 409 (exactly one retry)", async () => {
		const gh = fakeGitHub({ gets: [{ sha: "s1", text: FIXTURE_TEXT }], puts: [409, 409, 200] });
		await expect(markDone(cfg(gh.fetch), "assignment_1", clock)).rejects.toThrow(GitHubError);
		expect(gh.gets()).toHaveLength(2);
		expect(gh.puts()).toHaveLength(2);
	});

	it("on 409, refuses if the refetched row was already marked done elsewhere", async () => {
		const doneElsewhere = serializeRows(
			original().map((r) => (r.id === "assignment_1" ? { ...r, done: true, done_at: "2026-10-09T10:14" } : r)),
			2,
		);
		const gh = fakeGitHub({ gets: [{ sha: "s1", text: FIXTURE_TEXT }, { sha: "s2", text: doneElsewhere }], puts: [409] });
		await expect(markDone(cfg(gh.fetch), "assignment_1", clock)).rejects.toThrow(/Already done/);
		expect(gh.puts()).toHaveLength(1);
	});

	it("does not retry other errors", async () => {
		const gh = fakeGitHub({ gets: [{ sha: "s1", text: FIXTURE_TEXT }], puts: [500] });
		await expect(markDone(cfg(gh.fetch), "assignment_1", clock)).rejects.toThrow(/HTTP 500/);
		expect(gh.puts()).toHaveLength(1);
	});

	it("never PUTs when it refuses", async () => {
		for (const id of ["assignment_9147805", "C104", "assignment"]) {
			const gh = fakeGitHub({ gets: [{ sha: "s1", text: FIXTURE_TEXT }] });
			await expect(markDone(cfg(gh.fetch), id, clock)).rejects.toThrow(Refusal);
			expect(gh.puts()).toHaveLength(0);
		}
	});

	it("keeps an indent=1 file at indent 1", async () => {
		const one = serializeRows(original(), 1);
		const gh = fakeGitHub({ gets: [{ sha: "s1", text: one }] });
		await markDone(cfg(gh.fetch), "d-760a9021d9", clock);
		const written = decodeBase64Utf8(gh.puts()[0].body!.content);
		expect(written.split("\n")[1]).toBe(" {");
		expect(hunk(one, written)).toEqual({
			added: [
				'  "label": "DONE 10/9 — SOCIOL Week 6 SEE DO NOT RE-ADD.",',
				'  "id": "d-760a9021d9",',
				'  "done": true,',
				'  "done_at": "2026-10-09T10:15"',
			],
			removed: ['  "label": "SOCIOL Week 6 SEE",', '  "id": "d-760a9021d9"'],
		});
	});

	it("never puts the token in error messages", async () => {
		const gh = fakeGitHub({ gets: [{ sha: "s1", text: FIXTURE_TEXT }], puts: [500] });
		const err = await markDone(cfg(gh.fetch), "assignment_1", clock).catch((e: Error) => e);
		expect(String((err as Error).message)).not.toContain(TOKEN);
	});
});
