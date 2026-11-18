#!/usr/bin/env python3
"""Cloud heartbeat for your semester tracker. Runs on GitHub Actions about every 30 min
(see .github/workflows/heartbeat.yml), so pushes go out even when your laptop is asleep.

Jobs per run (each one is idempotent through state.json):
  1. Daily brief (~7:30am local): today's classes from heartbeat-config.json plus anything
     due in the next 72 hours.
  2. Deadline nudges: a push 3 hours and 1 hour before every open row in deadlines.json.
     "soft" rows (self-imposed targets) get one calm reminder instead.
  3. Readings push (~7am): what to read before today's classes (readings.json, optional).
  4. To-do push (~7am): open items in todo.json (optional).
  5. Canvas scan (twice daily, after 6am and after 2pm): new assignment due dates from your
     Canvas courses are appended to deadlines.json, with one heads-up push.
  6. Canvas reconciler (once a day, after 7am): READ-ONLY check of deadlines.json against
     Canvas (moved due dates, vanished assignments, untracked ones). Writes reconcile.json and
     pushes only when something is off.
  7. Token alert: if Canvas rejects CANVAS_TOKEN, one push per day saying to replace it.

state.json (committed back by the workflow) makes every send idempotent, so cron jitter can
neither double-push nor skip:
  "nudge|<when>|<label>|<stage>"  "scan|<date>|am|pm"  "reconcile|<date>"  "brief|<date>"

Env (GitHub repo secrets): NTFY_TOPIC (your ntfy topic), CANVAS_TOKEN (optional),
NTFY_SERVER (optional, default https://ntfy.sh), NTFY_TOKEN (optional, protected topics).
Test hooks: HB_TEST_NOW=YYYY-MM-DDTHH:MM (local time), HB_DRY_RUN=1.
"""
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

DIR = Path(__file__).resolve().parent
_CFG_PATH = DIR / "heartbeat-config.json"
if not _CFG_PATH.exists():
    _CFG_PATH = DIR / "heartbeat-config.example.json"  # copy it to heartbeat-config.json and edit
CFG = json.loads(_CFG_PATH.read_text())
TZ = ZoneInfo(CFG.get("timezone", "America/Los_Angeles"))
STATE_PATH = DIR / "state.json"
DEADLINES_PATH = DIR / "deadlines.json"
TODO_PATH = DIR / "todo.json"
RECONCILE_PATH = DIR / "reconcile.json"

SEMESTER_END = date.fromisoformat(CFG["term_end"])
LAST_INSTRUCTION = date.fromisoformat(CFG.get("last_instruction", CFG["term_end"]))
HOLIDAYS = {date.fromisoformat(d) for d in CFG.get("holidays", [])}
COURSES = {f"course_{cid}": name for cid, name in CFG.get("canvas_courses", {}).items()}
CANVAS_HOST = CFG.get("canvas_host", "canvas.instructure.com")
PUSH_TITLE = CFG.get("push_title", "Semester")
CANVAS_EVENTS_URL = f"https://{CANVAS_HOST}/api/v1/calendar_events"
CANVAS_MAX_PAGES = 20
SCAN_DAYS_AHEAD = 21                     # the twice-daily scan adds rows in [today, today+21]
RECON_DAYS_BACK, RECON_DAYS_AHEAD = 7, 30  # the daily reconciler compares this window
CANVAS_AUTH_ALERT = (
    "🔑 Canvas token stopped working — the heartbeat can't see new Canvas deadlines "
    "until it's replaced; make a new token in Canvas Account → Settings → Approved Integrations "
    "and update the CANVAS_TOKEN repo secret"
)


def now_local() -> datetime:
    t = os.environ.get("HB_TEST_NOW")
    return datetime.fromisoformat(t).replace(tzinfo=TZ) if t else datetime.now(TZ)


def send_push(body: str, media_url: str | None = None, priority: str | None = None) -> bool:
    body = body[:500]
    if os.environ.get("HB_DRY_RUN") == "1":
        print(f"DRY-RUN PUSH{f' [{priority}]' if priority else ''}: {body}"
              + (f" [attach: {media_url}]" if media_url else ""))
        return True
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        print("NTFY_TOPIC secret not set — skipping send (add it in repo Settings > Secrets)")
        return False
    server = os.environ.get("NTFY_SERVER") or "https://ntfy.sh"
    req = urllib.request.Request(f"{server.rstrip('/')}/{topic}", data=body.encode("utf-8"))
    req.add_header("Title", PUSH_TITLE)
    if os.environ.get("NTFY_TOKEN"):
        req.add_header("Authorization", f"Bearer {os.environ['NTFY_TOKEN']}")
    if priority:
        req.add_header("Priority", priority)  # ntfy: "high" / "urgent"
    if media_url:
        req.add_header("Attach", media_url)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print(f"push sent (http {r.status}): {body[:60]}")
            return True
    except Exception as e:  # noqa: BLE001 — log and keep the run alive for other sends
        print(f"push FAILED: {e}")
        return False


