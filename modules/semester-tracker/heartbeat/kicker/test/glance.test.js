// Run: npm test   (node --test, no dependencies)
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import {
  AHEAD_DAYS,
  CACHE_TTL_MS,
  LABEL_MAX,
  MAX_LINES,
  authorized,
  buildGlance,
  fmtDue,
  handleRequest,
  parseWhen,
  renderJson,
  renderText,
  timingSafeEqualStr,
  trimLabel,
  wallMs,
} from "../src/glance.js";
import worker from "../src/index.js";

const ROWS = JSON.parse(readFileSync(new URL("./fixtures/deadlines.json", import.meta.url), "utf8"));
const NOW = new Date("2026-10-09T17:00:00Z"); // Fri 10/9 10:00am PDT
const NOW_WALL = wallMs(NOW);
const KEY = "k".repeat(20) + "0123456789abcdefABCDEF+/="; // 45 chars, test-only value
const ENV = {
  GLANCE_KEY: KEY,
  GITHUB_READ_TOKEN: "test-read-token",
  REPO: "YOUR-GITHUB-LOGIN/semester-heartbeat",
  REF: "main",
};

function fakeGitHub(rows = ROWS, status = 200) {
  const calls = [];
  const fn = async (url, init) => {
    calls.push({ url, init });
    return new Response(JSON.stringify(rows), { status });
  };
  fn.calls = calls;
  return fn;
}

function get(path, headers = {}, method = "GET") {
  return new Request(`https://heartbeat-kicker.example.workers.dev${path}`, { method, headers });
}

const auth = { Authorization: `Bearer ${KEY}` };

// ---------------------------------------------------------------- time ----
test("wallMs converts UTC to Los Angeles wall-clock time (PDT and PST)", () => {
  assert.equal(new Date(wallMs(NOW)).toISOString().slice(0, 16), "2026-10-09T10:00");
  assert.equal(new Date(wallMs(new Date("2026-12-01T18:30:00Z"))).toISOString().slice(0, 16), "2026-12-01T10:30");
  // Just after midnight UTC is still the previous evening in LA.
  assert.equal(new Date(wallMs(new Date("2026-10-10T06:59:00Z"))).toISOString().slice(0, 16), "2026-10-09T23:59");
});

test("parseWhen accepts YYYY-MM-DDTHH:MM (optional :SS) and rejects junk", () => {
  assert.equal(parseWhen("2026-10-09T18:00"), Date.UTC(2026, 9, 9, 18, 0));
  assert.equal(parseWhen("2026-10-09T18:00:30"), Date.UTC(2026, 9, 9, 18, 0));
  for (const bad of ["next week", "", null, undefined, "2026-10-09", 12345]) assert.equal(parseWhen(bad), null);
});

test("fmtDue uses Today/Tmrw/Yest, otherwise weekday + date; drops :00", () => {
  assert.equal(fmtDue(parseWhen("2026-10-09T18:00"), NOW_WALL), "Today 6pm");
  assert.equal(fmtDue(parseWhen("2026-10-10T23:59"), NOW_WALL), "Tmrw 11:59pm");
  assert.equal(fmtDue(parseWhen("2026-10-08T00:05"), NOW_WALL), "Yest 12:05am");
  assert.equal(fmtDue(parseWhen("2026-10-15T12:00"), NOW_WALL), "Thu 10/15 12pm");
});

test("trimLabel collapses whitespace, caps at 80 chars, never splits an emoji", () => {
  assert.equal(trimLabel("  a \n\t b  "), "a b");
  const long = trimLabel("x".repeat(200));
  assert.equal(Array.from(long).length, LABEL_MAX);
  assert.ok(long.endsWith("…"));
  const emoji = trimLabel("🐻".repeat(100));
  assert.equal(Array.from(emoji).length, LABEL_MAX);
  assert.ok(!/[\uD800-\uDBFF]…$/.test(emoji), "no lone surrogate before the ellipsis");
  assert.equal(trimLabel("short"), "short");
});

