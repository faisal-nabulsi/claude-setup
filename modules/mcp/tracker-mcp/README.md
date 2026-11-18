# tracker-mcp

A private remote MCP server for the user's to-do tracker. MCP (Model Context Protocol) is the standard way Claude talks to outside tools. "Remote" means it runs on the internet, so Claude on the web, on the phone and in the desktop app can all use the same list.

It runs as a Cloudflare Worker and was started from Cloudflare's official [remote MCP + GitHub OAuth template](https://github.com/cloudflare/ai/tree/main/demos/remote-mcp-github-oauth).

**Not deployed yet. To set it up, follow [SETUP.md](SETUP.md).**

## What it does

The single source of truth is `deadlines.json` on the `main` branch of the private repo `YOUR-GITHUB-LOGIN/semester-heartbeat`. The server has three tools:

| Tool | What it does | Writes? |
|---|---|---|
| `todo_list({days?, include_soft?})` | Open items due in the next `days` days (default 7, max 30), plus items that went past due in the last 3 days and were never marked done. Soft (self-imposed) targets are hidden unless `include_soft` is true. | No |
| `todo_search({query})` | Rows whose label, id or date contains the text, done or not (id, when, done, label). Up to 25. | No |
| `todo_mark_done({id})` | Marks one row done, by exact id only. | Yes, one row |

There is no add, edit or delete tool, on purpose. Fewer tools means fewer ways for a confused or manipulated model to damage the list.

`todo_mark_done` follows the same rules as `verify/mark_done.py` and `verify/deadlines_lib.py` in the semester-tracker tracker:

- It changes exactly one row: `done: true`, `done_at` (Pacific time), and a `DONE m/d — <label> DO NOT RE-ADD.` label.
- Before writing, it re-checks that every other row is byte-for-byte unchanged, the row count is the same, and no done row went back to not-done. If any check fails, it writes nothing.
- It writes with the file's `sha` (GitHub's version stamp for the file). If someone else changed the file in between, GitHub answers 409 Conflict, so the server re-reads the file and tries once more.
- It refuses ids that don't match exactly, ids shared by two rows, and rows that are already done.

Labels come from Canvas, so the server treats them as untrusted text. It strips control and invisible characters, neutralises `<` and `>`, trims them, and tells the model they are data, not instructions.

## Who can use it

Only the GitHub accounts listed in `ALLOWED_GITHUB_LOGINS` (in `wrangler.jsonc`, default `YOUR-GITHUB-LOGIN`). The Cloudflare template lets anyone with a GitHub account in. This server checks the allowlist three times:

1. **At sign-in** (`src/github-handler.ts`, `/callback`). Another GitHub account is turned away before any token is created.
2. **On every `/mcp` request** (`gateApiHandler` in `src/access.ts`).
3. **Inside every tool** (`guarded()` in `src/tools.ts`), right before it touches GitHub.

Checks 2 and 3 read the allowlist on every request. Setting it to `""` and deploying therefore locks out even tokens that were already issued.

## Layout

```
src/index.ts              Worker entry: OAuth provider + MCP Durable Object (from the template)
src/github-handler.ts     /authorize, /callback: GitHub sign-in (template) + allowlist check 1
src/workers-oauth-utils.ts  consent screen, CSRF and state helpers (template, unchanged)
src/utils.ts              GitHub OAuth helpers (template; no scopes, no stored GitHub token)
src/access.ts             allowlist parsing + check 2 (the /mcp gate)
src/tools.ts              the three tools + check 3
src/deadlines.ts          pure logic: ids, Pacific time, rendering, mark-done plan + safety checks
src/github-contents.ts    GitHub Contents API read/write with sha + one retry on 409
test/                     unit tests (vitest, Node)
test-e2e/                 end-to-end test in the real Workers runtime, GitHub stubbed
wrangler.jsonc            Worker config (vars, KV, Durable Object, required secret names)
SETUP.md                  deploy + connect + revoke steps
```

## Commands

```bash
npm ci                # install the pinned dependencies
npm test              # unit tests
npm run test:e2e      # end-to-end test in local workerd (no network)
npm run check         # type-check + unit + e2e + `wrangler deploy --dry-run`
npm run cf-typegen    # regenerate worker-configuration.d.ts after editing wrangler.jsonc
```