def load_deadlines() -> list[tuple[datetime, str, bool]]:
    # A "soft" entry is a self-imposed target, not a real due date. It gets ONE calm
    # reminder instead of the 3h/1h urgent pair, so the urgent wording keeps meaning something.
    # Rows marked done are tombstones that stay in the file forever (so a merge can never
    # resurrect them). They must never be nudged.
    return [
        (datetime.fromisoformat(d["when"]).replace(tzinfo=TZ), d["label"],
         bool(d.get("soft")))
        for d in json.loads(DEADLINES_PATH.read_text())
        if not d.get("done")
    ]


def run_nudges(now: datetime, state: dict) -> None:
    fired = 0
    for when, label, soft in load_deadlines():
        hours_left = (when - now).total_seconds() / 3600
        if hours_left <= 0:
            continue
        key3 = f"nudge|{when.isoformat()}|{label}|3h"
        key1 = f"nudge|{when.isoformat()}|{label}|1h"
        due = when.strftime("%-I:%M%p").lower()
        if soft:
            keyS = f"nudge|{when.isoformat()}|{label}|soft"
            if hours_left <= 1.5 and keyS not in state:
                if send_push(f"📓 {label}", priority="default"):
                    state[keyS] = now.isoformat()
                    fired += 1
            continue
        if 1.5 < hours_left <= 3.5 and key3 not in state:
            if send_push(f"⏰ 3 HRS LEFT: {label} — due {due}", priority="high"):
                state[key3] = now.isoformat()
                fired += 1
        elif hours_left <= 1.5 and key1 not in state:
            if send_push(f"🚨 1 HR LEFT: {label} — due {due}. Submit now!", priority="urgent"):
                state[key1] = now.isoformat()
                fired += 1
    if not fired:
        print("no nudges to send")


def _known(existing: list[dict], when_local: str, course: str, title: str) -> bool:
    title_words = set(re.findall(r"[A-Z0-9]+", title.upper()))
    for d in existing:
        if d["when"][:10] != when_local[:10]:
            continue
        label = d["label"].upper()
        course_tag = course.split()[0].upper() if course else ""
        same_course = bool(course_tag) and course_tag in label
        overlap = len(title_words & set(re.findall(r"[A-Z0-9]+", label)))
        if overlap >= 2 and (same_course or d["when"] == when_local):
            return True
    return False


class CanvasAuthError(Exception):
    """Canvas rejected CANVAS_TOKEN (revoked or expired). Never carries the token."""


def _is_canvas_auth_failure(e: urllib.error.HTTPError) -> bool:
    # 401 = Canvas's answer for a revoked/expired/garbage token. A 403 is
    # usually a permissions problem, so it only counts when the body says the
    # token itself is bad.
    if e.code == 401:
        return True
    if e.code != 403:
        return False
    try:
        body = e.read(4000).decode("utf-8", "replace").lower()
    except Exception:  # noqa: BLE001
        return False
    return bool(re.search(r"invalid[ _]access[ _]token|invalid_token|token (?:has )?expired"
                          r"|expired[ _]token", body))


def _next_page(link_header: str) -> str | None:
    # Canvas paginates with an RFC 5988 Link header: <url>; rel="next", ...
    for part in link_header.split(","):
        m = re.match(r'\s*<([^>]+)>\s*;.*rel="?next"?', part)
        if m:
            nxt = urllib.parse.urlsplit(m[1])
            if nxt.scheme != "https" or nxt.hostname != CANVAS_HOST:
                # never send the token anywhere but Canvas; a truncated
                # fetch would fake "missing on Canvas", so fail the whole fetch
                raise RuntimeError("Canvas pagination pointed off Canvas")
            return m[1]
    return None


