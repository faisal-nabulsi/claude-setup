# Life vault

A **life vault** is a folder of plain Markdown files (`~/life/`) where Claude keeps durable
facts about you: people, organizations, projects, topics, places, and dated events. Every
Claude session can search it, so you stop re-explaining your life to each new chat.

Why a folder of files and not a database: you can read and edit it in any text editor (or
Obsidian), back it up with git, and grep it in milliseconds. Facts are **append-only**: an old
fact is never overwritten, only superseded by a newer entry, so you keep the history.

## What's in this module

| Piece | What it does |
|---|---|
| `vault-skeleton/` | An empty vault: folders, the tag vocabulary (`_index/tags.tsv`), and empty `_review/questions.md`, `deletions.md`, `conflicts.md`, `INBOX.md`. The installer copies it to `~/life/`. |
| `tools/bin/lv` | The CLI. `lv add` files one fact, `lv reindex` rebuilds the search index, `lv find <tag> <tag>` intersects tags, `lv health` says whether capture is working, `lv review` shows what the nightly sweep parked for you, `lv resolve` settles a contradiction. |
| `tools/bin/lv-capture` | A **Stop hook** (runs when a Claude session ends). It writes one tiny pointer file ("session X ended in folder Y") and nothing else. It never starts a process, so it can't slow Claude down or loop. |
| `tools/bin/lv-harvest` | Hourly launchd job. Reads the Claude Code transcripts the pointers name (`~/.claude/projects/*/*.jsonl`), keeps only the messages **you** typed, redacts secrets, and banks them in an evidence spool. No model, no network. |
| `tools/bin/lv-sweep` | Nightly launchd job (3:15am). The only part that calls a model (headless `claude -p`). It turns banked evidence into candidate facts, then runs **mechanical gates**: every name or number in a proposed fact must appear in your own words. Facts that pass are filed; unsure ones go to `_review/INBOX.md` for you. |
| `tools/bin/lv-forget` | Removes everything matching `_review/deletions.md`, every run, so a fact you asked to forget can't come back from a cached job. |
| `tools/bin/lv-install` | Registers the Stop hook in `~/.claude/settings.json` (backs it up first, merges with JSON, never hand-edits), installs the `/remember` skill, and writes the harvest + sweep launchd plists. `--uninstall` undoes it. |
| `tools/bin/lv-imessage`, `lv-mail` | **Opt-in** nightly feeds from Messages and mail. See Privacy below. Plists in `launchd-optin/`. |
| `tools/skills/remember/` | The `/remember <fact>` skill: file one fact right now, with the same gates. |

## How facts get in (three routes)

1. **Claude files them during a chat** because the CLAUDE.md "Life vault" rules tell it to
   (`lv add ...` with your exact words as the quote).
2. **You say `/remember <fact>`.**
3. **The capture loop** reads your past session history: Stop hook → hourly harvest → nightly
   sweep. This catches facts nobody filed by hand.

Every entry carries tags from a closed list (one per facet):
- `area/` admin, creative, family, finance, health, legal, school, social, work
- `type/` belief, credential, decision, event, fact, goal, obligation, preference, relationship
- `conf/` stated (you said it), confirmed (Claude said it, you agreed), derived (Claude inferred it; never overrides stated)
- `sens/` private (never leaves the machine without asking), normal

Example entry (in `people/casey-example/log.md`):
```
<!-- entry -->
## 2026-01-02 — Casey Example likes chess
subject: chess-preference
status: current
tags: area/social type/preference conf/stated sens/normal
source: user/stated/2026-01-02
quote: i play chess on weekends

Casey Example plays chess on weekends.
```

## Install

```bash
cp -R modules/life-vault/vault-skeleton/. ~/life/       # install.sh does this (never over an existing vault)
cp -R modules/life-vault/tools ~/life/tools
python3 -m venv ~/life/.venv && ~/life/.venv/bin/pip install pytest   # for lv-install's self-test
python3 ~/life/tools/bin/lv-install                     # hook + /remember skill + plists (not loaded)
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<prefix>.lifevault.harvest.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<prefix>.lifevault.sweep.plist
python3 ~/life/tools/bin/lv health
```
The jobs run under macOS's own `/usr/bin/python3`, and the sweep needs the `claude` CLI at
`~/.local/bin/claude`. The sweep uses your Claude plan's usage, once a night.

## Privacy (read before turning on the opt-in feeds)

- **Session history** (on by default with the capture loop): only the messages you typed are
  kept, after redaction. Secrets patterns (keys, tokens, card numbers) are dropped outright.
- **iMessage (opt-in):** `lv-imessage` reads `~/Library/Messages/chat.db`. It keeps only
  messages **you sent**, because a text someone sent you is their news, not a fact about you.
  macOS requires Full Disk Access for `/usr/bin/python3` to read that file, which is a big
  permission: give it only if you want this feed.
- **Mail (opt-in):** `lv-mail` reads incoming mail through the google-workspace MCP (read-only),
  for the addresses in `LIFE_VAULT_MAIL_ACCOUNTS`. Saved mail the sweep found nothing in is
  deleted after two weeks.
- Everything stays on your Mac. The only outside call is the nightly `claude -p` extraction,
  which goes to Anthropic like any Claude Code session.
- `sens/private` entries are never put into anything outbound without asking.
- To make the vault forget something, add a `pattern:` line to `_review/deletions.md` and run
  `lv-forget --apply`.

## Optional: a graph of the vault

[Graphify](https://github.com/Graphify-Labs/graphify) (`uv tool install graphifyy`) can build a
knowledge-graph view of a folder (`graphify-out/graph.html`). If you try it on your vault, run it on a copy with the
`sens/private` entries removed, because it sends file contents to a model to extract links.
