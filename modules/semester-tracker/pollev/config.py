"""Settings for the PollEv notifier. Everything personal lives in ../config.json under "pollev":

  "pollev": {
    "timezone": "America/New_York",
    "term_start": "2026-08-26", "term_end": "2026-12-04",
    "no_class": ["2026-09-07", "2026-11-26"],
    "courses": [
      {"course": "BIO 101", "hosts": ["presenter-handle"], "days": [0, 2, 4],
       "start": "12:00", "end": "13:00"}
    ]
  }

A "host" is the presenter's PollEv name: the part after pollev.com/ on the lecture slides.
days: Monday=0 ... Friday=4. Times are local, 24-hour.
"""
import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TRACKER = Path(__file__).resolve().parent.parent
_cfg_path = TRACKER / "config.json"
_P = (json.loads(_cfg_path.read_text()) if _cfg_path.exists() else {}).get("pollev", {})

TZ = ZoneInfo(_P.get("timezone", "America/Los_Angeles"))
STATE = Path.home() / ".local/state/pollev"
SEND = TRACKER / "send-push.sh"


def _t(s):
    h, m = s.split(":")
    return time(int(h), int(m))


COURSES = [{"course": c["course"], "hosts": [h.lower() for h in c["hosts"]], "days": set(c["days"]),
            "start": _t(c["start"]), "end": _t(c["end"])} for c in _P.get("courses", [])]
TERM_START = date.fromisoformat(_P.get("term_start", "2026-08-26"))
TERM_END = date.fromisoformat(_P.get("term_end", "2026-12-04"))
NO_CLASS = {date.fromisoformat(d) for d in _P.get("no_class", [])}


def course_for_host(host):
    host = (host or "").lower()
    for c in COURSES:
        if host in c["hosts"]:
            return c["course"]
    return None


def course_at(moment):
    """The course whose lecture window (with 10 minutes' slack each side) contains `moment`."""
    m = moment.astimezone(TZ)
    if m.date() in NO_CLASS or not (TERM_START <= m.date() <= TERM_END):
        return None
    for c in COURSES:
        if m.weekday() not in c["days"]:
            continue
        start = datetime.combine(m.date(), c["start"], TZ) - timedelta(minutes=10)
        end = datetime.combine(m.date(), c["end"], TZ) + timedelta(minutes=10)
        if start <= m <= end:
            return c
    return None