def fetch_canvas_events(token: str, start: date, end: date) -> list[dict]:
    """Every assignment calendar event for COURSES due start..end (both
    inclusive, Canvas reads the dates in the account's time zone), following
    pagination. Raises CanvasAuthError when Canvas rejects the token; any
    other failure propagates so the caller can retry next run."""
    params = [("type", "assignment"), ("per_page", "100"),
              ("start_date", start.isoformat()), ("end_date", end.isoformat())]
    params += [("context_codes[]", c) for c in COURSES]
    url: str | None = CANVAS_EVENTS_URL + "?" + urllib.parse.urlencode(params)
    events: list[dict] = []
    for _ in range(CANVAS_MAX_PAGES):
        req = urllib.request.Request(url)
        # unredirected: if Canvas ever redirects, the token is not forwarded
        req.add_unredirected_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                page = json.load(r)
                if not isinstance(page, list):
                    raise RuntimeError("Canvas returned a non-list page")
                events += page
                url = _next_page(r.headers.get("Link") or "")
        except urllib.error.HTTPError as e:
            if _is_canvas_auth_failure(e):
                raise CanvasAuthError(f"HTTP {e.code}") from None
            raise
        if not url:
            return events
    raise RuntimeError(f"Canvas returned more than {CANVAS_MAX_PAGES} pages")


def alert_canvas_auth(now: datetime, state: dict) -> None:
    key = f"canvas-auth-alert|{now.date().isoformat()}"
    if key in state:
        print("canvas token alert already sent today")
        return
    if send_push(CANVAS_AUTH_ALERT, priority="high"):
        state[key] = now.isoformat()


def canvas_events(now: datetime, state: dict, start: date, end: date, what: str) -> list[dict] | None:
    """fetch_canvas_events with the heartbeat's error policy: None on any
    failure (callers retry next run), plus the once-a-day push when the token
    itself is dead. Logs never include the token or the request URL."""
    token = os.environ.get("CANVAS_TOKEN")
    if not token:
        print(f"no CANVAS_TOKEN — {what} skipped")
        return None
    try:
        return fetch_canvas_events(token, start, end)
    except CanvasAuthError as e:
        print(f"{what} FAILED: Canvas rejected CANVAS_TOKEN ({e}) — revoked or expired")
        alert_canvas_auth(now, state)
    except Exception as e:  # noqa: BLE001 — network blip etc.: quiet retry
        print(f"{what} FAILED (will retry next run): {type(e).__name__}: {e}")
    return None


def _event_fields(ev: dict) -> tuple[str, str, str] | None:
    """(when_local "YYYY-MM-DDTHH:MM", course, title) of a Canvas assignment
    event, or None when it has no due date or title."""
    assignment = ev.get("assignment") or {}
    due = assignment.get("due_at") or ev.get("end_at") or ev.get("start_at")
    title = (ev.get("title") or "").strip()
    if not due or not title:
        return None
    course = COURSES.get(ev.get("context_code", ""), "")
    when_local = (datetime.fromisoformat(due.replace("Z", "+00:00"))
                  .astimezone(TZ).strftime("%Y-%m-%dT%H:%M"))
    return when_local, course, title


def scan_window(now: datetime) -> tuple[date, date]:
    return now.date(), now.date() + timedelta(days=SCAN_DAYS_AHEAD)


def run_canvas_scan(now: datetime, state: dict) -> None:
    # Twice daily: morning (first run after 6am) and afternoon (first run after
    # 2pm) — the afternoon pass catches same-day assignments like same-day check-ins,
    # which unlock ~1pm and are due 11:59pm, in time for the evening nudges.
    half = "pm" if now.hour >= 14 else "am"
    key = f"scan|{now.date().isoformat()}|{half}"
    if key in state or now.hour < 6:
        return
    events = canvas_events(now, state, *scan_window(now), "canvas scan")
    if events is None:
        return
    existing = json.loads(DEADLINES_PATH.read_text())
    known_ids = {d.get("canvas_id") for d in existing if d.get("canvas_id")}
    added = []
    for ev in events:
        fields = _event_fields(ev)
        if not fields:
            continue
        if ev.get("id") in known_ids:
            continue
        when_local, course, title = fields
        if _known(existing, when_local, course, title):
            continue
        # "id" is the stable key the tracker's sync_deadlines.py merges on.
        entry = {"when": when_local, "label": f"{course} {title}".strip(),
                 "canvas_id": ev.get("id"), "id": str(ev.get("id"))}
        existing.append(entry)
        added.append(entry)
    state[key] = now.isoformat()
    if added:
        existing.sort(key=lambda d: d["when"])
        DEADLINES_PATH.write_text(json.dumps(existing, indent=1, ensure_ascii=False) + "\n")
        lines = "; ".join(
            f"{e['label']} due {datetime.fromisoformat(e['when']).strftime('%a %-m/%-d %-I:%M%p').lower()}"
            for e in added[:4]
        )
        send_push(f"📌 New deadline(s) posted on Canvas: {lines}")
        print(f"scan: added {len(added)} deadline(s)")
    else:
        print("scan: no new deadlines")


