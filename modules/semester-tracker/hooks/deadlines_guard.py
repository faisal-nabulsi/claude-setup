#!/usr/bin/env python3
"""Guard for the two deadlines.json copies (semester-tracker tracker + heartbeat).

  pre  (PreToolUse: Bash|Write|Edit|MultiEdit): refuses cp/mv/rsync/redirects/
       git checkout|restore onto deadlines.json, and any Write/Edit that would
       drop a row or flip done -> not done.
  post (PostToolUse: same tools): if a script changed either copy anyway, compares
       it with the last good snapshot. Rows that disappeared come back and done
       flags are restored (done-wins merge); Claude is told what happened.

Escape hatch for a deliberate deletion: touch ~/.claude/state/deadlines-guard/ALLOW_SHRINK
(the next post check accepts the smaller file once, then deletes the flag).
"""
import json, os, re, shlex, sys
from pathlib import Path

TRACKER_VERIFY = Path(os.environ.get("SEMESTER_TRACKER", Path.home() / "semester-tracker")) / "verify"
STATE = Path.home() / ".claude/state/deadlines-guard"
SYNC_HINT = ("Use `python3 verify/sync_deadlines.py` (union merge) or `python3 verify/mark_done.py \"<text>\"` "
             "in ~/semester-tracker instead. Never copy one deadlines.json over the other.")

sys.path.insert(0, str(TRACKER_VERIFY))
try:
    from deadlines_lib import PATHS, merge_rows, violations  # noqa: E402
    PROTECTED = {os.path.realpath(p) for p in PATHS.values()}
except Exception as _e:  # tracker moved or broken: main() fails closed for deadlines.json commands
    PATHS, PROTECTED, IMPORT_ERROR = None, set(), _e
else:
    IMPORT_ERROR = None


def is_protected(path: str, cwd: str) -> bool:
    p = os.path.realpath(os.path.join(cwd, os.path.expanduser(path)))
    return p in PROTECTED


def deny(reason: str) -> None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                             "permissionDecision": "deny",
                                             "permissionDecisionReason": reason}}))
    sys.exit(0)


def bash_targets_protected(cmd: str, cwd: str) -> str | None:
    for m in re.finditer(r"(?<![0-9&])>{1,2}\s*[\"']?([^\s;&|\"']*deadlines\.json)", cmd):
        if is_protected(m.group(1), cwd):
            return "redirects output onto deadlines.json"
    for seg in re.split(r"&&|\|\||;|\||\n", cmd):
        try:
            toks = shlex.split(seg)
        except ValueError:
            toks = seg.split()
        if not toks:
            continue
        prog = os.path.basename(toks[0])
        args = [t for t in toks[1:] if not t.startswith("-")]
        if prog in ("cp", "mv", "rsync", "ditto", "install", "ln") and len(args) >= 2:
            target = os.path.join(cwd, os.path.expanduser(args[-1]))
            if os.path.isdir(target):
                target = os.path.join(target, os.path.basename(args[-2]))
            if is_protected(target, cwd):
                return f"`{prog}` would overwrite deadlines.json"
        if prog == "tee" and any(is_protected(a, cwd) for a in args):
            return "`tee` would overwrite deadlines.json"
        if prog == "git" and {"checkout", "restore"} & set(toks) and any(
                t.endswith("deadlines.json") and is_protected(t, cwd) for t in toks):
            return "restoring an old deadlines.json can drop rows and done flags"
    return None


def simulate_edit(text: str, ti: dict, tool: str) -> str:
    edits = ti.get("edits") if tool == "MultiEdit" else [ti]
    for e in edits:
        old, new = e.get("old_string", ""), e.get("new_string", "")
        text = text.replace(old, new) if e.get("replace_all") else text.replace(old, new, 1)
    return text


def pre(data: dict) -> None:
    tool, ti, cwd = data.get("tool_name"), data.get("tool_input") or {}, data.get("cwd") or os.getcwd()
    if tool == "Bash":
        cmd = ti.get("command", "")
        if "deadlines.json" in cmd:
            why = bash_targets_protected(cmd, cwd)
            if why:
                deny(f"Blocked: {why}. {SYNC_HINT}")
        return
    path = ti.get("file_path", "")
    if not path or not is_protected(path, cwd):
        return
    current_text = Path(os.path.realpath(path)).read_text()
    new_text = ti.get("content", "") if tool == "Write" else simulate_edit(current_text, ti, tool)
    try:
        new_rows = json.loads(new_text)
    except ValueError as e:
        deny(f"Blocked: this edit would leave deadlines.json as invalid JSON ({e}).")
    v = violations(json.loads(current_text), new_rows)
    if v:
        deny("Blocked: this change would break the tracker (" + "; ".join(v[:5]) + f"). {SYNC_HINT}")


def post(_: dict) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    meta_path = STATE / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    notes = []
    for name, path in PATHS.items():
        if not path.exists():
            continue
        st = path.stat()
        sig = [st.st_mtime_ns, st.st_size]
        snap = STATE / f"{name}.json"
        if meta.get(name) == sig and snap.exists():
            continue  # unchanged since the last good snapshot: fast path
        text = path.read_text()
        if snap.exists():
            good = json.loads(snap.read_text())
            try:
                rows = json.loads(text)
            except ValueError:
                path.write_text(snap.read_text())
                notes.append(f"{name} deadlines.json became invalid JSON; restored the last good copy")
                rows = good
            allow = STATE / "ALLOW_SHRINK"
            v = violations(good, rows)
            if v and allow.exists():
                allow.unlink()
                notes.append(f"{name} deadlines.json: accepted a deliberate change ({len(v)} row(s)) via ALLOW_SHRINK")
            elif v:
                fixed, _ = merge_rows(rows, good)  # current edits win, missing rows + done flags come back
                path.write_text(json.dumps(fixed, indent=2, ensure_ascii=False) + "\n")
                notes.append(f"{name} deadlines.json: undid {len(v)} problem(s): " + "; ".join(v[:4]))
        snap.write_text(path.read_text())
        st = path.stat()
        meta[name] = [st.st_mtime_ns, st.st_size]
    meta_path.write_text(json.dumps(meta))
    if notes:
        msg = "deadlines guard: " + " | ".join(notes) + ". " + SYNC_HINT
        print(json.dumps({"decision": "block", "reason": msg,
                          "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": msg}}))


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    raw = sys.stdin.read()
    data = json.loads(raw) if raw.strip() else {}
    try:
        if IMPORT_ERROR:
            raise RuntimeError(f"cannot load deadlines_lib: {IMPORT_ERROR!r}")
        pre(data) if mode == "pre" else post(data)
    except SystemExit:
        raise
    except Exception as e:  # fail closed only when deadlines.json is involved
        if mode == "pre" and "deadlines.json" in raw:
            deny(f"Blocked: the deadlines guard hit an error ({e!r}), so it could not check this change. {SYNC_HINT}")
        print(f"deadlines guard error: {e!r}", file=sys.stderr)


if __name__ == "__main__":
    main()
