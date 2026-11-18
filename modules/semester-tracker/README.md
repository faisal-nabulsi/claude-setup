# Semester tracker

One folder (`~/semester-tracker/`) that knows every class, deadline and rule for the term,
plus a cloud copy that pushes your phone before things are due, even when your laptop is asleep.

## The pieces

| Piece | What it is | Why |
|---|---|---|
| `tracker/schedule.md` | Human-readable source of truth: weekly schedule, key dates, no-class days, per-course rules, contacts, reminders. Stable section headings. | Claude and scripts look things up by heading, so the file can grow without breaking them. |
| `tracker/deadlines.json` | Machine-readable list of rows: `{"id", "when": "YYYY-MM-DDTHH:MM", "label", "short"?, "soft"?, "done"?, "done_at"?, "canvas_id"?}` | Scripts, hooks, the band and the phone pushes all read this. |
| `tracker/config.json` | Everything that is yours: timezone, term dates, Canvas course ids, Gradescope course names, PollEv presenters, watchers. Copy from `config.example.json`. | Scripts contain no personal values. |
| `tracker/verify/` | `deadlines_lib.py` (merge rules), `sync_deadlines.py` (merge the two copies), `mark_done.py` (finish one row in both copies), `check_files.py`, `check_deadlines.py`, `check_weekdays.py` (sanity checks). | See "The two copies" below. |
| `tracker/send-push.sh` | One phone push through ntfy (optional iMessage-to-self fallback). Reads `.ntfy.env`. | Every watcher calls this one script. |
| `tracker/deadline_nudge.py` | Local backup nudger (3h and 1h before each deadline). | For when the cloud heartbeat is down. |
| `heartbeat/` | A separate GitHub repo (`~/semester-heartbeat`) whose GitHub Actions workflow runs `heartbeat.py` every 30 minutes: morning mini-brief, 3h/1h deadline pushes, a twice-daily Canvas scan that adds new due dates, and a daily read-only reconcile against Canvas. | Pushes arrive even when the Mac is asleep or off. |
| `heartbeat/kicker/` | Optional Cloudflare Worker that triggers the workflow on a reliable cron (GitHub's own cron can run late or skip) and serves a tiny `/todo` JSON for an iPhone Shortcut (`GLANCE.md`). | Reliability. |
| `gradescope/` | Gradescope reader (launchd, 4×/day): copies Gradescope due dates into the tracker and marks rows it created done when Gradescope shows a submission. Read-only to Gradescope. | Gradescope deadlines often aren't on Canvas. |
| `pollev/` | PollEv notifier: pushes your phone when a lecture poll opens. **Watch-only: it never answers.** | Polls open for a minute; you answer in the room. |
| `assignment-watch/` | Pushes your phone when a Canvas assignment matching a pattern opens (e.g. a surprise in-class check-in). Watch-only. | Same idea, for Canvas. |
| `hooks/deadlines_guard.py` | Guard hook for deadlines.json (see below). | |
| `scheduled-tasks/morning-brief/` | Prompt template for a daily 7:40am Claude scheduled task: classes, readings, what's due, new Canvas deadlines, course mail, emailed to you plus one push. | A written brief beats remembering to check. |
| `tools/ocr-pdf.swift` | `swift ocr-pdf.swift file.pdf` writes a text copy of a scanned PDF using macOS's built-in Vision OCR. | Scanned readings have no text layer. |
| skills `/hw`, `/verify-date` | In `modules/skills/`. | |

## The two copies, and why "done wins"

`deadlines.json` exists twice: in the tracker (where you and Claude edit) and in the heartbeat
repo (where the cloud job adds Canvas rows and the phone tracker ticks things off). Copying one
over the other silently deletes rows. So:

- Every row has a stable `id` that never changes, even if its label does.
- `sync_deadlines.py` does a **union merge** by id: a row is never dropped because one copy lacks it.
- **Done wins:** if either copy says a row is done, it's done. Nothing flips back. Finished rows
  stay forever as tombstones ("DONE 10/3 — ... DO NOT RE-ADD"), so a merge can't resurrect them.
- `deadlines_guard.py` blocks `cp`/`mv`/redirects onto either copy and undoes any edit that
  drops a row or un-finishes one. Deliberate deletion: `touch ~/.claude/state/deadlines-guard/ALLOW_SHRINK`.

Daily use: add a row to `tracker/deadlines.json` → `python3 verify/sync_deadlines.py`;
finish something → `python3 verify/mark_done.py "<text or id>"`; then commit and push both repos.

## Set up

1. `install.sh` (pick "semester-tracker") copies `tracker/*`, `gradescope/`, `pollev/` and
   `assignment-watch/` into `~/semester-tracker/`, and `heartbeat/` into `~/semester-heartbeat/`.
   It `git init`s both. Keep them out of iCloud-synced folders.
2. Edit `~/semester-tracker/config.json`, `schedule.md` and `tags.json`. Replace the example rows
   in `deadlines.json` (keep the ids unique).
3. ntfy: `modules/ntfy/README.md`, then put your topic in `~/semester-tracker/.ntfy.env`
   (`NTFY_TOPIC=...`, chmod 600). Test: `bash ~/semester-tracker/send-push.sh "hello"`.
4. Heartbeat: create a **private** GitHub repo, push `~/semester-heartbeat` to it, copy
   `heartbeat-config.example.json` to `heartbeat-config.json` and edit it, then add repo secrets
   `NTFY_TOPIC` and (optional) `CANVAS_TOKEN`. Run the workflow once from the Actions tab.
   Dry run locally: `HB_DRY_RUN=1 python3 heartbeat.py`.
5. Gradescope (optional): `bash ~/semester-tracker/gradescope/setup.sh` (you type the password
   into macOS's own prompt), then `reader.py --dry-run`, then load `gradescope-reader.plist`.
6. Watchers (optional): add the `pollev` / `assignment_watch` blocks to config.json, edit the
   times in their plists, and load them with `launchctl bootstrap gui/$(id -u) <plist>`.
7. Morning brief (optional): fill in the `{{...}}` values in
   `scheduled-tasks/morning-brief/SKILL.md` and create it as a scheduled task in the Claude
   desktop app (Code tab → Scheduled), or ask Claude to create it.