def reconcile(events: list[dict], rows: list[dict], now: datetime) -> list[dict]:
    """Compare Canvas assignment events with deadlines.json rows. Pure: reads,
    never edits. Mismatch types:
      date_changed       tracked row (canvas_id, not done, not soft) whose
                         Canvas due time differs from its "when"
      missing_on_canvas  tracked row due inside the window whose assignment
                         is no longer on Canvas
      not_in_tracker     Canvas assignment due inside the window with no row
                         (by id or the scan's fuzzy _known match), skipping the
                         ones the twice-daily scan adds by itself
    Done rows are never reported (but still count as "in the tracker"). Soft
    rows carry a self-chosen time, so they are not checked for date drift."""
    today = now.date()
    lo, hi = today - timedelta(days=RECON_DAYS_BACK), today + timedelta(days=RECON_DAYS_AHEAD)
    scan_lo, scan_hi = scan_window(now)

    def day(when: str) -> date:
        return date.fromisoformat(when[:10])

    on_canvas: dict[str, tuple[str, str, str]] = {}
    for ev in events:
        fields = _event_fields(ev)
        if fields and ev.get("id"):
            on_canvas[str(ev["id"])] = fields
    in_tracker = {str(r[k]) for r in rows for k in ("canvas_id", "id") if r.get(k)}
    out: list[dict] = []
    for r in rows:
        cid = r.get("canvas_id")
        if not cid or r.get("done"):
            continue
        hit = on_canvas.get(str(cid))
        if hit:
            if hit[0] != r["when"] and not r.get("soft"):
                out.append({"type": "date_changed", "id": r.get("id"), "canvas_id": cid,
                            "label": r["label"], "canvas_name": f"{hit[1]} {hit[2]}".strip(),
                            "old": r["when"], "new": hit[0]})
        elif lo <= day(r["when"]) <= hi:
            out.append({"type": "missing_on_canvas", "id": r.get("id"), "canvas_id": cid,
                        "label": r["label"], "when": r["when"]})
    for cid, (when_local, course, title) in sorted(on_canvas.items(), key=lambda kv: (kv[1][0], kv[0])):
        if cid in in_tracker or not lo <= day(when_local) <= hi:
            continue
        if scan_lo <= day(when_local) <= scan_hi:
            continue  # the twice-daily scan adds these itself
        if _known(rows, when_local, course, title):
            continue
        out.append({"type": "not_in_tracker", "canvas_id": cid,
                    "label": f"{course} {title}".strip(), "when": when_local})
    return out


def _short(text: str, n: int = 40) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def _fmt_when(when: str) -> str:
    return datetime.fromisoformat(when).strftime("%a %-m/%-d %-I:%M%p").lower()


def reconcile_push_text(mismatches: list[dict], shown: int = 4) -> str:
    lines = []
    for m in mismatches[:shown]:
        if m["type"] == "date_changed":
            lines.append(f"{_short(m['canvas_name'])}: date changed "
                         f"{_fmt_when(m['old'])} → {_fmt_when(m['new'])}")
        elif m["type"] == "missing_on_canvas":
            lines.append(f"{_short(m['label'])}: missing on Canvas (was {_fmt_when(m['when'])})")
        else:
            lines.append(f"{_short(m['label'])}: not in tracker (due {_fmt_when(m['when'])})")
    more = len(mismatches) - shown
    n = len(mismatches)
    return (f"🔎 Canvas vs tracker, {n} mismatch{'es' if n > 1 else ''}: " + " | ".join(lines)
            + (f" | +{more} more" if more > 0 else "") + " (see reconcile.json)")


