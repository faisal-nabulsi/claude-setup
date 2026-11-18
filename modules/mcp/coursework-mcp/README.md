# coursework-mcp

A local, **read-only** MCP server for your day-to-day class sites: Gradescope, PollEv and Ed Discussion. The three share one server so a small Mac starts one process instead of three. Startup takes about 1 s, because each site's libraries load only when one of its tools is called.

## Tools

| Tool | What it does | How it gets in |
|---|---|---|
| `gradescope_courses(term)` | Lists their Gradescope courses (default: the term in the tracker's config.json) | The tracker reader's own login: Keychain items `gradescope-email` and `gradescope-password`, with the password sent only in the POST body |
| `gradescope_assignments(course, term, include_past)` | Shows due and late-due times (your timezone), submission status, grade and link | Same |
| `gradescope_reader_report` | Shows the last report from the launchd reader | Reads the local file `~/.local/state/gradescope-reader/report.json` |
| `pollev_open_now(course)` | Says whether a poll is open right now for the presenters in config.json | PollEv's public "is anything open?" endpoint, the same one the notifier uses; about 20 s |
| `pollev_attendance` | Shows your hand-kept attendance record | Reads the local file `semester-tracker/attendance.md` |
| `ed_courses` | Lists their Ed courses | Ed API with their token from `.env` |
| `ed_threads(course, ...)` | Lists recent threads with title, type, category, replies, whether answered, and a preview | Same |
| `ed_thread(id)` | Shows one thread with its answers and comments as plain text | Same. It does not pass `view=1`, so reading here doesn't mark the thread as viewed |

## What it will never do

- **Gradescope:** nothing that submits, uploads, resubmits or requests a regrade. The tracker file (`deadlines.json`) stays owned by the launchd reader, and this server doesn't write it.
- **PollEv:** it never answers a poll. Answering would fake attendance, and the user answers polls themselves, in the room. Their response history is behind Amazon's bot check, and this server does not try to get past it.
- **Ed:** it sends GET requests only. There is no posting, replying, voting, starring, editing or deleting.

## Setup (you do these)

- **Gradescope:** run the tracker reader's `bash ~/semester-tracker/gradescope/setup.sh` once (it stores the login in Keychain).
- **Ed:**
  1. Make a token at https://edstem.org/us/settings/api-tokens.
  2. Run `sh ~/code/coursework-mcp/set-ed-token.sh`. Your typing stays hidden, and the token is saved to `.env` (chmod 600).

  Claude never reads or prints `.env`.

## Registration and bridge

```bash
claude mcp add --scope user coursework -- ~/code/coursework-mcp/.venv/bin/python ~/code/coursework-mcp/server.py
python3 ~/code/scripts/mcp_bridge.py coursework call gradescope_assignments '{"course":"CS 101"}'
```

## Files

- `server.py`: the tools.
- `gs.py`: imports `semester-tracker/gradescope/reader.py` for its login and course-list fallback.
- `pollev.py`: imports `semester-tracker/pollev/config.py` and `notifier.py`.
- `ed.py`: the Ed API client.
- `set-ed-token.sh`: your one-time token setup.

**Environment:** `.venv` holds Python 3.12 with mcp 1.30.0, gradescopeapi 1.8.1 (pinned, the same as the reader), requests 2.34.2, beautifulsoup4 and httpx 0.28.1.

**Rebuild:**

```bash
cd ~/code/coursework-mcp
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python "mcp==1.30.0" "gradescopeapi==1.8.1" "requests==2.34.2" beautifulsoup4 "httpx==0.28.1"
```

## If something breaks

- **The Gradescope login fails:** the reader breaks too. Check `gradescope_reader_report`.
- **The PollEv presenters change:** edit the `pollev` block in `~/semester-tracker/config.json`.
- **Next term:** change `TERM` in `reader.py`, or pass `term`.
