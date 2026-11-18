# tracker-mcp: setup, connect, revoke

This gets the tracker into every Claude surface (claude.ai, the phone apps, the desktop app, Claude Code). It takes about 20 minutes. Everything below is done by you, because each step signs in to an account or creates a credential.

**What you will create**

| Thing | Where | Why it is needed |
|---|---|---|
| A GitHub OAuth App | github.com | It runs the "Sign in with GitHub" step, so the server knows it is you. |
| A fine-grained personal access token (PAT) | github.com | It lets the server read and write `deadlines.json`, and nothing else. |
| A KV namespace | Cloudflare | KV is Cloudflare's key-value store. The sign-in library keeps its tokens there. |
| 4 secrets | Cloudflare | Passwords the Worker needs. They never go in the repo. |

Your Worker's address will be `https://tracker-mcp.<your-subdomain>.workers.dev`. Below, that is written as **`$URL`**.

---

## 0. Check the build (1 min)

```bash
cd ~/code/tracker-mcp
npm ci
npm run check
```

`npm run check` type-checks the code, runs the unit tests and the end-to-end test, and does `wrangler deploy --dry-run`. Do this first, because it proves the code builds on your machine before anything goes online.

## 1. Sign in to Cloudflare (1 min)

```bash
npx wrangler login
npx wrangler whoami
```

Use the same Cloudflare account that runs `heartbeat-kicker`.

## 2. Find your workers.dev address (1 min)

Open the Cloudflare dashboard → **Workers & Pages**. Your subdomain is shown in the right-hand column (it looks like `something.workers.dev`).

Then `$URL` = `https://tracker-mcp.<that subdomain>.workers.dev`. You need it now, because GitHub asks for it in the next step.

If you can't find it: continue anyway with a guess. `npx wrangler deploy` in step 7 prints the real address, and you can fix the two URLs in the GitHub OAuth App afterwards.

## 3. Create the KV namespace (1 min)

```bash
npx wrangler kv namespace create OAUTH_KV
```

It prints an `id`. Open `wrangler.jsonc`, find `"id": "<Add-KV-ID>"`, and replace `<Add-KV-ID>` with that id. Commit the change (the id is not a secret).

## 4. Create the GitHub OAuth App (3 min)

GitHub → **Settings → Developer settings → OAuth Apps → New OAuth App**.

- Application name: `tracker-mcp`
- Homepage URL: `$URL`
- Authorization callback URL: `$URL/callback`
- Leave "Enable Device Flow" unchecked.

Click **Register application**. Copy the **Client ID**. Click **Generate a new client secret** and copy it. Keep both for step 6.

This app only proves who you are. The server asks GitHub for no permissions (no scopes), so it can't read or change anything in your GitHub account.

## 5. Create the fine-grained PAT (3 min)

GitHub → **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**.

- Token name: `tracker-mcp`
- Expiration: pick a date you will remember, such as Dec 31, 2026. When it expires the tools stop working until you put in a new token (step 6, last command).
- Resource owner: `YOUR-GITHUB-LOGIN`
- Repository access: **Only select repositories** → `YOUR-GITHUB-LOGIN/semester-heartbeat`
- Repository permissions: **Contents → Read and write**. GitHub adds **Metadata → Read-only** by itself. Leave everything else at "No access".

Copy the token. This is the most powerful secret here, because it can write to the heartbeat repo. That is why it is limited to one repo and one permission.

## 6. Put the 4 secrets into Cloudflare (2 min)

Run these one at a time and paste the value when asked. Nothing is echoed to the screen.

```bash
npx wrangler secret put GITHUB_CLIENT_ID        # from step 4
```

The first command says there is no Worker called `tracker-mcp` yet and asks to create one. Answer **Y**. It creates an empty placeholder, which step 7 replaces.

```bash
npx wrangler secret put GITHUB_CLIENT_SECRET    # from step 4
openssl rand -hex 32 | npx wrangler secret put COOKIE_ENCRYPTION_KEY   # random; you never need to see it
npx wrangler secret put GITHUB_TRACKER_TOKEN    # the PAT from step 5
npx wrangler secret list                        # should list exactly these 4 names
```

Never put these values in a file in this repo. `wrangler deploy` refuses to deploy if any of the 4 is missing, because `wrangler.jsonc` lists them under `secrets.required`.

## 7. Deploy (1 min)

```bash
npm run check
npx wrangler deploy
```

The output ends with the Worker's address. If it differs from your `$URL`, edit the two URLs in the GitHub OAuth App now (GitHub → Settings → Developer settings → OAuth Apps → tracker-mcp).

Quick check that it is up and locked:

```bash
curl -s $URL/.well-known/oauth-authorization-server | head -c 300; echo
curl -s -o /dev/null -w "%{http_code}\n" -X POST $URL/mcp     # expect 401 (no token)
```

## 8. Add it to Claude as a custom connector (3 min)

