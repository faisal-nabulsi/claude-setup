"""Gradescope reads for the coursework MCP, READ-ONLY. Wraps the tracker's reader (semester-tracker/gradescope/reader.py).

Reuses the reader's own login (Keychain items "gradescope-email" / "gradescope-password", the
password sent only in the POST body, never printed) and its course-list fallback. It only GETs
course and assignment pages. There are no tools that submit, upload, resubmit, request regrades
or change anything on Gradescope.

The tracker (deadlines.json) is NOT touched here; the launchd reader still owns that.
Startup is cheap: requests/bs4/gradescopeapi are imported only on the first tool call.
"""
from __future__ import annotations

import json
import re
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

READER_DIR = Path(os.environ.get("SEMESTER_TRACKER", Path.home() / "semester-tracker")) / "gradescope"
TZ = ZoneInfo("America/Los_Angeles")
SESSION_TTL = 1800

_state = {"session": None, "at": 0.0}


def _reader():
    if str(READER_DIR) not in sys.path:
        sys.path.insert(0, str(READER_DIR))
    import reader  # noqa: E402  (the tracker's module; importing it changes nothing)
    return reader


def _session():
    r = _reader()
    if _state["session"] is None or time.time() - _state["at"] > SESSION_TTL:
        s = r.make_session()
        r.login(s, r.keychain("gradescope-email"), r.keychain("gradescope-password"))
        _state.update(session=s, at=time.time())
    return _state["session"]


def _fmt(dt):
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ).strftime("%a %b %d %Y %I:%M %p")


def _courses(term: str | None):
    r = _reader()
    from gradescopeapi.classes.account import Account
    s = _session()
    acct = Account(s, r.BASE)
    want = tuple(term.split()) if term else r.TERM
    found = {cid: c for cid, c in acct.get_courses()["student"].items() if (c.semester, c.year) == want}
    if not found and want == r.TERM:
        found = r.account_page_courses(s)
    return acct, found


def _list_courses(term):
    _, found = _courses(term)
    return [{"course_id": cid, "short_name": c.name, "name": c.full_name,
             "term": f"{c.semester} {c.year}"} for cid, c in found.items()]


def _assignments(course, term, include_past):
    r = _reader()
    acct, found = _courses(term)
    now = datetime.now(TZ)
    out = []
    for cid, c in found.items():
        label = r.course_name(c.name, c.full_name)
        if course and course.lower() not in f"{label} {c.name} {c.full_name} {cid}".lower():
            continue
        for a in acct.get_assignments(cid):
            due = a.due_date
            if due is not None and due.tzinfo is None:
                due = due.replace(tzinfo=TZ)
            if not include_past and (due is None or due < now) and r.is_submitted(a.submissions_status, a.grade):
                continue
            out.append({
                "course": label, "assignment": re.sub(r"\s+", " ", a.name or "").strip(),
                "assignment_id": a.assignment_id, "due": _fmt(a.due_date), "late_due": _fmt(a.late_due_date),
                "status": a.submissions_status, "submitted": r.is_submitted(a.submissions_status, a.grade),
                "grade": a.grade, "max_grade": a.max_grade,
                "url": f"{r.BASE}/courses/{cid}/assignments/{a.assignment_id}" if a.assignment_id else
                       f"{r.BASE}/courses/{cid}",
            })
    out.sort(key=lambda x: (x["due"] is None, x["due"] or ""))
    return {"term": term or " ".join(r.TERM), "assignments": out,
            "note": "Read-only. Due times are Pacific. The tracker row (if any) is updated by the launchd reader."}


def reader_report() -> str:
    p = Path.home() / ".local/state/gradescope-reader/report.json"
    return p.read_text() if p.exists() else "No report yet (~/.local/state/gradescope-reader/report.json missing)."


list_courses = _list_courses
assignments = _assignments
