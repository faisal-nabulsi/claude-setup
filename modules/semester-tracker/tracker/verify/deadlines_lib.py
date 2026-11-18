"""Shared logic for the two deadlines.json copies (tracker + heartbeat).

Every entry carries a stable "id" that never changes, even when its label does:
  - Canvas entries:  id = canvas_id (e.g. "assignment_9116314")
  - everything else: id = "d-" + first 10 hex of sha1("<when>|<label>") at the
    moment the entry first got an id. Relabeling later keeps the old id.

Merge rules (merge_rows), applied by sync_deadlines.py, mark_done.py and the
deadlines guard hook:
  1. Union by id. An entry is never dropped because one copy lacks it.
  2. "done" wins. If either copy says done, the merged entry is done
     (a tombstone). Nothing ever flips back to not-done.
  3. Otherwise, if the copies disagree, the one with the newer "updated"
     stamp wins; with no stamps, the tracker copy wins (it is the one humans
     edit) and the conflict is reported.
"""
import hashlib, json
from pathlib import Path

TRACKER = Path(__file__).resolve().parent.parent
PATHS = {"tracker": TRACKER / "deadlines.json",
         "heartbeat": TRACKER.parent / "semester-heartbeat" / "deadlines.json"}


def legacy_id(row: dict) -> str:
    if row.get("canvas_id"):
        return str(row["canvas_id"])
    raw = f"{row['when']}|{row['label']}".encode()
    return "d-" + hashlib.sha1(raw).hexdigest()[:10]


def assign_ids(rows: list[dict], known: dict | None = None) -> list[dict]:
    """Give every id-less row an id. `known` maps (when, label) -> id from the
    other copy, so a row added to both copies by hand gets the same id."""
    known = known or {}
    out = []
    for r in rows:
        if not r.get("id"):
            r = {**r, "id": known.get((r["when"], r["label"])) or legacy_id(r)}
        out.append(r)
    return out


def _pick(a: dict, b: dict) -> tuple[dict, str | None]:
    """a = tracker row, b = heartbeat row (same id). Returns (row, conflict)."""
    if a == b:
        return a, None
    if a.get("done") or b.get("done"):
        win = a if a.get("done") else b
        other = b if win is a else a
        win = dict(win)
        win["done"] = True
        if not win.get("done_at") and other.get("done_at"):
            win["done_at"] = other["done_at"]
        return win, None
    ua, ub = a.get("updated", ""), b.get("updated", "")
    if ua != ub and (ua or ub):
        return (a if ua > ub else b), None
    return a, f"{a['id']}: copies disagree, kept tracker's ({a['label'][:60]!r} vs {b['label'][:60]!r})"


def merge_rows(tracker: list[dict], heartbeat: list[dict]) -> tuple[list[dict], list[str]]:
    """Union-merge two copies. Returns (merged rows sorted by when, conflicts)."""
    known = {(r["when"], r["label"]): r["id"] for r in tracker + heartbeat if r.get("id")}
    t = assign_ids(tracker, known)
    h = assign_ids(heartbeat, known)
    conflicts = []
    merged: dict[str, dict] = {}
    for side in (t, h):  # tracker first, so _pick sees (tracker, heartbeat); also dedupes within a copy
        for r in side:
            if r["id"] in merged:
                merged[r["id"]], c = _pick(merged[r["id"]], r)
                if c:
                    conflicts.append(c)
            else:
                merged[r["id"]] = r
    rows = sorted(merged.values(), key=lambda r: (r["when"], r["id"]))
    return rows, conflicts


def violations(before: list[dict], after: list[dict]) -> list[str]:
    """What the guard refuses: an entry that disappeared, or done -> not done."""
    b = {r.get("id") or legacy_id(r): r for r in before}
    a = {r.get("id") or legacy_id(r): r for r in after}
    out = [f"removed: {r['when']} {r['label'][:70]}" for k, r in b.items() if k not in a]
    out += [f"un-done: {r['when']} {r['label'][:70]}" for k, r in b.items()
            if k in a and r.get("done") and not a[k].get("done")]
    return out


def pull_heartbeat() -> None:
    """The tracker connector (claude.ai / phone) commits done-marks to the
    heartbeat repo on GitHub, so fetch them before merging. Quiet and
    best-effort: offline or a dirty tree just means we merge what's local."""
    import subprocess
    repo = PATHS["heartbeat"].parent
    try:
        subprocess.run(["git", "-C", str(repo), "pull", "-q", "--rebase", "--autostash"],
                       capture_output=True, timeout=20)
    except Exception:
        pass


def load(path: Path) -> list[dict]:
    return json.loads(path.read_text())


def dump(path: Path, rows: list[dict]) -> None:
    path.write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n")