// ------------------------------------------------------------ selection ----
test("buildGlance: windows, done rows skipped, hard before soft, malformed rows skipped", () => {
  const g = buildGlance(ROWS, NOW_WALL);
  assert.deepEqual(g.overdue.map((i) => i.id), ["h-3days", "h-overdue", "s-overdue"]);
  assert.deepEqual(g.upcoming.map((i) => i.id), [
    "h-now", "h-today", "h-tmrw", "h-day6", // hard, by time
    "s-today", "s-emoji", "s-null-flags", // then soft, by time
  ]);
  assert.equal(g.more, 0);
  const shown = new Set([...g.overdue, ...g.upcoming].map((i) => i.id));
  for (const id of ["h-too-old", "done-overdue", "done-future", "no-label", "bad-when", "h-day7", "h-day8"]) {
    assert.ok(!shown.has(id), `${id} must not be shown`);
  }
});

test("renderText matches the expected phone layout", () => {
  const text = renderText(buildGlance(ROWS, NOW_WALL), NOW_WALL);
  const lines = text.trimEnd().split("\n");
  assert.deepEqual(lines.slice(0, 7), [
    "To-do · Fri 10/9 10am  (~ = soft)",
    "OVERDUE",
    "Tue 10/6 10am  Hard row exactly 3 days ago (inside window)",
    "Yest 11:59pm  Hard essay due last night",
    "~ Wed 10/7 7pm  Soft journal entry from Wednesday",
    "NEXT 7 DAYS",
    "Today 10am  Hard row due exactly now",
  ]);
  assert.equal(lines[7], "Today 6pm  Gradescope make-ups");
  assert.ok(lines[8].startsWith("Tmrw 12pm  A very long hard label") && lines[8].endsWith("…"));
  assert.equal(lines[9], "Thu 10/15 1pm  Field trip with odd whitespace");
  assert.equal(lines[10], "~ Today 8pm  IF TIME: optional make-up entries");
  assert.ok(lines[11].startsWith("~ Mon 10/12 7pm  🐻🐻🐻"));
  assert.equal(lines[12], "~ Tue 10/13 8am  Soft null done");
  assert.equal(lines.length, 13);
  for (const l of lines.slice(2)) {
    const label = l.replace(/^~ /, "").split("  ").slice(1).join("  ");
    assert.ok(Array.from(label).length <= LABEL_MAX, `label too long: ${l}`);
  }
});

test("caps output at 25 lines; keeps hard rows over soft and says how many are hidden", () => {
  const rows = [];
  for (let i = 0; i < 30; i++) rows.push({ id: `s${i}`, when: "2026-10-10T09:00", label: `soft ${i}`, soft: true });
  for (let i = 0; i < 20; i++) rows.push({ id: `h${i}`, when: "2026-10-11T09:00", label: `hard ${i}` });
  rows.push({ id: "o1", when: "2026-10-08T09:00", label: "overdue hard" });
  const g = buildGlance(rows, NOW_WALL);
  const lines = renderText(g, NOW_WALL).trimEnd().split("\n");
  assert.ok(lines.length <= MAX_LINES, `got ${lines.length} lines`);
  assert.equal(lines.length, MAX_LINES);
  assert.equal(lines.at(-1), `+${g.more} more`);
  assert.equal(g.overdue.length + g.upcoming.length + g.more, 51);
  assert.equal(g.upcoming.filter((i) => !i.soft).length, 20, "all hard rows survive the cap");
  assert.ok(g.overdue.some((i) => i.id === "o1"));
});

test("empty list renders a friendly line", () => {
  const text = renderText(buildGlance([], NOW_WALL), NOW_WALL);
  assert.equal(text, "To-do · Fri 10/9 10am\nNothing due in the next 7 days.\n");
  assert.equal(AHEAD_DAYS, 7);
});

