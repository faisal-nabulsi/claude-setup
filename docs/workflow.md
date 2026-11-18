# Workflow conventions

Habits that make this setup work day to day, in the Claude desktop app (Code tab) and on the phone.

## One session per assignment (or per task)

Keep one long-lived **planner** session in `~/semester-tracker` for the to-do list, and start a
separate session for each piece of real work. `/hw <assignment>` does this for homework: it
gathers the brief and leaves a one-click **chip** ("start session") with the whole brief inside.
Because: a long homework conversation fills the context window, and the planner then forgets
your deadlines. Small sessions also cost less.

## Sidebar groups

Group sessions in the sidebar by area ("Classes", "Projects", "Job search"). Claude can move
sessions into a group for you. Because: finding last week's session should take one glance.

## Never archive sessions in bulk

Don't turn on auto-archive, and don't ask Claude to "clean up" sessions. Archived sessions are
hard to find again, and the vault's capture loop reads session history. If the app feels slow,
check memory use first (`top -o mem`): a heavy browser tab or a swapping Mac is the usual cause,
not the number of sessions.

## Remote Control and your phone

Turn on **Remote Control** for a session to follow and steer it from the Claude phone app
while it runs on your Mac. Combined with ntfy pushes, you can start work at your desk, get a
push when it's done, and answer its question from anywhere. Keep the Mac awake for long runs.

## Connectors first

When something needs data from an account (Canvas, mail, Slack), say "use the connector"; the
hook enforces it anyway. If a connector can't do the job, Claude falls back to the browser and
tells you why in one line. When a connector gets fixed or added, update the note that said it
couldn't.

## Small Mac (8 GB) habits

- Close browser tabs Claude opened when it's done.
- Prefer local MCP servers that start once over tools that launch a browser per call.
- Run long jobs (tests, builds, crawls) in the background.

## Before anything leaves your Mac

Push, publish, email, post, invite: Claude asks first, every time, and shows you exactly what
goes where. That's in the CLAUDE.md hard rules; keep it.
