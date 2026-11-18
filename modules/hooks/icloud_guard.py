#!/usr/bin/env python3
"""SessionStart hook: warn when an active git repo lives in iCloud-synced
Desktop/Documents. iCloud evicts files (git hangs) and makes "main 2" conflict
copies inside .git (pushes break). This happened to semester-tracker on 2026-10-08.

- Every startup: warns if the session's own repo is in iCloud and not *.nosync.
- Once a day: lists active repos (.git/index touched in the last 14 days) under
  ~/Desktop and ~/Documents that are not *.nosync. Only stats files, never reads
  them, so dataless iCloud files are not downloaded.
Fix: tag the folder so iCloud ignores it (keeps its name, no symlink; tested 2026-10-09):
  xattr -w 'com.apple.fileprovider.ignore#P' 1 <folder>
Older protection still counts: a <name>.nosync folder, or moving the repo to ~/code.
"""
import ctypes, ctypes.util, json, os, subprocess, sys, time
from pathlib import Path

_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
IGNORE_TAG = b"com.apple.fileprovider.ignore#P"


def tagged(p: str) -> bool:
    """True if the folder carries iCloud's ignore tag (getxattr via libc: no subprocess per folder)."""
    return _libc.getxattr(os.fsencode(p), IGNORE_TAG, None, 0, 0, 0) >= 0

HOME = Path.home()
ICLOUD_DESKTOP = HOME / "Library/Mobile Documents/com~apple~CloudDocs/Desktop"
ROOTS = [HOME / "Desktop", HOME / "Documents"]
STATE = HOME / ".claude/state/icloud-guard.json"
SKIP = {"node_modules", ".build", "build", "dist", ".venv", "venv", "Pods", "DerivedData"}


def in_icloud(p: str) -> bool:
    """In synced Desktop/Documents and not protected by .nosync or an ignore tag on it or a parent."""
    real = os.path.realpath(p)
    root = next((str(r) for r in ROOTS if real.startswith(str(r) + "/")), None)
    if root is None or ".nosync" in real:
        return False
    cur = real
    while len(cur) > len(root):
        if tagged(cur):
            return False
        cur = os.path.dirname(cur)
    return True


def active_repos(max_depth: int = 4, budget_s: float = 4.0) -> list[str]:
    start, found = time.time(), []
    for root in ROOTS:
        for dirpath, dirs, _ in os.walk(root):
            if time.time() - start > budget_s:
                return found
            depth = len(Path(dirpath).relative_to(root).parts)
            if ".git" in dirs:
                idx = Path(dirpath, ".git", "index")
                try:
                    if time.time() - idx.stat().st_mtime < 14 * 86400:
                        found.append(dirpath)
                except OSError:
                    pass
            dirs[:] = [d for d in dirs if depth < max_depth and not d.startswith(".")
                       and not d.endswith(".nosync") and d not in SKIP
                       and not tagged(os.path.join(dirpath, d))]
    return found


def main() -> None:
    if not ICLOUD_DESKTOP.exists():
        return
    raw = sys.stdin.read()
    cwd = (json.loads(raw) if raw.strip() else {}).get("cwd") or os.getcwd()
    msgs = []
    try:
        top = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"], capture_output=True,
                             text=True, timeout=2).stdout.strip()
    except Exception:
        top = ""
    if top and in_icloud(top):
        msgs.append(f"WARNING: this session's git repo {top} is inside iCloud-synced Desktop/Documents. "
                    "iCloud can evict its files and corrupt .git. Suggest tagging the folder: xattr -w 'com.apple.fileprovider.ignore#P' 1 <folder> "
                    "before heavy git work.")
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    today = time.strftime("%Y-%m-%d")
    if state.get("scanned") != today:
        repos = [r for r in active_repos() if r != top]
        state["scanned"] = today
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state))
        if repos:
            msgs.append("Daily iCloud check: active git repos in iCloud-synced folders (risk of eviction/corruption): "
                        + ", ".join(r.replace(str(HOME), "~") for r in repos[:8])
                        + ". Mention this to the user once; fix = tag the folder (xattr -w 'com.apple.fileprovider.ignore#P' 1 <folder>) or move it to ~/code.")
    if msgs:
        print("\n".join(msgs))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"(icloud guard skipped: {e!r})", file=sys.stderr)
