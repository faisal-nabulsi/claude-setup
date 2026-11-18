"""Ed Discussion (edstem.org) reads for the coursework MCP. READ-ONLY.

Uses Ed's API (https://us.edstem.org/api) with the user's personal API token, which they creates at
https://edstem.org/us/settings/api-tokens and stores themselves with ./set-ed-token.sh (.env, chmod 600).
Only GET requests. Thread reads do NOT pass view=1, so reading here doesn't mark posts as viewed.
No tools post, reply, comment, vote, star, edit or delete.
"""
from __future__ import annotations

import html
import re
from pathlib import Path

import httpx

API = "https://us.edstem.org/api"
ENV = Path(__file__).resolve().parent / ".env"


def _token() -> str:
    if ENV.exists():
        for line in ENV.read_text().splitlines():
            if line.startswith("ED_API_TOKEN="):
                tok = line.split("=", 1)[1].strip().strip("'\"")
                if tok:
                    return tok
    raise RuntimeError("No Ed API token yet. The user creates one at https://edstem.org/us/settings/api-tokens "
                       "and runs: sh ~/code/coursework-mcp/set-ed-token.sh")


def _get(path: str, params: dict | None = None):
    r = httpx.get(API + path, params=params, timeout=30,
                  headers={"Authorization": f"Bearer {_token()}", "Accept": "application/json"})
    if r.status_code == 401:
        raise RuntimeError("Ed rejected the token (expired or revoked). Make a new one and rerun set-ed-token.sh.")
    r.raise_for_status()
    return r.json()


def _plain(doc: str | None, limit: int = 4000) -> str:
    """Ed stores posts as XML-ish 'document' markup; turn it into readable text."""
    t = re.sub(r"<(paragraph|break|heading|list-item|callout)[^>]*>", "\n", doc or "")
    t = re.sub(r"<link href=\"([^\"]+)\"[^>]*>", r" [\1] ", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = re.sub(r"\n\s*\n+", "\n", html.unescape(t)).strip()
    return t if len(t) <= limit else t[:limit] + " [...]"


def courses() -> list:
    u = _get("/user")
    return [{"course_id": c["course"]["id"], "code": c["course"].get("code"), "name": c["course"].get("name"),
             "term": f'{c["course"].get("session", "")} {c["course"].get("year", "")}'.strip(),
             "status": c["course"].get("status"), "role": (c.get("role") or {}).get("role")}
            for c in u.get("courses", [])]


def _course_id(course: str) -> int:
    if str(course).isdigit():
        return int(course)
    want = course.lower().replace(" ", "")
    active = [c for c in courses() if c["status"] == "active"] or courses()
    for c in active:
        if want in (c["code"] or "").lower().replace(" ", "") or want in (c["name"] or "").lower().replace(" ", ""):
            return c["course_id"]
    raise LookupError(f"no Ed course matches {course!r}; see ed_courses")


def _thread_row(t: dict) -> dict:
    return {"id": t.get("id"), "number": t.get("number"), "title": t.get("title"), "type": t.get("type"),
            "category": " / ".join(x for x in (t.get("category"), t.get("subcategory")) if x),
            "created": t.get("created_at"), "replies": t.get("reply_count"), "votes": t.get("vote_count"),
            "pinned": t.get("is_pinned"), "answered": t.get("is_answered"), "staff_post": t.get("is_staff_post") or
            None, "preview": _plain(t.get("document"), 240)}


def threads(course: str, limit: int = 30, sort: str = "new", category: str | None = None,
            announcements_only: bool = False, query: str | None = None) -> dict:
    cid = _course_id(course)
    data = _get(f"/courses/{cid}/threads", {"limit": max(1, min(limit, 100)), "offset": 0, "sort": sort})
    rows = [_thread_row(t) for t in data.get("threads", [])]
    if announcements_only:
        rows = [r for r in rows if r["type"] == "announcement"]
    if category:
        rows = [r for r in rows if category.lower() in (r["category"] or "").lower()]
    if query:
        q = query.lower()
        rows = [r for r in rows if q in (r["title"] or "").lower() or q in (r["preview"] or "").lower()]
    return {"course_id": cid, "count": len(rows), "threads": rows}


def thread(thread_id: int) -> dict:
    d = _get(f"/threads/{int(thread_id)}")  # no view=1: reading here doesn't mark it viewed
    t = d.get("thread", d)

    def comments(cs, depth=0):
        out = []
        for c in cs or []:
            out.append({"depth": depth, "type": c.get("type"), "endorsed": c.get("is_endorsed"),
                        "created": c.get("created_at"), "text": _plain(c.get("document"), 2500)})
            out += comments(c.get("comments"), depth + 1)
        return out

    return {**_thread_row(t), "body": _plain(t.get("document"), 6000),
            "answers": comments(t.get("answers")), "comments": comments(t.get("comments")),
            "url": f"https://edstem.org/us/courses/{t.get('course_id')}/discussion/{t.get('id')}"}
