# Connectors and MCP servers

**MCP** (Model Context Protocol) is the standard way Claude talks to outside tools. An "MCP
server" is a small program that offers Claude a set of tools ("list my Canvas assignments",
"search my email"). Claude Code starts the servers you register and shows their tools in every
session.

**Why connectors instead of the browser:** a connector returns clean data in one call. Driving
a browser means loading pages, reading screens and clicking, which is slower, breaks when a
site changes, and fills the context window. So the rule (in CLAUDE.md and enforced by the
`connectors_first` hook) is: connector first, browser last.

There are two kinds:

- **claude.ai connectors:** hosted by Anthropic or the vendor, added in claude.ai →
  Settings → Connectors. They also work in Claude Code when you're signed in with the same
  account. No code on your Mac.
- **Local MCP servers:** programs on your Mac, registered with `claude mcp add`. Everything in
  this folder is one of these.

Register a local server for all projects with `--scope user` (`-s user`). Check them with
`claude mcp list`; remove one with `claude mcp remove <name> -s user`.

## Where secrets live

Never put a token in `~/.claude.json`: that file is read by many tools and easy to paste by
accident. This setup keeps secrets in one of two places:

1. **The macOS Keychain**, read by a small launcher script at startup (`launchers/*.sh`).
   You store the secret yourself with `launchers/setup-secrets.sh`.
2. **A `.env` file** next to the server, `chmod 600`, written by that server's own `set-*.sh`
   script that hides your typing. Claude never reads or prints these files (the `secrets_guard`
   hook backs that up).

## The servers

| Server | What it does | Writes? | Auth | Folder |
|---|---|---|---|---|
| **canvas** | Your school's Canvas: courses, assignments, due dates, announcements, pages, modules, grades; saves course files to disk | Read-only (only "save a file to this Mac") | Personal Canvas token in Keychain | upstream + `launchers/canvas.sh` |
| **google-workspace** | Gmail, Calendar, Drive, Docs, Sheets, Slides, Contacts, Tasks across **several Google accounts** at once (personal + school) | Can write; you choose scopes. CLAUDE.md forbids writes without your order | Your own Google Cloud OAuth client | upstream + `launchers/google-workspace.sh` |
| **coursework** | Gradescope (assignments, due dates, submitted?), PollEv (is a poll open now?), Ed Discussion (threads) | Read-only | Gradescope login in Keychain; Ed token in `.env` | `coursework-mcp/` |
| **calcentral** | Berkeley students only: your schedule, enrollment appointments, holds, billing, grades, degree progress, plus public class seats and grade distributions | Read-only (no enroll/drop/pay tools exist) | You sign in yourself in a private browser window | `calcentral-mcp/` (optional) |
| **espn-fantasy** | ESPN fantasy leagues: rosters, matchups, free agents, set lineups, waiver claims | Reads by default; writes only with `WRITES_ENABLED=true` | Your ESPN cookies in `.env` | upstream + `espn-fantasy/` |
| **crawl4ai** | Scrape a web page or PDF to Markdown, crawl a small site, list links, screenshot | Read-only | None (local, free) | `crawl4ai-mcp/` |
| **lemonsqueezy** | A Lemon Squeezy store: orders, subscriptions, customers, license keys, revenue | Read-only gate; writes need a flag plus your order | API key in `.env` | `lemonsqueezy-mcp/` (optional) |
| **autohub** | Your own "Zapier": recipes that watch Canvas, grades or a store and push your phone | Push-only | Its own Canvas token in Keychain | `autohub/` |
| **tracker** | A remote MCP (Cloudflare Worker) so Claude on the web or phone can list and tick off your tracker to-dos | One write: mark a row done | GitHub OAuth, allow-listed logins | `tracker-mcp/` (optional) |
| **iMCP** | Apple apps on your Mac: Messages, Reminders, Notes, Calendar, Contacts, Maps, Weather | Depends on what you switch on | macOS permissions | app from https://github.com/mattt/iMCP |
| **parallel-search** | Free web search and fetch | Read-only | None | remote URL |

### canvas (any school's Canvas)
Upstream: https://github.com/vishalsachdev/canvas-mcp (MIT).
1. Install it in its own venv: `uv venv ~/.local/share/mcp/canvas && uv pip install --python ~/.local/share/mcp/canvas/bin/python canvas-mcp`
2. Copy `launchers/canvas.sh` and `launchers/setup-secrets.sh` to `~/.claude/mcp-wrappers/` (the installer does this and fills in your Canvas host, like `canvas.yourschool.edu`).
3. Run `bash ~/.claude/mcp-wrappers/setup-secrets.sh` yourself and paste a Canvas token
   (Canvas → Account → Settings → Approved Integrations → + New Access Token).
