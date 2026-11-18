# Mods (Claude Code plugins that change the interface)

A **mod** is a Claude Code plugin with a JavaScript hooks module: it can draw a band above the
prompt, open a side panel, add a slash command, or react to each turn. These three live in a
local "marketplace" folder, so installing them is one settings change.

| Mod | What you see | Notes |
|---|---|---|
| `status-band` | A thin band above the prompt: how full the context window is (`▰▰▰▱▱ Chat 57% full`), your next deadlines today, a prompt queue, and a warning when a usage limit runs high. Rows appear only when there's something to say. `/q <prompt>` queues a prompt to send when the current turn ends; `/q` lists, `/q clear | pause | resume | drop N`. `/band off` hides it. | Deadlines come from `~/.claude/hooks/today.py --band` |
| `todo-panel` | `/todo` opens a to-do panel with no model call (instant, free): missed items, then each of the next 7 days with bold times, a colored course tag and short titles; self-imposed items are dimmed. `/todo text` prints the same list into the chat. | Reads `today.py --json`. Tags come from `~/semester-tracker/tags.json` |
| `model-router` | Picks a model and effort for each turn from what you typed: quick questions go to a small model at low effort, coding turns to a coding-strong model, essays and research get high effort. Override per turn by starting the prompt with `!opus`, `!sonnet`, `!haiku`, `!deep`, `!quick` or `!auto`. `/route` shows status; `/route off` turns it off. | Changing effort keeps the prompt cache; changing model doesn't, so it switches model only when the gain is clear. Model ids are in `hooks/classify.js`: update them when new models ship. |

## Install

1. Copy `modules/mods/` to `~/code/claude-mods/` (install.sh does this and fills in `{{HOME}}`).
2. Add the folder as a marketplace and enable the mods in `~/.claude/settings.json`:
   ```json
   "extraKnownMarketplaces": {
     "my-mods": {"source": {"source": "directory", "path": "/Users/YOU/code/claude-mods"}}
   },
   "enabledPlugins": {
     "status-band@my-mods": true,
     "todo-panel@my-mods": true,
     "model-router@my-mods": true
   }
   ```
3. Restart Claude Code. The folder also needs a marketplace manifest, which install.sh writes
   (`.claude-plugin/marketplace.json`).

The band and panel need the semester-tracker module's `today.py` hook; without it they just
show no deadlines.

## Other plugins worth knowing (upstream, install from their repos)

| Plugin | What it does | Where |
|---|---|---|
| superpowers | Process skills: brainstorming, writing plans, TDD, systematic debugging, verification before "done" | official marketplace (`claude-plugins-official`) |
| plan-progress | A live progress bar for multi-step work | https://github.com/zycck/claude-mods |
| agent-flow | Shows running subagents as a live flow | https://github.com/Charlie0113-T/claude-agent-flow |
| blast-radius, token-weather | Demo mods: hold risky commands for confirmation; context "weather" band | https://github.com/anthropics/claude-code-playground |
| frontend-design, playwright, vercel | Design guidance, browser automation, Vercel deploys | official marketplace |
