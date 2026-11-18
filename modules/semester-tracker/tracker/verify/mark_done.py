#!/usr/bin/env python3
"""Mark one tracker entry done in BOTH deadlines.json copies.

Usage:
  python3 verify/mark_done.py "<search text or id>" [--note "uploaded on Canvas"] [--dry-run]

Finds open (not done) entries whose id equals the query or whose label
contains it (case-insensitive). Exactly one match is required; otherwise the
matches are listed and nothing changes. The entry gets done: true, a done_at
timestamp, and a "DONE <m/d> ... DO NOT RE-ADD" label, then both copies are
union-merged. Afterwards, commit and push both repos.
"""
import argparse, sys
from datetime import datetime
from deadlines_lib import pull_heartbeat, PATHS, load, dump, merge_rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--note", default="")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    pull_heartbeat()

    merged, _ = merge_rows(load(PATHS["tracker"]), load(PATHS["heartbeat"]))
    q = a.query.lower()
    hits = [r for r in merged if not r.get("done") and (r["id"] == a.query or q in r["label"].lower())]
    if len(hits) != 1:
        print(f"{len(hits)} open matches for {a.query!r}; need exactly 1. Use the id to pick one:")
        for r in hits[:20]:
            print(f"  {r['id']:<22} {r['when']}  {r['label'][:90]}")
        return 2
    r = hits[0]
    now = datetime.now()
    note = f" ({a.note})" if a.note else ""
    label = r["label"]
    if not label.startswith("DONE"):
        label = f"DONE {now.month}/{now.day}{note} — {label}"
    if "DO NOT RE-ADD" not in label:
        label = label.rstrip() + " DO NOT RE-ADD."
    done = {**r, "label": label, "done": True, "done_at": now.strftime("%Y-%m-%dT%H:%M")}
    rows = [done if x["id"] == r["id"] else x for x in merged]
    print(f"done: {r['id']}  {r['when']}  {r['label'][:90]}")
    if a.dry_run:
        return 0
    for p in PATHS.values():
        dump(p, rows)
    print("both copies updated; now commit + push semester-tracker and semester-heartbeat")
    return 0


if __name__ == "__main__":
    sys.exit(main())
