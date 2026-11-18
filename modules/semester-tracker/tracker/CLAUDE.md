# Semester tracker — read this first

This file is a map. It holds no dates itself, so it can't go stale.

## Where to look (in this order; stop when answered)

| Question | Look in |
|---|---|
| When is X due / exam dates / rooms / rules / contacts | `schedule.md` (source of truth; section headings are stable) |
| What's due next, machine-readable | `deadlines.json` (merged with the heartbeat copy; done wins) |
| An instructor allowed something different | `EXCEPTIONS.md` (beats the printed rule everywhere) |
| Course material | `<course>/` → `readings/ slides/ notes/` |
| Anything not saved locally | `canvas` MCP for Canvas, `google-workspace` MCP for mail/Drive/calendar; browser only if those can't reach it |
| Gradescope due dates / submitted? | rows with `"source": "gradescope"` come from `gradescope/reader.py`; its last report is `~/.local/state/gradescope-reader/report.json` |

## When you learn something new, write it down here

- **A date, deadline, room, policy or contact** → edit `schedule.md`, add the row to `deadlines.json`, then run `python3 verify/sync_deadlines.py` (union merge by stable `id`; it fills the heartbeat copy). Commit and push both repos.
- **Something got finished** → `python3 verify/mark_done.py "<text or id>"`. Done rows are permanent tombstones: never delete them, never flip them back. A guard hook blocks cp/mv/redirects onto deadlines.json and undoes row removals.
- **Rows may carry a `short` title** (≤30 chars) used by the status band.
- **Facts about the user's life** → the life vault (`lv add`), not here.
- Never paste secrets (`.ntfy.env`, tokens) into any of these files.

## Gotchas

- Keep this folder out of iCloud-synced Desktop/Documents (or tag it: `xattr -w 'com.apple.fileprovider.ignore#P' 1 <folder>`): iCloud can evict files and corrupt `.git`.
- Commit on `main` only. A branch checkout can hide files that exist only on main.
- Scripts use fixed relative paths: run `python3 verify/check_files.py` after moving anything.