def run_reconcile(now: datetime, state: dict) -> None:
    # Once a day, first run after 7am. Fetches one day wider than it compares
    # on each side, so a time-zone edge can't fake "missing on Canvas".
    key = f"reconcile|{now.date().isoformat()}"
    if key in state or now.hour < 7:
        return
    start = now.date() - timedelta(days=RECON_DAYS_BACK + 1)
    end = now.date() + timedelta(days=RECON_DAYS_AHEAD + 1)
    events = canvas_events(now, state, start, end, "canvas reconcile")
    if events is None:
        return
    rows = json.loads(DEADLINES_PATH.read_text())
    mismatches = reconcile(events, rows, now)
    RECONCILE_PATH.write_text(json.dumps({
        "checked_at": now.isoformat(timespec="minutes"),
        "window": {"from": (now.date() - timedelta(days=RECON_DAYS_BACK)).isoformat(),
                   "to": (now.date() + timedelta(days=RECON_DAYS_AHEAD)).isoformat()},
        "canvas_assignments_seen": len(events),
        "mismatches": mismatches,
    }, indent=1, ensure_ascii=False) + "\n")
    print(f"reconcile: {len(mismatches)} mismatch(es) across {len(events)} Canvas assignment(s)")
    if mismatches and not send_push(reconcile_push_text(mismatches)):
        return  # push failed: no state key, so the next run retries
    state[key] = now.isoformat()


def run_daily_brief(now: datetime, state: dict) -> None:
    """Mini morning brief (~7:30am PT push) — Mac-independent. The Mac's email
    brief is richer (readings, course messages); this guarantees a baseline."""
    key = f"brief|{now.date().isoformat()}"
    if key in state or (now.hour, now.minute) < (7, 30):
        return
    wd, d = now.weekday(), now.date()
    if d > LAST_INSTRUCTION:
        classes = "no lectures (end of term)"
    elif d in HOLIDAYS:
        classes = "no class today (holiday)"
    else:
        # heartbeat-config.json "classes_by_weekday": {"0": "CS 101 10am, BIO 101 1pm", ...}, Monday = 0
        classes = CFG.get("classes_by_weekday", {}).get(str(wd), "no classes")
    upcoming = [f"{label} ({when.strftime('%a %-I:%M%p').lower()})"
                for when, label, soft in load_deadlines() if not soft
                if 0 < (when - now).total_seconds() / 3600 <= 72]
    due = (" | DUE soon: " + "; ".join(upcoming[:4])) if upcoming else " | nothing due in 72h"
    see = ""
    if send_push(f"☀️ {now.strftime('%A')}: {classes}{due}{see}"):
        state[key] = now.isoformat()


READINGS_PATH = DIR / "readings.json"

def run_readings_push(now: datetime, state: dict) -> None:
    # Daily ~7am: what to read BEFORE today's classes (full-semester map in
    # readings.json; only class days with readings appear there).
    if now.hour < 7:
        return
    key = f"readings|{now.date().isoformat()}"
    if key in state:
        return
    if not READINGS_PATH.exists():
        return
    todays = json.loads(READINGS_PATH.read_text()).get(now.date().isoformat())
    state[key] = now.isoformat()
    if not todays:
        print("no readings mapped today")
        return
    send_push("📚 READ before class: " + " | ".join(todays))


def run_todo_push(now: datetime, state: dict) -> None:
    # Daily ~7am: your open to-do list (unordered) with due times.
    # todo.json = [{"task": str, "due": "YYYY-MM-DDTHH:MM"|null, "done": bool}]
    # You add/complete items by telling Claude in any session.
    if now.hour < 7:
        return
    key = f"todo|{now.date().isoformat()}"
    if key in state or not TODO_PATH.exists():
        return
    items = [t for t in json.loads(TODO_PATH.read_text()) if not t.get("done")]
    state[key] = now.isoformat()
    if not items:
        print("todo list empty")
        return
    def fmt(t):
        if not t.get("due"):
            return t["task"]
        d = datetime.fromisoformat(t["due"])
        return f"{t['task']} (due {d.strftime('%a %-m/%-d %-I:%M%p').lower()})"
    send_push("📝 TO-DO: " + " | ".join(fmt(t) for t in items))


def prune(state: dict, now: datetime) -> None:
    horizon = now - timedelta(days=14)
    for key in list(state):
        stamp = state[key] if isinstance(state[key], str) else ""
        try:
            if datetime.fromisoformat(stamp) < horizon:
                del state[key]
        except ValueError:
            pass  # keep unparsable entries


def main() -> None:
    now = now_local()
    if now.date() > SEMESTER_END:
        print("semester over — nothing to do (disable the workflow)")
        return
    state = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}
    prune(state, now)
    run_daily_brief(now, state)
    run_nudges(now, state)
    run_readings_push(now, state)
    run_todo_push(now, state)
    run_canvas_scan(now, state)
    run_reconcile(now, state)  # after the scan, so rows it just added count as tracked
    STATE_PATH.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
