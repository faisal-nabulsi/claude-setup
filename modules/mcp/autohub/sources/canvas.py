"""Your school's Canvas through its own REST API: GET requests only.

Uses autohub's own token, Keychain item "autohub:canvas", which the user stores themselves with
setup-secrets.sh. (The canvas connector's token stays with that connector's launcher.)
The token is passed only in the Authorization header and never printed or logged.
"""
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from html.parser import HTMLParser

from . import register

HOST = __import__("os").environ.get("CANVAS_HOST", "{{CANVAS_HOST}}")  # e.g. canvas.yourschool.edu
BASE = f"https://{HOST}/api/v1"
KEYCHAIN_ITEM = "autohub:canvas"
_token_cache = None


class Forbidden(Exception):
    """403/404: this student can't see that list (e.g. a hidden Pages tab). Callers skip it."""


def token() -> str:
    global _token_cache
    if _token_cache:
        return _token_cache
    r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_ITEM, "-w"],
                       capture_output=True, text=True, timeout=20)
    tok = r.stdout.strip()
    if r.returncode != 0 or not tok:
        raise RuntimeError("no Canvas token: run sh ~/code/autohub/setup-secrets.sh")
    _token_cache = tok
    return tok


def _next_link(headers) -> str:
    link = (headers or {}).get("Link") or ""
    for part in link.split(","):
        m = re.match(r'\s*<([^>]+)>\s*;\s*rel="next"', part)
        if m:
            return m.group(1)
    return None


def canvas_get(path: str, params=None):
    url = path if path.startswith("http") else BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    out, pages = None, 0
    while url and pages < 20:
        pages += 1
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token()}",
                                                   "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read().decode())
                url = _next_link(r.headers)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise RuntimeError("Canvas token rejected (expired?): run sh ~/code/autohub/setup-secrets.sh")
            if e.code in (403, 404):
                raise Forbidden(f"{e.code} {path}")
            raise RuntimeError(f"Canvas HTTP {e.code} on {path}")
        if isinstance(data, list):
            out = (out or []) + data
        else:
            return data
    return out if out is not None else []


# ---------- formatting ----------

def _local(ts: str):
    if not ts:
        return None
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)


def _clock(dt: datetime) -> str:
    return dt.strftime("%I:%M%p").lstrip("0").lower()


def _when(dt: datetime, now: datetime) -> str:
    if now and dt.date() == now.date():
        return _clock(dt)
    return dt.strftime("%a %-m/%-d ") + _clock(dt)


def _posted(ts, ctx) -> str:
    dt = _local(ts)
    return f" (posted {_when(dt, ctx.now if ctx else None)})" if dt else ""


def _due(ts) -> str:
    dt = _local(ts)
    return f" (due {dt.strftime('%a %-m/%-d ')}{_clock(dt)})" if dt else " (no due date)"


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self._href, self._text = [], None, []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href, self._text = dict(attrs).get("href"), []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._text).split())))
            self._href = None


NOISE_PARAMS = {"verifier", "wrap", "download_frd"}   # Canvas file-link extras; other params (v=, id=, url=) matter


def _norm_link(href: str) -> str:
    if not href or href.startswith(("#", "mailto:", "javascript:", "tel:")):
        return None
    full = urllib.parse.urljoin(f"https://{HOST}/", href)
    p = urllib.parse.urlsplit(full)
    if p.scheme not in ("http", "https"):
        return None
    query = urllib.parse.urlencode(sorted((k, v) for k, v in urllib.parse.parse_qsl(p.query)
                                          if k not in NOISE_PARAMS))
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path.rstrip("/") or "/", query, ""))


# ---------- source: things posted ----------

def _skip(params, cid, name) -> bool:
    rx = (params.get("skip_name_regex") or {}).get(cid)
    return bool(rx and name and re.search(rx, name))


def _safe(fn, *a):
    try:
        return fn(*a)
    except Forbidden:
        return []


@register("canvas_posted")
def fetch_posted(params: dict, ctx) -> list:
    courses = params["courses"]
    now = ctx.now if ctx else datetime.now()
    items = []

    # Canvas defaults end_date to start_date + 28 days, so both ends are given explicitly.
    since = (now - timedelta(days=14)).strftime("%Y-%m-%d")
    until = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    anns = _safe(canvas_get, "/announcements",
                 [("context_codes[]", f"course_{c}") for c in courses]
                 + [("start_date", since), ("end_date", until), ("per_page", 50)])
    for a in anns or []:
        cid = (a.get("context_code") or "").replace("course_", "")
        label = courses.get(cid, cid)
        items.append({"key": f"ann:{a['id']}",
                      "text": f"{label}: announcement '{a.get('title', '')}'{_posted(a.get('posted_at'), ctx)}"})

    for cid, label in courses.items():
        for a in _safe(canvas_get, f"/courses/{cid}/assignments", {"per_page": 100}) or []:
            if _skip(params, cid, a.get("name")):
                continue
            items.append({"key": f"asg:{a['id']}",
                          "text": f"{label}: new assignment '{a.get('name', '')}'{_due(a.get('due_at'))}"})

        for m in _safe(canvas_get, f"/courses/{cid}/modules", [("include[]", "items"), ("per_page", 50)]) or []:
            mitems = m.get("items")
            if mitems is None and m.get("items_url"):
                path = urllib.parse.urlsplit(m["items_url"]).path.replace("/api/v1", "", 1)
                mitems = _safe(canvas_get, path, {"per_page": 100})
            for it in mitems or []:
                if it.get("type") == "SubHeader" or _skip(params, cid, it.get("title")):
                    continue
                items.append({"key": f"mod:{it['id']}",
                              "text": f"{label}: '{it.get('title', '')}' added to {m.get('name', 'a module')}"})

        pages = _safe(canvas_get, f"/courses/{cid}/pages", {"sort": "created_at", "order": "desc", "per_page": 30})
        for p in pages or []:
            if os.environ.get("AUTOHUB_DEBUG_PAGES"):
                print(f"[pages] {label}: {p.get('title')!r} url={p.get('url')!r}", file=sys.stderr)
            items.append({"key": f"page:{p.get('page_id') or p.get('url')}",
                          "text": f"{label}: new page '{p.get('title', '')}'{_posted(p.get('created_at'), ctx)}"})

    watch = [(cid, f"/courses/{cid}/front_page") for cid in courses]
    watch += [(str(w["course"]), f"/courses/{w['course']}/pages/{w['page']}") for w in params.get("watch_pages", [])]
    for cid, path in watch:
        try:
            page = canvas_get(path)
        except Forbidden:
            continue
        if not isinstance(page, dict):
            continue
        parser = _Links()
        parser.feed(page.get("body") or "")
        label, seen_here = courses.get(cid, cid), set()
        for href, text in parser.links:
            norm = _norm_link(href)
            if not norm or norm in seen_here:
                continue
            seen_here.add(norm)
            short = (text or norm)[:60]
            items.append({"key": f"link:{cid}:{norm}",
                          "text": f"{label}: new link '{short}' on {page.get('title', 'a page')}"})
    return items