4. It registers `canvas` for you: `claude mcp add canvas -s user -- ~/.claude/mcp-wrappers/canvas.sh`.

Tip: students often get 403 on a course's file *list*. Take file ids from module items, pages
and assignments, then use `download_course_file` with `save_directory` set to the course folder.
Never use `read_course_file` to download (it pours the whole file into the chat).

### google-workspace (several Google accounts, one server)
Upstream: https://github.com/aaronsb/google-workspace-mcp. This is the piece that lets Claude
read your **school** Google account and your **personal** one side by side (the claude.ai
Gmail/Calendar/Drive connectors only see one account).
1. Go to https://console.cloud.google.com → create a project (any name).
2. APIs & Services → Library → enable Gmail, Google Calendar, Google Drive, Google Docs,
   Google Sheets, Google Slides, People and Tasks APIs (only the ones you want).
3. OAuth consent screen → External → add yourself (every Google address you'll connect) as a
   test user. Testing mode is fine for personal use.
   (A school Google account may block third-party apps; if sign-in says "admin approval
   needed", that account can't be connected this way.)
4. Credentials → Create credentials → OAuth client ID → **Desktop app**. Keep the window open.
5. Run `bash ~/.claude/mcp-wrappers/setup-secrets.sh`, answer `y` to step 1 and paste the
   client id and secret when macOS asks.
6. In a new Claude session, ask Claude to add each account (`manage_accounts`); a browser
   window opens for you to approve. Pick read-only scopes if you only want reading.
   Re-running the scopes operation replaces the whole set, so list every scope you want.

### coursework (Gradescope, PollEv, Ed)
`coursework-mcp/`. Gradescope reuses the semester tracker's Gradescope reader login
(`semester-tracker/gradescope/setup.sh` stores it in Keychain). Ed needs a token from
https://edstem.org/us/settings/api-tokens, saved with `sh set-ed-token.sh`.
```bash
cd ~/code/coursework-mcp && uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python "mcp==1.30.0" "gradescopeapi==1.8.1" "requests==2.34.2" beautifulsoup4 "httpx==0.28.1"
claude mcp add -s user coursework -- ~/code/coursework-mcp/.venv/bin/python ~/code/coursework-mcp/server.py
```
It never submits, never answers polls, never posts.

### calcentral (Berkeley students only, optional)
See `calcentral-mcp/README.md`. Skip it unless you're at Berkeley.

### espn-fantasy (fantasy football and friends)
Upstream: https://github.com/tlo1216/espn-fantasy-mcp (MIT). Clone it to `~/code/espn-fantasy-mcp`,
`npm install && npm run build`, copy `.env.example` to `.env` and fill in your league id. Then
copy `espn-fantasy/run.sh` and `espn-fantasy/set-cookies.sh` from here into that folder, run
`sh set-cookies.sh` (it hides the cookies as you paste them) and register:
`claude mcp add -s user espn-fantasy -- ~/code/espn-fantasy-mcp/run.sh`.
**Write gate:** setting lineups or claiming players only works when `WRITES_ENABLED=true` is
set for that one process (e.g. on a single `mcp_bridge.py` call), and only after you've said
so in chat. Tip: pass `scoring_period_id` for the week you mean, or bye weeks get missed.

### crawl4ai (free local scraper)
See `crawl4ai-mcp/README.md` for the four-line rebuild. No account, no credits.

### lemonsqueezy (optional, for a Lemon Squeezy store)
See `lemonsqueezy-mcp/README.md`. Read tools only unless you deliberately unlock writes.

### autohub (your own Zapier)
See `autohub/README.md`.

### tracker (custom remote connector, optional)
See `tracker-mcp/README.md` and `tracker-mcp/SETUP.md`. It deploys a Cloudflare Worker that only
your GitHub login can use, so Claude on the web and phone can list your tracker to-dos and tick
one off. It is added as a **custom connector**, not with `claude mcp add`:
1. Deploy it (SETUP.md). Your address will be `https://tracker-mcp.<your-subdomain>.workers.dev`.
2. claude.ai → Settings → Connectors → **Add custom connector** → name it "Tracker", URL
   `https://tracker-mcp.<your-subdomain>.workers.dev/mcp`.
3. Click Connect, approve the consent page, and sign in with the GitHub account you allow-listed.

Tools: `todo_list`, `todo_search` (read) and `todo_mark_done` (one write: marks one row done, by
exact id). No add, edit or delete tools on purpose.

### iMCP (Apple apps)
Install the app from https://github.com/mattt/iMCP, switch on only the services you want
(each one asks for macOS permission), then `claude mcp add -s user iMCP -- /Applications/iMCP.app/Contents/MacOS/imcp-server`.
Messages access is powerful: keep it off unless you need it.

### parallel-search (free web search)
`claude mcp add -s user --transport http parallel-search https://search.parallel.ai/mcp`

## claude.ai connectors (no code)

How to connect any of these: claude.ai → **Settings (Customize) → Connectors** → find it in the
directory → **Connect** → sign in to that service and approve. Desktop app: the same list under
Connectors. They then appear in every Claude Code session signed in to the same Claude account
(no `claude mcp add` needed). To limit what one can do, open it on the Connectors page and turn
individual tools off (for example everything that sends or deletes).

| Connector | What it does | Read / write | Setup notes |
|---|---|---|---|
| **Granola** | AI meeting notes: search and read your meeting transcripts and summaries, so Claude can pull action items into the tracker | Read | Needs the Granola Mac app and a Granola account; record a meeting in Granola first. Remote URL if you add it as a custom connector: `https://mcp.granola.ai/mcp` |
| **Gmail** | Search, read, label and draft email in ONE Google account | Read + write (send, trash, labels) | CLAUDE.md forbids sending or deleting without your explicit order; consider turning send/trash off. For several accounts use google-workspace |
| **Google Calendar** | List, search, create and update events in one account | Read + write | Same rule: no creating, moving or declining events without your order |
| **Google Drive** | Search and read Drive files (Docs export as text) in one account | Read + some write (create, share, trash) | Turn off share/trash if you only read |
| **Slack** | Search and read channels, threads and DMs in your workspaces; post messages and canvases | Read + write | Connect each workspace you want. Reading is the common use; posting needs your order |
| **Notion** | Search, read and edit pages and databases (planners, notes) | Read + write | Pick which pages Notion shares during Connect |
| **Spotify** | Search music, see what's playing, make playlists, save to library | Read + write (library, playlists) | Spotify account |
| **Apollo.io** | Company and people lookup, enrichment, job postings: job-search and outreach research | Read + write (contacts, sequences, sending) | Apollo account; credits are spent on enrichment, and Claude should show the cost first. Never let it send sequences without your order |
| **Exa** | Web search and page fetch tuned for AI ("find pages like..."), plus multi-step research runs | Read | Exa account (free tier available) |
| **Firecrawl** | Hosted scraping, search, and a catalog of structured data providers | Read | Firecrawl account; credit-limited, so crawl4ai (local, free) is the default and this is the backup |
| **Claude Docs** | Anthropic's living documents: Claude creates and edits shareable docs (memos, plans, notes) you can open on claude.ai | Read + write (docs it creates) | Nothing to set up beyond Connect |

## Browser and screen control (optional, last resort)

The rule is connectors first (see the `connectors_first` hook), but some sites have no API.

| Tool | What it's for | Setup |
|---|---|---|
| **Claude in Chrome** | Lets Claude drive your real Chrome (your sign-ins) for sites with no connector: filling a form you asked for, reading a page behind your login | Install the "Claude" extension from the Chrome Web Store, sign in, and pick it as the preferred browser in the desktop app. It asks per site; treat it as acting as you |
| **Built-in browser** (desktop app) | A separate browser pane inside the app, ideal for previewing your own dev servers and localhost pages | Nothing to install |
| **Playwright plugin** | Headless browser automation for testing web apps | `/plugin install playwright@claude-plugins-official` |
| **Computer use** | Lets Claude see the screen and click and type in native Mac apps (Finder, System Settings, apps with no API) | Desktop app → Settings → enable computer use, then approve each app when asked. Browsers are view-only and terminals click-only under it |

Neither should ever type your passwords: sign-ins stay yours.

## Built into the Claude desktop app (nothing to install)

| Server | What it does |
|---|---|
| **scheduled-tasks** | Create, run and manage scheduled Claude tasks (like the 7:40am morning brief) |
| **easel** | Draws charts inline when numbers over time or across categories are clearer as a picture |
| session, sidebar, terminal, browser tools | Start side sessions, group sessions in the sidebar, read your terminal tabs, preview pages |

Plugins (superpowers, plan-progress, playwright, design and deploy plugins, and more) are listed
with install commands in [`../../docs/plugins.md`](../../docs/plugins.md).

## Calling any MCP from a script: the bridge

Scheduled tasks and launchd jobs often don't have MCP tools loaded. `bridge/mcp_bridge.py`
starts a registered server, calls one tool and prints the result:
```bash
python3 ~/code/scripts/mcp_bridge.py canvas list
python3 ~/code/scripts/mcp_bridge.py canvas call list_announcements '{"course_identifier":"1234567"}'
```
See `../../docs/mcp-bridge.md`.
