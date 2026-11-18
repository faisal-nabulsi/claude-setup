#!/usr/bin/env python3
"""Check that the files the tracker relies on actually exist. Prints FILES-OK only if all pass.

Why: scripts in this folder use fixed relative paths. After you move or rename something,
run this before trusting any automation again.

Optional minimum file counts go in ../config.json:
  "file_checks": {"cs101/readings": 5, "bio101/slides": 3}
Counts are MINIMUMS, because course folders only grow during a term.
"""
import json, sys
from pathlib import Path

DIR = Path(__file__).resolve().parent.parent
fails = []
cfg = json.loads((DIR / "config.json").read_text()) if (DIR / "config.json").exists() else {}

for subdir, minimum in cfg.get("file_checks", {}).items():
    p = DIR / subdir
    n = len([f for f in p.iterdir() if f.is_file() and not f.name.startswith(".")]) if p.exists() else -1
    if n < minimum:
        fails.append(f"{subdir}: expected >= {minimum} files, found {n}")

for core in ["schedule.md", "deadlines.json", "config.json", "send-push.sh",
             "verify/deadlines_lib.py", "verify/sync_deadlines.py", "verify/mark_done.py"]:
    if not (DIR / core).exists():
        fails.append(f"missing core file: {core}")

if fails:
    print("FAILURES:")
    for x in fails:
        print(" -", x)
    sys.exit(1)
print("FILES-OK")
