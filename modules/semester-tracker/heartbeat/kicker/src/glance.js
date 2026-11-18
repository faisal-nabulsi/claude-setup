// Phone glance: GET /todo returns a short to-do list built from deadlines.json
// in the private heartbeat repo (read through the GitHub contents API).
// Everything here is plain functions so test/glance.test.js can run it under
// `node --test` with no Cloudflare tooling and no network.

export const TZ = "America/Los_Angeles";
export const MAX_LINES = 25; // whole text body, title line included
export const LABEL_MAX = 80;
export const OVERDUE_DAYS = 3;
export const AHEAD_DAYS = 7;
export const CACHE_TTL_MS = 60_000; // re-read GitHub at most once a minute per isolate
export const STALE_OK_MS = 6 * 3_600_000; // if GitHub is down, serve a copy up to 6h old

const DAY_MS = 86_400_000;
const DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

// ---------------------------------------------------------------- time ----
// "Wall-clock ms": a Los Angeles local time encoded as if it were UTC. Rows in
// deadlines.json are local "YYYY-MM-DDTHH:MM" strings, so converting `now` the
// same way lets plain subtraction and getUTC* do the date math without a
// timezone library. A DST change can move a window edge by one hour, which
// does not matter for a glance.
const wallFmt = new Intl.DateTimeFormat("en-US", {
  timeZone: TZ,
  hourCycle: "h23",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
});

export function wallMs(date) {
  const p = Object.fromEntries(wallFmt.formatToParts(date).map((x) => [x.type, x.value]));
  return Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour, +p.minute);
}

export function parseWhen(s) {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::\d{2})?$/.exec(String(s ?? "").trim());
  return m ? Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]) : null;
}

export function fmtTime(ms) {
  const d = new Date(ms);
  const h = d.getUTCHours();
  const m = d.getUTCMinutes();
  const ap = h < 12 ? "am" : "pm";
  return `${h % 12 || 12}${m ? `:${String(m).padStart(2, "0")}` : ""}${ap}`;
}

// "Today 6pm", "Tmrw 11:59pm", "Yest 7pm", otherwise "Thu 10/15 1pm".
export function fmtDue(ms, nowMs) {
  const dayDiff = Math.floor(ms / DAY_MS) - Math.floor(nowMs / DAY_MS);
  const day =
    dayDiff === 0 ? "Today"
    : dayDiff === 1 ? "Tmrw"
    : dayDiff === -1 ? "Yest"
    : fmtDate(ms);
  return `${day} ${fmtTime(ms)}`;
}

export function fmtDate(ms) {
  const d = new Date(ms);
  return `${DOW[d.getUTCDay()]} ${d.getUTCMonth() + 1}/${d.getUTCDate()}`;
}

function fmtWallIso(ms) {
  return new Date(ms).toISOString().slice(0, 16); // "YYYY-MM-DDTHH:MM", LA local
}

// Collapse whitespace and cut to `max` characters. Array.from splits by code
// point, so an emoji is never cut in half.
export function trimLabel(s, max = LABEL_MAX) {
  const chars = Array.from(String(s ?? "").replace(/\s+/g, " ").trim());
  return chars.length <= max ? chars.join("") : chars.slice(0, max - 1).join("").trimEnd() + "…";
}

// ------------------------------------------------------------ selection ----
// Picks what to show:
//   overdue  = not done, due in the last 3 days
//   upcoming = not done, due from now to 7 days out
// Inside each section hard deadlines come first, then soft ones (soft = a
// self-imposed target, flagged with "~"). If everything does not fit in
// MAX_LINES, lines are kept in this order: overdue hard, upcoming hard,
// overdue soft, upcoming soft. The rest becomes a "+N more" line.
export function buildGlance(rows, nowMs) {
  const overdue = [];
  const upcoming = [];
  for (const r of Array.isArray(rows) ? rows : []) {
    if (!r || typeof r !== "object" || r.done) continue; // done rows are tombstones
    const t = parseWhen(r.when);
    if (t === null || !r.label) continue;
    const item = { id: r.id ?? null, when: r.when, t, soft: Boolean(r.soft), label: trimLabel(r.label) };
    if (t < nowMs && t >= nowMs - OVERDUE_DAYS * DAY_MS) overdue.push(item);
    else if (t >= nowMs && t < nowMs + AHEAD_DAYS * DAY_MS) upcoming.push(item);
  }
  const byTime = (a, b) => a.t - b.t || a.label.localeCompare(b.label);
  const split = (list) => [list.filter((i) => !i.soft).sort(byTime), list.filter((i) => i.soft).sort(byTime)];
  const [overHard, overSoft] = split(overdue);
  const [upHard, upSoft] = split(upcoming);

  const priority = [...overHard, ...upHard, ...overSoft, ...upSoft];
  let budget = MAX_LINES - 1 - (overdue.length ? 1 : 0) - (upcoming.length ? 1 : 0);
  if (priority.length > budget) budget -= 1; // room for the "+N more" line
  const keep = new Set(priority.slice(0, Math.max(budget, 0)));
  const pick = (list) => list.filter((i) => keep.has(i));
  return {
    overdue: [...pick(overHard), ...pick(overSoft)],
    upcoming: [...pick(upHard), ...pick(upSoft)],
    more: priority.length - keep.size,
  };
}

