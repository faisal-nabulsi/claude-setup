# claude-setup

A complete, install-it-yourself Claude Code setup for a student who wants Claude to actually
know their life: their classes and deadlines, their accounts, and the facts that matter, with
phone pushes before anything is due and hard safety rules so it never sends, deletes or buys
anything without being told to.

Nothing in here is anyone's personal data. Every example (Casey Example, CS 101, course id
1234567) is fake. You bring your own accounts; the installer and Claude wire them up.

## Quickstart (about 20 minutes plus account setup)

```bash
git clone <this repo> ~/code/claude-setup
cd ~/code/claude-setup
./install.sh --dry-run          # see exactly what it would write (changes nothing)
```

Then open Claude Code in this folder and paste the prompt in **[BOOTSTRAP.md](BOOTSTRAP.md)**.
Claude interviews you (name, school, accounts, which modules), runs the installer, and finishes
each module with you, verifying as it goes. Prefer doing it by hand? `./install.sh` asks about
each module, and every module folder has its own README.

**Requirements:** macOS (launchd and Keychain are used throughout; Linux works with cron and a
`.env` instead), Claude Code, `python3`, `git`; `node` for the mods and some MCP servers;
[`uv`](https://docs.astral.sh/uv/) for the Python MCP servers.

**Installer promises:** never overwrites an existing file, backs up `~/.claude/settings.json`
before merging into it, safe to re-run, `--dry-run` touches nothing, and it never handles your
passwords or tokens (those go in through scripts you run yourself).

## Map of every piece

### Global brain
| Piece | One line |
|---|---|
| [`modules/core/CLAUDE.md.template`](modules/core/README.md) | Your `~/.claude/CLAUDE.md`: hard safety rules, research follow-ups, remote-box hygiene, plain-language writing, connectors-first, instructor exceptions, life-vault rules |
| [`modules/core/memory-example/`](modules/core/README.md) | How auto-memory works: one fact per file, typed frontmatter, `MEMORY.md` index, `[[links]]` |
| [`docs/memory-and-vault.md`](docs/memory-and-vault.md) | CLAUDE.md vs memory vs vault, and the capture loop |
| [`docs/safety.md`](docs/safety.md) | Every safety rule, why, and what enforces it |

### Life vault ([modules/life-vault](modules/life-vault/README.md))
| Piece | One line |
|---|---|
| `vault-skeleton/` | Empty `~/life/` vault: folders, tag vocabulary, review files (questions, deletions, conflicts, inbox) |
| `tools/bin/lv` | `add`, `reindex`, `find`, `health`, `review`, `resolve` |
| `lv-capture` | Stop hook: one pointer file per finished session |
| `lv-harvest` | Hourly: banks what you typed in Claude sessions (redacted, no model) |
| `lv-sweep` | Nightly: turns evidence into facts, with mechanical grounding gates |
| `lv-forget` | Re-applies your suppression list every run |
| `lv-imessage`, `lv-mail` | Opt-in nightly feeds from Messages and mail (privacy notes inside) |
| `/remember` | File one fact right now |

### Hooks ([modules/hooks](modules/hooks/README.md))
| Hook | One line |
|---|---|
| `today.py` | Puts your next deadlines into every session at start |
| `connectors_first.py` | Refuses the first browser/screen/AppleScript call per session in favor of a connector |
| `secrets_guard.py` | Blocks commands that would print Keychain secrets |
| `deadlines_guard.py` | Stops anything from deleting or un-finishing tracker rows |
| `icloud_guard.py` | Warns when a git repo sits in iCloud-synced Desktop/Documents |

### Semester tracker ([modules/semester-tracker](modules/semester-tracker/README.md))
| Piece | One line |
|---|---|
| `schedule.md` + `deadlines.json` + `config.json` | Source of truth for classes and deadlines (fake sample rows) |
| `verify/` | Union merge with stable ids where "done" always wins; mark done; sanity checks |
| `heartbeat/` | GitHub Actions job: deadline pushes, Canvas scan and reconcile, even with your Mac asleep |
| `gradescope/` | Copies Gradescope due dates into the tracker, 4×/day |
| `pollev/`, `assignment-watch/` | Phone push when a lecture poll or a surprise check-in opens (watch-only, never answers) |
| `scheduled-tasks/morning-brief/` | Daily 7:40am brief prompt: classes, readings, due, new Canvas deadlines, mail |
| `/hw`, `/verify-date` skills | One session per assignment; check a date across every source |

### Push notifications ([modules/ntfy](modules/ntfy/README.md))
| Piece | One line |
|---|---|
| ntfy explainer | What ntfy is, why the topic is a password, first push in 2 minutes, what it connects to |
| `examples/github-actions-job-alert/` | Every-2-hours internship alert to your phone |

### Connectors and MCP servers ([modules/mcp](modules/mcp/README.md))
| Server | One line |
|---|---|
| `bridge/mcp_bridge.py` | Call any MCP tool from a script or scheduled job ([docs](docs/mcp-bridge.md)) |
| canvas | Any school's Canvas, read-only, token in Keychain |
| google-workspace | Several Google accounts at once (mail, calendar, Drive, Docs, Sheets), with your own OAuth client |
| autohub | Your own Zapier: recipes that watch Canvas, grades or a store and push your phone |
| coursework | Gradescope, PollEv, Ed Discussion, read-only |
| crawl4ai | Free local web/PDF to Markdown scraper |
| espn-fantasy | Fantasy leagues; writes only behind a flag |
| calcentral | Berkeley student portal (optional, Berkeley students only) |
| lemonsqueezy | A Lemon Squeezy store, read-only gate (optional) |
| tracker | Remote to-do MCP on Cloudflare for Claude on the web and phone (optional) |
| iMCP, parallel-search | Apple apps on your Mac; free web search |
| claude.ai connectors | Granola, Gmail, Google Calendar, Google Drive, Slack, Notion, Spotify, Apollo.io, Exa, Firecrawl, Claude Docs: what each does, read vs write, how to connect |
| Tracker (custom connector) | Your tracker on claude.ai and the phone, through the tracker Worker |
| Claude in Chrome, computer use, built-in browser | Optional last-resort control, and what each is for |
| [`docs/plugins.md`](docs/plugins.md) | Every plugin used (superpowers, playwright, vercel, design, finance, plan-progress, agent-flow...) with install commands |

### Interface mods ([modules/mods](modules/mods/README.md))
| Mod | One line |
|---|---|
| status-band | Context-fullness bar, today's deadlines, a `/q` prompt queue, usage-limit warning |
| todo-panel | `/todo`: instant to-do panel, no model call |
| model-router | Routes each turn to the right model and effort; `!opus`, `!quick` overrides |

### Skills ([modules/skills](modules/skills/README.md)) and habits ([docs/workflow.md](docs/workflow.md))
`/hw`, `/verify-date`, `lock-in`, plus links to recommended upstream skills; one session per
assignment, sidebar groups, never bulk-archive, Remote Control from your phone.

## Third-party code

Upstream projects are linked, not copied: canvas-mcp, google-workspace-mcp, espn-fantasy-mcp,
Lemon Squeezy MCP, iMCP, superpowers and other plugins, unlazy, task-observer, agent-reach,
Graphify. `modules/mcp/tracker-mcp/` started from Cloudflare's remote-MCP GitHub-OAuth template
(see its README).
