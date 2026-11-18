#!/usr/bin/env python3
"""Union-merge the two deadlines.json copies. NEVER `cp` one over the other.

On 2026-09-28 a session copied the heartbeat repo's deadlines.json over the
tracker's after a rebase, which silently deleted four entries. On 2026-10-08 a
relabel ("DONE ... DO NOT RE-ADD") nearly brought a finished task back, because
the old merge matched rows on (when, label). Rows now carry a stable "id", and
"done" always wins (see deadlines_lib.py for the rules).

Usage:  python3 verify/sync_deadlines.py [--dry-run]
Prints every row a copy gains, every done flag that changes, and any conflict.
"""
import sys
from deadlines_lib import pull_heartbeat, PATHS, load, dump, merge_rows


def main() -> int:
    dry = "--dry-run" in sys.argv
    pull_heartbeat()
    sides = {name: load(p) for name, p in PATHS.items()}
    merged, conflicts = merge_rows(sides["tracker"], sides["heartbeat"])
    by_id = {r["id"]: r for r in merged}
    changed = False
    for name, rows in sides.items():
        have = {r.get("id") for r in rows}
        before_done = {r.get("id"): bool(r.get("done")) for r in rows}
        gained = [r for r in merged if r["id"] not in have and
                  (r["when"], r["label"]) not in {(x["when"], x["label"]) for x in rows}]
        flipped = [by_id[i] for i, d in before_done.items() if i in by_id and not d and by_id[i].get("done")]
        if gained:
            print(f"{name}: +{len(gained)} row(s) it was missing")
            for r in gained:
                print(f"    + {r['when']}  {r['label'][:90]}")
        for r in flipped:
            print(f"{name}: now DONE  {r['when']}  {r['label'][:80]}")
        if rows != merged:
            changed = True
            if not dry:
                dump(PATHS[name], merged)
    for c in conflicts:
        print("CONFLICT", c)
    print(f"{'would be' if dry else 'now'} {len(merged)} entries in both copies"
          + ("" if changed else "  (already in sync)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