// ------------------------------------------------------------ rendering ----
export function renderText(glance, nowMs, { staleMinutes = null } = {}) {
  const anySoft = [...glance.overdue, ...glance.upcoming].some((i) => i.soft);
  let title = `To-do · ${fmtDate(nowMs)} ${fmtTime(nowMs)}`;
  if (anySoft) title += "  (~ = soft)";
  if (staleMinutes !== null) title += `  [GitHub unreachable, ${staleMinutes}m old]`;
  const line = (i) => `${i.soft ? "~ " : ""}${fmtDue(i.t, nowMs)}  ${i.label}`;
  const out = [title];
  if (glance.overdue.length) out.push("OVERDUE", ...glance.overdue.map(line));
  if (glance.upcoming.length) out.push("NEXT 7 DAYS", ...glance.upcoming.map(line));
  if (!glance.overdue.length && !glance.upcoming.length) out.push("Nothing due in the next 7 days.");
  if (glance.more > 0) out.push(`+${glance.more} more`);
  return out.join("\n") + "\n";
}

export function renderJson(glance, nowMs, { staleMinutes = null } = {}) {
  const item = (i) => ({ id: i.id, when: i.when, due: fmtDue(i.t, nowMs), soft: i.soft, label: i.label });
  return JSON.stringify({
    now: fmtWallIso(nowMs),
    stale: staleMinutes !== null,
    overdue: glance.overdue.map(item),
    upcoming: glance.upcoming.map(item),
    more: glance.more,
  });
}

// ----------------------------------------------------------------- auth ----
// Constant-time string comparison: hash both sides to fixed-length SHA-256
// digests, then XOR every byte without stopping early. Neither the position
// of the first wrong character nor the key length leaks through timing.
export async function timingSafeEqualStr(a, b) {
  const enc = new TextEncoder();
  const [x, y] = await Promise.all(
    [a, b].map((s) => crypto.subtle.digest("SHA-256", enc.encode(String(s)))),
  );
  const u = new Uint8Array(x);
  const v = new Uint8Array(y);
  let diff = 0;
  for (let i = 0; i < u.length; i++) diff |= u[i] ^ v[i];
  return diff === 0;
}

// Fails closed: if the GLANCE_KEY secret is missing or short, nobody gets in,
// so `wrangler secret delete GLANCE_KEY` is an instant revoke.
export async function authorized(header, key) {
  const expected = typeof key === "string" ? key.trim() : "";
  const m = /^Bearer[ \t]+(\S+)[ \t]*$/i.exec(header ?? "");
  const ok = await timingSafeEqualStr(m ? m[1] : "", expected);
  return ok && expected.length >= 32;
}

// --------------------------------------------------------------- GitHub ----
export class GlanceError extends Error {}

export async function fetchDeadlines(env, fetchImpl) {
  if (!env.GITHUB_READ_TOKEN) throw new GlanceError("GITHUB_READ_TOKEN secret is not set");
  const path = env.DEADLINES_PATH || "deadlines.json";
  const url = `https://api.github.com/repos/${env.REPO}/contents/${path}?ref=${encodeURIComponent(env.REF || "main")}`;
  const res = await fetchImpl(url, {
    headers: {
      Authorization: `Bearer ${env.GITHUB_READ_TOKEN}`,
      Accept: "application/vnd.github.raw+json", // file bytes, not base64-in-JSON
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "heartbeat-kicker-glance",
    },
  });
  // Status only: the response body is never echoed back or logged.
  if (!res.ok) throw new GlanceError(`GitHub read failed: HTTP ${res.status}`);
  const rows = await res.json();
  if (!Array.isArray(rows)) throw new GlanceError("deadlines.json is not a JSON array");
  return rows;
}

// `cache` is a plain object that lives as long as the Worker isolate (see
// index.js). Within CACHE_TTL_MS it is served without calling GitHub.
export async function loadDeadlines(env, fetchImpl, cache, nowMsUtc) {
  const age = cache.at === undefined ? Infinity : nowMsUtc - cache.at;
  if (cache.rows && age < CACHE_TTL_MS) return { rows: cache.rows, staleMinutes: null };
  try {
    const rows = await fetchDeadlines(env, fetchImpl);
    cache.rows = rows;
    cache.at = nowMsUtc;
    return { rows, staleMinutes: null };
  } catch (err) {
    if (cache.rows && age < STALE_OK_MS) return { rows: cache.rows, staleMinutes: Math.round(age / 60_000) };
    throw err;
  }
}

// -------------------------------------------------------------- routing ----
const BASE_HEADERS = { "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff" };

function reply(status, body, type = "text/plain; charset=utf-8", extra = {}) {
  return new Response(body, { status, headers: { ...BASE_HEADERS, "Content-Type": type, ...extra } });
}

// Only GET /todo exists. Everything else is 404 (no OPTIONS, no CORS headers,
// so a web page in a browser cannot read it cross-origin).
export async function handleRequest(request, env, deps = {}) {
  const now = deps.now ?? (() => new Date());
  const fetchImpl = deps.fetchImpl ?? ((...a) => fetch(...a));
  const cache = deps.cache ?? {};

  const url = new URL(request.url);
  if (request.method !== "GET" || url.pathname !== "/todo") return reply(404, "not found\n");
  if (!(await authorized(request.headers.get("Authorization"), env.GLANCE_KEY))) {
    return reply(401, "unauthorized\n", undefined, { "WWW-Authenticate": 'Bearer realm="todo"' });
  }

  const nowDate = now();
  let loaded;
  try {
    loaded = await loadDeadlines(env, fetchImpl, cache, nowDate.getTime());
  } catch (err) {
    const msg = err instanceof GlanceError ? err.message : "unexpected error";
    return reply(502, `Couldn't load deadlines: ${msg}\n`);
  }
  const nowMs = wallMs(nowDate);
  const glance = buildGlance(loaded.rows, nowMs);
  const opts = { staleMinutes: loaded.staleMinutes };
  if (url.searchParams.get("format") === "json") {
    return reply(200, renderJson(glance, nowMs, opts), "application/json; charset=utf-8");
  }
  return reply(200, renderText(glance, nowMs, opts));
}
