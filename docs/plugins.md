# Plugins: what this setup uses and how to install each

A **plugin** bundles skills, hooks, slash commands, agents and sometimes MCP servers. Plugins
come from **marketplaces** (a GitHub repo or a local folder listing plugins). In Claude Code:

```text
/plugin marketplace add <owner/repo>        # once per marketplace
/plugin install <plugin>@<marketplace>      # each plugin
/plugin                                      # browse, enable, disable
```

Nothing here is copied into this repo; install from the source.

| Plugin | What it does | Install |
|---|---|---|
| **superpowers** | Process skills: brainstorming before building, writing plans, test-driven development, systematic debugging, verification before saying "done", parallel agents | `/plugin install superpowers@claude-plugins-official` |
| **frontend-design** | Design guidance so UI work doesn't look templated | `/plugin install frontend-design@claude-plugins-official` |
| **playwright** | Browser automation MCP (headless Chromium) for testing web apps | `/plugin install playwright@claude-plugins-official` |
| **vercel** | Vercel deploys, env vars, Next.js and AI SDK skills, deployment agents | `/plugin install vercel@claude-plugins-official` (sign in to Vercel when asked) |
| **ui-ux-pro-max** | Design-system, branding, slides and UI styling skills | `/plugin marketplace add nextlevelbuilder/ui-ux-pro-max-skill` then `/plugin install ui-ux-pro-max@ui-ux-pro-max-skill` |
| **financial-analysis**, **market-researcher**, **pitch-agent** | Finance skills and agents: DCF, comps, LBO, sector research, pitch decks. Their data connectors (FactSet, PitchBook, ...) need paid accounts; the modeling skills work without them | `/plugin marketplace add anthropics/financial-services-plugins` then `/plugin install financial-analysis@claude-for-financial-services` (and the other two) |
| **plan-progress** | A live progress bar above the prompt for multi-step work | `/plugin marketplace add zycck/claude-mods` then `/plugin install plan-progress@zycck-mods` |
| **agent-flow** | Shows running subagents as a live flow | `/plugin marketplace add Charlie0113-T/claude-agent-flow` then `/plugin install agent-flow@claude-agent-flow` |
| **blast-radius**, **token-weather** | Demo mods from Anthropic's playground: hold risky commands (deletes, force pushes) for your confirmation; a context "weather" band | `git clone https://github.com/anthropics/claude-code-playground ~/code/claude-code-playground`, then `/plugin marketplace add ~/code/claude-code-playground/claude-code/mods` and `/plugin install blast-radius@claude-code-playground-mods` |
| **status-band**, **todo-panel**, **model-router** | This repo's own mods | `modules/mods/README.md` |

## Skills installed outside plugins

| Skills | Install |
|---|---|
| Cloudflare (cloudflare, wrangler, workers-best-practices, durable-objects, agents-sdk, sandbox, turnstile, web-perf, ...) | Cloudflare's official agent skills, installed with the `skills` CLI (https://github.com/vercel-labs/skills) into `~/.claude/skills/` |
| unlazy, task-observer, agent-reach | `modules/skills/README.md` |

## Built in (no install)

The Claude desktop app ships its own servers: scheduled tasks, easel charts, sessions and
sidebar, terminal, built-in browser. Claude in Chrome is a browser extension, and computer use
is a desktop-app setting (see `modules/mcp/README.md`).

**Tip:** every enabled plugin adds tools and skill descriptions to each session's context. Enable
the ones you use weekly; disable the rest from `/plugin`.
