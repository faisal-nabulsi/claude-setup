#!/usr/bin/env python3
"""Hourly deadline nudger (LOCAL BACKUP; normally the semester-heartbeat GitHub
Actions repo sends these, so they arrive even when the Mac is asleep).

Pushes ~3h and ~1h before each open deadline in deadlines.json, through
send-push.sh (ntfy). Windows are half-open and disjoint so an hourly cadence
never double-sends: (2.5h, 3.5h] -> "3 hours" nudge, (0.5h, 1.5h] -> "1 hour".
"soft" rows (self-imposed targets) get one calm reminder instead.
"""
import json, os, subprocess, sys
from datetime import datetime, date, timedelta
from pathlib import Path

DIR = Path(__file__).resolve().parent
# Test hooks: NUDGE_TEST_NOW=YYYY-MM-DDTHH:MM freezes the clock; NUDGE_DRY_RUN=1 prints instead of sending.
def deadlines():
    # "soft" entries are self-imposed targets (the M/W/F journal cadence), not
    # real due dates: one calm reminder, never the urgent pair.
    # Done rows are permanent tombstones; never nudge them.
    return [(datetime.fromisoformat(d["when"]), d["label"], bool(d.get("soft")))
            for d in json.loads((DIR / "deadlines.json").read_text()) if not d.get("done")]

def main():
    test_now = os.environ.get("NUDGE_TEST_NOW")
    now = datetime.fromisoformat(test_now) if test_now else datetime.now()
    three, one, soft_due = [], [], []
    for when, label, soft in deadlines():
        delta = (when - now).total_seconds() / 3600
        if soft:
            if 0.5 < delta <= 1.5:
                soft_due.append((when, label))
            continue
        if 2.5 < delta <= 3.5:
            three.append((when, label))
        elif 0.5 < delta <= 1.5:
            one.append((when, label))
    # Real deadlines first: the 320-char cap must never truncate an actual
    # due-date alert off the end in favour of a soft, self-imposed one.
    parts = [f"⏰ 3 HRS LEFT: {l} — due {w.strftime('%-I:%M%p').lower()}" for w, l in three]
    parts += [f"🚨 1 HR LEFT: {l} — due {w.strftime('%-I:%M%p').lower()}. Submit now!" for w, l in one]
    parts += [f"\U0001F4D3 {l}" for _, l in soft_due]
    if not parts:
        print("no deadlines in nudge windows")
        return
    msg = " | ".join(parts)[:320]  # stay within 2 SMS segments
    if os.environ.get("NUDGE_DRY_RUN") == "1":
        print("WOULD SEND:", msg)
        return
    r = subprocess.run(["bash", str(DIR / "send-push.sh"), msg], capture_output=True, text=True)
    print(r.stdout.strip() or "push script ran (no output)")

if __name__ == "__main__":
    sys.exit(main())
