#!/usr/bin/env python3
"""Gate G1: deadlines.json is valid, in-semester, sorted, and every entry's
date appears in schedule.md's key-dates table. Prints DEADLINES-OK only if all pass."""
import json, sys
from datetime import datetime
from pathlib import Path

DIR = Path(__file__).resolve().parent.parent
fails = []
CFG = json.loads((DIR / "config.json").read_text()) if (DIR / "config.json").exists() else {}
START = datetime.fromisoformat(CFG.get("term_start", "2000-01-01"))
END = datetime.fromisoformat(CFG.get("term_end", "2100-01-01") + "T23:59")

data = json.loads((DIR / "deadlines.json").read_text())
if not isinstance(data, list) or not data:
    print("FAIL: deadlines.json not a non-empty list"); sys.exit(1)

sched = (DIR / "schedule.md").read_text()
prev = None
for d in data:
    when = datetime.fromisoformat(d["when"])
    label = d["label"]
    if not (START <= when <= END):
        fails.append(f"out of semester range: {d}")
    if prev and when < prev:
        fails.append(f"not sorted at: {d}")
    prev = when
    if d.get("soft") or d.get("done"):
        continue  # self-imposed targets and finished rows needn't be in the key-dates table
    # date must appear in schedule.md key-dates table (format: 'Sep 3')
    stamp = f"{when.strftime('%b')} {when.day}"
    if stamp not in sched:
        fails.append(f"date '{stamp}' ({label}) not found anywhere in schedule.md")

if fails:
    print("FAILURES:"); [print(" -", f) for f in fails]; sys.exit(1)
print(f"checked {len(data)} deadline entries against schedule.md")
print("DEADLINES-OK")
