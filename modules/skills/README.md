# Skills

A **skill** is a folder with a `SKILL.md`: instructions Claude loads when a task matches the
skill's description (or when you type `/<skill-name>`). Skills live in `~/.claude/skills/<name>/`.
Why skills: you write a good procedure once and every session follows it the same way.

## Included (written for this setup)

| Skill | What it does |
|---|---|
| `hw` | `/hw <assignment>`: gathers one assignment (due date, instructions, files, AI-use rule, instructor exceptions) and creates a one-click "start session" chip with the full brief, so each assignment gets its own session and the planner session stays small. Defaults to tutor-only when a no-AI rule applies. |
| `verify-date` | `/verify-date <thing>`: checks one date against the tracker, Canvas, Slack, email and calendars, and prints one table with conflicts flagged. Read-only. Runs on a cheaper model (`model: sonnet` in its frontmatter). |
| `lock-in` | For when work has gone sloppy: a fix failed twice, or you say "that's not it". Forces a stop, a written root-cause, and verification before the next attempt. |
| `remember` | `/remember <fact>`: files one fact in the life vault (in `modules/life-vault/tools/skills/`). |

## Recommended upstream skills (install from their repos)

| Skill | What it's for | Where |
|---|---|---|
| unlazy | Acceptance gates before long autonomous work; re-verify before reporting "done" | https://github.com/Leonxlnx/unlazy (MIT): `npx skills add Leonxlnx/unlazy` |
| task-observer ("One Skill to Rule Them All") | Notices patterns during work that should become new skills | https://github.com/rebelytics/one-skill-to-rule-them-all (CC BY 4.0) |
| agent-reach | YouTube transcripts, RSS feeds, GitHub search, pages normal fetch fails on (no-login channels only) | https://github.com/Panniantong/Agent-Reach |
| Cloudflare skills (wrangler, workers-best-practices, durable-objects, ...) | Building on Cloudflare | Cloudflare's official agent skills, installed with the `skills` CLI (https://github.com/vercel-labs/skills) |
| Vercel skills | Building and deploying on Vercel | the `vercel` plugin in the official marketplace |

## An idea you can build yourself: "write like me"

A skill that drafts emails and messages in your own voice works well: collect 20–50 things you
actually wrote (texts, emails, essays), have Claude write a short voice profile from them
(sentence length, greetings, punctuation habits, words you never use), and put that profile in
a `SKILL.md`. Keep your samples private and out of any shared repo. Always read a draft before
sending it.
