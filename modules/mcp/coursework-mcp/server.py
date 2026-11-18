"""Coursework MCP server (Gradescope, PollEv, Ed Discussion). READ-ONLY.

- Gradescope: courses and assignments (due, late due, submitted, grade), via the tracker reader's
  own login (Keychain). No submit/upload/regrade tools.
- PollEv: is a lecture poll open right now (public endpoint) + your local attendance record (attendance.md).
  Watch-only: nothing answers polls.
- Ed Discussion: courses, threads, one thread with answers, via the user's API token (.env).
  GET only; reading doesn't mark posts viewed; no post/reply/vote tools.
Heavy imports happen inside tool calls, so the initialize handshake stays fast.
"""
from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("coursework", log_level="WARNING")


def _guard(fn, *a, **kw) -> str:
    try:
        r = fn(*a, **kw)
        return r if isinstance(r, str) else json.dumps(r, indent=1, ensure_ascii=False, default=str)
    except Exception as e:
        name = type(e).__name__
        hint = " (run semester-tracker/gradescope/setup.sh to store the Gradescope login in Keychain)" \
            if name == "SetupError" else ""
        return f"ERROR ({name}): {e}{hint}"


# ---------- Gradescope ----------

@mcp.tool()
def gradescope_courses(term: str | None = None) -> str:
    """The user's Gradescope courses for a term (default: the term in the tracker's config.json). term: 'Spring 2027'."""
    import gs
    return _guard(gs.list_courses, term)


@mcp.tool()
def gradescope_assignments(course: str | None = None, term: str | None = None, include_past: bool = False) -> str:
    """Gradescope assignments: due, late due (Pacific), submission status, grade / max grade, link.
    course: filter text ('CS 101', 'BIO'). include_past: also list past assignments already submitted
    (default lists open, upcoming and missed ones)."""
    import gs
    return _guard(gs.assignments, course, term, include_past)


@mcp.tool()
def gradescope_reader_report() -> str:
    """The launchd tracker reader's last report (failures, submitted-but-row-open, ambiguous matches)."""
    import gs
    return _guard(gs.reader_report)


# ---------- PollEv ----------

@mcp.tool()
def pollev_open_now(course: str | None = None) -> str:
    """Is a PollEv poll open right now for the presenters in config.json? One public check per presenter
    (each can take up to ~25 s when nothing is open). Watch-only: never answers a poll."""
    import pollev
    return _guard(pollev.poll_open_now, course)


@mcp.tool()
def pollev_attendance() -> str:
    """Your attendance record (attendance.md in the tracker, kept by hand)."""
    import pollev
    return _guard(pollev.attendance)


# ---------- Ed Discussion ----------

@mcp.tool()
def ed_courses() -> str:
    """The user's Ed Discussion courses (id, code, name, term, active/archived)."""
    import ed
    return _guard(ed.courses)


@mcp.tool()
def ed_threads(course: str, limit: int = 30, sort: str = "new", category: str | None = None,
               announcements_only: bool = False, query: str | None = None) -> str:
    """Recent Ed threads for a course: title, type (question/post/announcement), category, replies, votes,
    pinned/answered, preview. course: a course code like 'CS 101' or a numeric Ed course id.
    sort: 'new' (default), 'top', 'active'. query filters titles/previews of the fetched threads."""
    import ed
    return _guard(ed.threads, course, limit, sort, category, announcements_only, query)


@mcp.tool()
def ed_thread(thread_id: int) -> str:
    """One Ed thread with its body, answers and comments (endorsed flags), as plain text. Doesn't mark it viewed."""
    import ed
    return _guard(ed.thread, thread_id)


if __name__ == "__main__":
    mcp.run()
