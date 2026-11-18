"""PollEv reads for the coursework MCP. WATCH-ONLY: never answers or changes anything.

- poll_open_now: one request per presenter to PollEv's public "is anything open?" endpoint, the
  same one the pollev.com page and semester-tracker/pollev/notifier.py use. No login.
- attendance: reads your local attendance record (attendance.md, kept by hand plus the
  pollev-reader block). PollEv's own response history sits behind Amazon's bot check (Oct 2026),
  and nothing here tries to get past it; the browser route in pollev/README.md stays the way to refresh it.
Never answer a poll for the user (that would fake attendance).
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

POLLEV_DIR = Path(os.environ.get("SEMESTER_TRACKER", Path.home() / "semester-tracker")) / "pollev"
ATTENDANCE = POLLEV_DIR.parent / "attendance.md"


def _mods():
    if str(POLLEV_DIR) not in sys.path:
        sys.path.insert(0, str(POLLEV_DIR))
    import config  # noqa: E402
    import notifier  # noqa: E402
    return config, notifier


def poll_open_now(course: str | None = None) -> dict:
    config, notifier = _mods()
    from concurrent.futures import ThreadPoolExecutor

    pairs = [(c["course"], h) for c in config.COURSES
             if not course or course.lower() in c["course"].lower() for h in c["hosts"]]
    with ThreadPoolExecutor(max_workers=4) as pool:  # each check is a ~20 s long poll; run them side by side
        results = list(pool.map(lambda p: notifier.poll_uid(p[1], timeout=25), pairs))
    out = [{"course": cname, "presenter": host, "open": bool(uid), "link": f"https://pollev.com/{host}",
            "http": status if status else "long-poll timeout (nothing open)"}
           for (cname, host), (status, uid) in zip(pairs, results)]
    now = datetime.now(config.TZ)
    lec = config.course_at(now)
    return {"checked_at": now.strftime("%a %b %d %I:%M %p"), "lecture_now": lec["course"] if lec else None,
            "presenters": out, "note": "Watch-only. The user answers polls themselves, in the room."}


def attendance(max_chars: int = 12000) -> str:
    if not ATTENDANCE.exists():
        return f"{ATTENDANCE} not found."
    text = ATTENDANCE.read_text()
    return text if len(text) <= max_chars else text[:max_chars] + f"\n[... cut at {max_chars} of {len(text)} chars]"
