"""CalCentral + Berkeley class search MCP server. READ-ONLY.

CalCentral tools read the user's own data (schedule, enrollment appointments, holds, billing, grades,
degree progress) from the JSON feeds CalCentral's own web pages use, with the session they signed in
to themselves (see session.py). Class tools read the public schedule (classes.berkeley.edu) and
Berkeleytime grade distributions, no login.

There are deliberately NO tools that enroll, drop, swap, edit a cart, accept anything, or pay.
Every CalCentral request is a GET (or a GraphQL *query*, never a mutation) to an allow-listed path.
The user's rule: nothing is enrolled, dropped or paid on their behalf without asking item by item.

Startup is cheap: Playwright is imported only for the rare silent session refresh.
"""
from __future__ import annotations

import json
import re

from mcp.server.fastmcp import FastMCP

import calc
import classes
from session import SignInRequired

mcp = FastMCP("calcentral", log_level="WARNING")


def _out(obj) -> str:
    return json.dumps(obj, indent=1, ensure_ascii=False, default=str)


def _guard(fn, *a, **kw) -> str:
    try:
        return _out(fn(*a, **kw))
    except SignInRequired as e:
        return f"SIGN-IN NEEDED: {e}"
    except Exception as e:  # report, don't crash the server
        return f"ERROR ({type(e).__name__}): {e}"


# ---------------- CalCentral (the user's own record) ----------------

@mcp.tool()
def calcentral_status() -> str:
    """Is the saved CalCentral session still signed in? Returns signed_in true/false and, if false,
    how the user signs in again (they do it themselves; Claude never types the password)."""
    return _guard(calc.status)


@mcp.tool()
def my_schedule(term: str | None = None) -> str:
    """The user's classes for a term from CalCentral: enrolled and waitlisted sections (waitlist position
    when CalCentral gives it), units, times, rooms, instructors, grading option.
    term: e.g. 'Spring 2027' or 'Fall 2026'. Default: every term CalCentral currently lists (current + next)."""
    return _guard(calc.schedule, term)


@mcp.tool()
def enrollment_info(term: str | None = None) -> str:
    """The user's enrollment appointments for a term (Phase 1 / Phase 2 / Adjustment start and end times),
    unit limits per phase, and the term's add/drop and grading-option deadlines, from CalCentral's
    Class Enrollment card. term: e.g. 'Spring 2027'. Default: all terms shown."""
    return _guard(calc.enrollment, term)


@mcp.tool()
def my_holds() -> str:
    """Holds on the user's record (registration, enrollment, transcript, etc.) from CalCentral. Empty list = no holds."""
    return _guard(calc.holds)


@mcp.tool()
def my_billing() -> str:
    """The user's CalCentral billing: account balance, amount due now, upcoming charges/installments with
    due dates (including tuition payment-plan installments), and recent payments. Read-only."""
    return _guard(calc.billing)


@mcp.tool()
def my_grades() -> str:
    """The user's grades by term and their GPA, straight from CalCentral's JSON (never scraped from the page,
    which has shown a wrong GPA before). Includes cumulative GPA, units, and each course's grade."""
    return _guard(calc.grades)


@mcp.tool()
def degree_progress() -> str:
    """The user's degree progress (university, college and major requirement status) as CalCentral reports it."""
    return _guard(calc.degree_progress)


@mcp.tool()
def calcentral_get(path: str) -> str:
    """Escape hatch for any other CalCentral JSON feed: GET an allow-listed read-only /api/ path and return
    the raw JSON (trimmed). Example paths: /api/my/academics, /api/my/registrations, /api/my/standings,
    /api/my/finances/billing_items, /api/campus_solutions/financial_aid_data, /api/academics/transfer_credits.
    Paths that change anything (view_as, google, sir_response, record_first_login, ...) are refused."""
    return _guard(calc.raw_get, path)


# ---------------- Public class schedule + grade distributions ----------------

@mcp.tool()
def class_seats(course: str, term: str | None = None, section: str = "001", kind: str = "LEC") -> str:
    """Live seats for one class section from classes.berkeley.edu: enrolled/capacity, open seats,
    waitlisted/waitlist max, reserved seats (total, open, and the reserved-seat rules), meeting times,
    instructor, units, class number, and the term's general enrollment phase windows.
    course: 'MATH 1A', 'CHEM 1A'. term: 'Spring 2027' (default: the term now being enrolled).
    section: '001'. kind: LEC, DIS, LAB, SEM. If the URL guess fails, it searches and lists the real sections.
    Requests are throttled (one every 3+ s) and cached 10 minutes, so ask for what you need."""
    return _guard(classes.seats, course, term, section, kind)


@mcp.tool()
def class_search(query: str = "", term: str | None = None, filters: list[str] | None = None, page: int = 0,
                 limit: int = 25) -> str:
    """Search the Berkeley class schedule (classes.berkeley.edu). Returns sections with title, instructor,
    days/times, room, units, class number, open/reserved seat summary, and the section URL.
    query: keywords or a course code ('CHEM 1A', 'health economics'). The site's keyword search is fuzzy:
    check the 'section' field. term: 'Spring 2027' (default: the term now being enrolled).
    filters: raw facet strings as the site uses them, e.g. ['breadth_requirements:Biological Science'],
    ['breadth_requirements:International Studies']. page: 0-based results page (about 30 per page)."""
    return _guard(classes.search, query, term, filters, page, limit)


@mcp.tool()
def grade_distribution(course: str, instructor: str | None = None) -> str:
    """Historical grade distribution for a course from Berkeleytime: average GPA, % A-range, % A/A+,
    letter-graded count, full letter counts. instructor ('Josh Grossman') adds that instructor's own numbers."""
    return _guard(classes.grades, course, instructor)


if __name__ == "__main__":
    mcp.run()