test("renderJson carries id, when, due, soft, label for each item", () => {
  const j = JSON.parse(renderJson(buildGlance(ROWS, NOW_WALL), NOW_WALL));
  assert.equal(j.now, "2026-10-09T10:00");
  assert.equal(j.stale, false);
  assert.equal(j.more, 0);
  assert.deepEqual(j.upcoming[1], { id: "h-today", when: "2026-10-09T18:00", due: "Today 6pm", soft: false, label: "Gradescope make-ups" });
  assert.equal(j.overdue.length, 3);
});

// ----------------------------------------------------------------- auth ----
test("timingSafeEqualStr", async () => {
  assert.equal(await timingSafeEqualStr("abc", "abc"), true);
  assert.equal(await timingSafeEqualStr("abc", "abd"), false);
  assert.equal(await timingSafeEqualStr("abc", "abcd"), false);
  assert.equal(await timingSafeEqualStr("", ""), true);
});

test("authorized: exact bearer key only; fails closed when the secret is missing or short", async () => {
  assert.equal(await authorized(`Bearer ${KEY}`, KEY), true);
  assert.equal(await authorized(`bearer ${KEY}`, KEY), true, "scheme is case-insensitive");
  assert.equal(await authorized(`Bearer ${KEY}`, `${KEY}\n`), true, "trailing newline in the secret is ignored");
  assert.equal(await authorized(`Bearer ${KEY}x`, KEY), false);
  assert.equal(await authorized(`Bearer ${KEY.slice(0, -1)}`, KEY), false);
  assert.equal(await authorized(KEY, KEY), false, "missing scheme");
  assert.equal(await authorized(`Basic ${KEY}`, KEY), false);
  assert.equal(await authorized(`Bearer ${KEY} extra`, KEY), false);
  assert.equal(await authorized(null, KEY), false);
  assert.equal(await authorized("Bearer ", ""), false, "unset secret never matches an empty key");
  assert.equal(await authorized("Bearer short", "short"), false, "secrets under 32 chars are refused");
  assert.equal(await authorized(`Bearer ${KEY}`, undefined), false);
});

// -------------------------------------------------------------- routing ----
test("non-GET methods and other paths are 404 with no CORS headers", async () => {
  const gh = fakeGitHub();
  const cases = [
    get("/", auth), get("/todo/", auth), get("/todo.json", auth), get("/TODO", auth),
    get("/todo", auth, "POST"), get("/todo", auth, "PUT"), get("/todo", auth, "DELETE"),
    get("/todo", { ...auth, Origin: "https://evil.example", "Access-Control-Request-Method": "GET" }, "OPTIONS"),
    get("/todo", auth, "HEAD"),
  ];
  for (const req of cases) {
    const res = await handleRequest(req, ENV, { now: () => NOW, fetchImpl: gh, cache: {} });
    assert.equal(res.status, 404, `${req.method} ${new URL(req.url).pathname}`);
    assert.equal(res.headers.get("Access-Control-Allow-Origin"), null);
  }
  assert.equal(gh.calls.length, 0, "GitHub is never read for rejected requests");
});

test("GET /todo without a valid key is 401 and never touches GitHub", async () => {
  const gh = fakeGitHub();
  for (const headers of [{}, { Authorization: "Bearer nope" }, { Authorization: `Token ${KEY}` }]) {
    const res = await handleRequest(get("/todo", headers), ENV, { now: () => NOW, fetchImpl: gh, cache: {} });
    assert.equal(res.status, 401);
    assert.equal(await res.text(), "unauthorized\n");
    assert.match(res.headers.get("WWW-Authenticate"), /^Bearer/);
    assert.equal(res.headers.get("Access-Control-Allow-Origin"), null);
  }
  const noSecret = await handleRequest(get("/todo", auth), { ...ENV, GLANCE_KEY: undefined }, { now: () => NOW, fetchImpl: gh, cache: {} });
  assert.equal(noSecret.status, 401);
  assert.equal(gh.calls.length, 0);
});

