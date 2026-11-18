#!/usr/bin/env python3
"""Gate G10: every '<Weekday> <Mon> <D>' string in the tracker's markdown files
names the correct weekday for that date (catches slips like "Monday Oct 28" when Oct 28 is a Wednesday).
Prints WEEKDAY-OK only if zero mismatches."""
import re, sys
from datetime import date
from pathlib import Path

DIR = Path(__file__).resolve().parent.parent
import json
_CFG = json.loads((DIR / "config.json").read_text()) if (DIR / "config.json").exists() else {}
YEAR = int(_CFG.get("term_start", str(date.today().year))[:4])
MONTHS = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}
DAYS = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}
pat = re.compile(r"\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*[,]? (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* (\d{1,2})\b")

checked, fails = 0, []
for f in ["schedule.md"] + _CFG.get("weekday_check_files", []):
    p = DIR / f
    if not p.exists():
        fails.append(f"missing file: {f}"); continue
    for m in pat.finditer(p.read_text()):
        wd, mon, day = m.group(1), m.group(2), int(m.group(3))
        try:
            actual = date(YEAR, MONTHS[mon], day).weekday()
        except ValueError:
            fails.append(f"{f}: impossible date '{m.group(0)}'"); continue
        checked += 1
        if actual != DAYS[wd]:
            real = [k for k, v in DAYS.items() if v == actual][0]
            fails.append(f"{f}: '{m.group(0)}' — {mon} {day} {YEAR} is actually {real}")

if fails:
    print("FAILURES:"); [print(" -", x) for x in fails]; sys.exit(1)
print(f"verified {checked} weekday-date pairs across tracker files")
print("WEEKDAY-OK")