On **claude.ai** (the website, not the phone):

1. Open **Customize → Connectors** (some versions call this **Settings → Connectors**).
2. Click **+ Add → Add custom connector**.
3. Name: `Tracker`. URL: **`$URL/mcp`**.
4. If there is an OAuth client choice, keep the default ("Use Claude's published identity"). If connecting fails, remove the connector and add it again with **"Register automatically"**. The server supports both.
5. Click **Connect**. A consent page titled "the user's tracker" opens. Click **Approve**, then sign in with GitHub as `YOUR-GITHUB-LOGIN`.

Notes:

- **Free plan limit:** Free accounts can have only one custom connector. If you already use your one slot, this needs Pro, or you remove the other one.
- **Phone and desktop:** connectors added on claude.ai also appear in the Claude iOS and Android apps and in Claude Desktop. You can't add a custom connector from the phone, so do this step on the web first.
- **Claude Code** (optional): `claude mcp add --transport http tracker $URL/mcp`, then run `/mcp` inside Claude Code and choose tracker → Authenticate.
- **Only approve a consent page you started yourself.** The page names the app asking for access. Expect Claude. A link from someone else that leads to this page is an attempt to get a token for your list.

Try it: ask Claude "what's on my to-do list this week?" Then try "mark `<id>` done" with an id from the list.

## How the copies stay in sync

- The server writes only to `deadlines.json` on `main` in `semester-heartbeat`.
- The heartbeat GitHub Action reads that file directly. A row marked done stops getting phone nudges on the next run.
- Your Mac copy (`~/semester-tracker/deadlines.json`) catches up when you `git pull` in `semester-heartbeat` and then run `python3 verify/sync_deadlines.py`. That script union-merges by `id`, and "done" always wins.
- Always `git pull` in `semester-heartbeat` before running `mark_done.py` or `sync_deadlines.py` on the Mac. Otherwise your next push collides with the server's commit.

---

## Revoking access

From fastest to most thorough. Each one works on its own.

| Goal | Do this | Effect |
|---|---|---|
| Lock everyone out now | Set `"ALLOWED_GITHUB_LOGINS": ""` in `wrangler.jsonc`, then `npx wrangler deploy`. | Sign-in, every `/mcp` request and every tool call are refused, including tokens issued earlier. |
| Cut repo access | GitHub → Settings → Developer settings → Fine-grained tokens → `tracker-mcp` → **Revoke**. (Or `npx wrangler secret delete GITHUB_TRACKER_TOKEN`.) | The Worker can no longer read or write `deadlines.json` at all. |
| Disconnect one Claude account | claude.ai → Customize → Connectors → Tracker → **Remove**. | That Claude account stops using the server. |
| Kill all issued MCP tokens | `npx wrangler kv namespace create OAUTH_KV_2`, put the new id in `wrangler.jsonc`, `npx wrangler deploy`. Then delete the old namespace in the dashboard. | Every existing token and client registration is gone, because they lived in the old namespace. Claude asks you to sign in again. |
| Forget "remembered" consent | `openssl rand -hex 32 \| npx wrangler secret put COOKIE_ENCRYPTION_KEY` | The consent page shows again for every client. |
| Stop GitHub sign-in entirely | GitHub → Settings → Developer settings → OAuth Apps → tracker-mcp → **Delete application** (or "Revoke all user tokens"). | Nobody can sign in, so no new tokens can be issued. |
| Remove everything | `npx wrangler delete`, then delete the KV namespace(s) in the dashboard, then do the two GitHub rows above. | Nothing is left running. |

If you edit `ALLOWED_GITHUB_LOGINS` in the Cloudflare dashboard instead of `wrangler.jsonc`, the next `wrangler deploy` puts the file's value back. Edit the file.

## Things to know

- **If you rename your GitHub account,** update `ALLOWED_GITHUB_LOGINS` the same day. The check is by username, and after a rename someone else could register your old one.
- **To allow another account,** list it: `"ALLOWED_GITHUB_LOGINS": "YOUR-GITHUB-LOGIN, other-login"`, then deploy.
- **When the PAT expires,** tool calls answer "GitHub read failed (HTTP 401 ...)". Make a new PAT (step 5) and run `npx wrangler secret put GITHUB_TRACKER_TOKEN`.
- **Logs:** `npx wrangler tail` shows refused sign-ins and tool errors live. Secrets are never logged.
- **Local development** (optional): create a second GitHub OAuth App with callback `http://localhost:8788/callback`, copy `.dev.vars.example` to `.dev.vars`, fill it in, and run `npm run dev`. Consider a read-only PAT there.
- **Dependency note:** `npm audit` flags `@modelcontextprotocol/sdk` 1.29.0 (GHSA-6qxp-vccf-f47h). That bug is in the SDK's OAuth *client* code, which this server never runs (it only uses the SDK's server side). The version is pinned by `agents` 0.17.4, the version the Cloudflare template is built on.
