#!/usr/bin/env python3
"""Assignment watcher: pushes your phone when a Canvas assignment whose name matches a
pattern becomes OPEN (unlock time passed, not yet locked).

Typical use: an in-class participation or attendance check-in that your instructor posts
without warning and closes within hours. You get one push per assignment, with the close time.

It reads Canvas through the canvas connector (modules/mcp/bridge/mcp_bridge.py, read-only)
and pushes through ../send-push.sh (ntfy). It never submits anything.

Config: ../config.json, key "assignment_watch":
  "assignment_watch": {
    "timezone": "America/New_York",
    "watches": [
      {"course_id": "1234567", "name_regex": "\\bcheck-?in\\b", "label": "BIO 101"}
    ]
  }

launchd (assignment-watch.plist) runs it at the times you choose; with --loop-until HH:MM it
re-checks every 90 s until then (useful for a lecture window).

Usage: assignment_watch.py [--loop-until HH:MM] [--init] [--dry-run]
  --init     mark every matching assignment that exists now as already seen (no pushes)
"""
import argparse, json, re, subprocess, sys, time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
TRACKER = HERE.parent
_cfg_path = TRACKER / "config.json"
CFG = (json.loads(_cfg_path.read_text()) if _cfg_path.exists() else {}).get("assignment_watch", {})
TZ = ZoneInfo(CFG.get("timezone", "America/Los_Angeles"))
WATCHES = CFG.get("watches", [])
SEND = TRACKER / "send-push.sh"
BRIDGE = Path.home() / "code/scripts/mcp_bridge.py"   # the installer puts the bridge here
STATE = Path.home() / ".local/state/assignment-watch/seen.json"


def log(m):
    print(f"{datetime.now(TZ):%Y-%m-%d %H:%M:%S} {m}", flush=True)


def fetch(course_id):
    out = subprocess.run(["python3", str(BRIDGE), "canvas", "call", "list_assignments",
                          json.dumps({"course_identifier": course_id, "raw_dates": True}), "120"],
                         capture_output=True, text=True, timeout=180).stdout
    names = {m.group(1): m.group(2).strip() for m in re.finditer(
        r"ID: (\d+)\s*\nName: <<<[^:]*:\s*(.*?)>>>", out)}
    raw = out.split("Canvas returned null):", 1)
    dates = {}
    if len(raw) == 2:
        try:
            for a in json.loads(raw[1].strip())["assignments"]:
                dates[str(a["assignment_id"])] = a
        except (ValueError, KeyError):
            pass
    return names, dates


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(TZ) if s else None


def check(seen, dry, init=False):
    for w in WATCHES:
        names, dates = fetch(str(w["course_id"]))
        if not names:
            log(f"{w.get('label', w['course_id'])}: no assignments returned (connector problem?)")
            continue
        pat = re.compile(w["name_regex"], re.I)
        now = datetime.now(TZ)
        for aid, name in names.items():
            key = f"{w['course_id']}:{aid}"
            if key in seen or not pat.search(name):
                continue
            d = dates.get(aid, {})
            unlock, lock, due = ts(d.get("unlock_at")), ts(d.get("lock_at")), ts(d.get("due_at"))
            if init:
                seen.add(key)
                continue
            if (unlock and unlock > now) or (lock and lock <= now):
                continue
            close = lock or due
            when = f"{close:%-I:%M%p}".lower() if close else "?"
            msg = f"📝 {w.get('label', 'Canvas')}: {name} is OPEN, closes {when}"
            log(("DRY " if dry else "") + msg)
            if not dry:
                subprocess.run(["bash", str(SEND), msg], timeout=60)
            seen.add(key)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop-until")
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if not WATCHES:
        sys.exit("no \"assignment_watch\" watches in config.json; see the docstring")
    STATE.parent.mkdir(parents=True, exist_ok=True)
    seen = set(json.loads(STATE.read_text())) if STATE.exists() else set()
    end = None
    if a.loop_until:
        h, m = map(int, a.loop_until.split(":"))
        end = datetime.now(TZ).replace(hour=h, minute=m, second=0)
    while True:
        try:
            check(seen, a.dry_run, a.init)
        except Exception as e:  # keep looping through a network blip
            log(f"error: {e}")
        STATE.write_text(json.dumps(sorted(seen)))
        if not end or datetime.now(TZ) >= end or a.init:
            break
        time.sleep(90)


if __name__ == "__main__":
    sys.exit(main())