test("GET /todo with the key returns the text list and reads GitHub correctly", async () => {
  const gh = fakeGitHub();
  const res = await handleRequest(get("/todo", auth), ENV, { now: () => NOW, fetchImpl: gh, cache: {} });
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("Content-Type"), "text/plain; charset=utf-8");
  assert.equal(res.headers.get("Cache-Control"), "no-store");
  assert.equal(res.headers.get("Access-Control-Allow-Origin"), null);
  const body = await res.text();
  assert.ok(body.startsWith("To-do · Fri 10/9 10am"));
  assert.ok(body.includes("Today 6pm  Gradescope make-ups"));
  assert.equal(gh.calls.length, 1);
  const { url, init } = gh.calls[0];
  assert.equal(url, "https://api.github.com/repos/YOUR-GITHUB-LOGIN/semester-heartbeat/contents/deadlines.json?ref=main");
  assert.equal(init.headers.Authorization, "Bearer test-read-token");
  assert.equal(init.headers.Accept, "application/vnd.github.raw+json");
  assert.ok(!body.includes("test-read-token") && !body.includes(KEY), "no secrets in the body");
});

test("?format=json returns JSON", async () => {
  const res = await handleRequest(get("/todo?format=json", auth), ENV, { now: () => NOW, fetchImpl: fakeGitHub(), cache: {} });
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("Content-Type"), "application/json; charset=utf-8");
  const j = await res.json();
  assert.deepEqual(j.overdue.map((i) => i.id), ["h-3days", "h-overdue", "s-overdue"]);
});

test("GitHub read is cached for ~60 s per isolate", async () => {
  const gh = fakeGitHub();
  const cache = {};
  let t = NOW.getTime();
  const deps = { now: () => new Date(t), fetchImpl: gh, cache };
  await handleRequest(get("/todo", auth), ENV, deps);
  t += CACHE_TTL_MS - 1000;
  await handleRequest(get("/todo?format=json", auth), ENV, deps);
  assert.equal(gh.calls.length, 1, "second call within 60 s uses the cache");
  t += 2000;
  await handleRequest(get("/todo", auth), ENV, deps);
  assert.equal(gh.calls.length, 2, "after 60 s GitHub is read again");
});

test("GitHub failure: serves a recent cached copy marked stale, else 502 with the status only", async () => {
  const cache = {};
  let t = NOW.getTime();
  await handleRequest(get("/todo", auth), ENV, { now: () => new Date(t), fetchImpl: fakeGitHub(), cache });
  t += 10 * 60_000;
  const stale = await handleRequest(get("/todo", auth), ENV, { now: () => new Date(t), fetchImpl: fakeGitHub(ROWS, 503), cache });
  assert.equal(stale.status, 200);
  assert.match((await stale.text()).split("\n")[0], /\[GitHub unreachable, 10m old\]$/);

  const cold = await handleRequest(get("/todo", auth), ENV, { now: () => NOW, fetchImpl: fakeGitHub(ROWS, 404), cache: {} });
  assert.equal(cold.status, 502);
  assert.equal(await cold.text(), "Couldn't load deadlines: GitHub read failed: HTTP 404\n");

  const noToken = await handleRequest(get("/todo", auth), { ...ENV, GITHUB_READ_TOKEN: undefined }, { now: () => NOW, fetchImpl: fakeGitHub(), cache: {} });
  assert.equal(noToken.status, 502);
  assert.equal(await noToken.text(), "Couldn't load deadlines: GITHUB_READ_TOKEN secret is not set\n");

  const notArray = await handleRequest(get("/todo", auth), ENV, { now: () => NOW, fetchImpl: fakeGitHub({ rows: [] }), cache: {} });
  assert.equal(notArray.status, 502);
});

test("worker entry keeps the cron handler and routes fetch to the glance", async () => {
  assert.equal(typeof worker.scheduled, "function");
  assert.equal(typeof worker.fetch, "function");
  const res = await worker.fetch(get("/"), ENV);
  assert.equal(res.status, 404);
  const unauth = await worker.fetch(get("/todo"), ENV);
  assert.equal(unauth.status, 401);
});
