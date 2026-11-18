#!/usr/bin/env python3
"""autohub: the user's own "Zapier" on their Mac.

launchd ({{LABEL_PREFIX}}.autohub) runs `autohub.py tick` every 5 minutes. Each tick runs the
recipes in recipes/*.json that are due, compares what each source returns with what it has
already seen, and pushes anything new to the user's phone (ntfy). Standard library only.

See README.md for how to add a recipe, read the logs, pause everything and uninstall.
"""
import contextlib
import copy
import fcntl
import json
import os
import re
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import sources  # noqa: E402

STATE_DIR = Path(os.environ.get("AUTOHUB_STATE_DIR", "~/.local/state/autohub")).expanduser()
RECIPES_DIR = Path(os.environ.get("AUTOHUB_RECIPES_DIR", str(HERE / "recipes")))
SEEN_CAP = 3000
ID_RE = re.compile(r"^[a-z0-9-]+$")
WEEKLY_RE = re.compile(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun) ([01]\d|2[0-3]):([0-5]\d)$")
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# ---------- recipes ----------

def validate_recipe(r: dict) -> list:
    errs = []
    if not isinstance(r, dict):
        return ["recipe must be a JSON object"]
    if not isinstance(r.get("id"), str) or not ID_RE.match(r.get("id", "")):
        errs.append("id must be lowercase letters, digits and dashes")
    if not isinstance(r.get("note"), str) or not r.get("note"):
        errs.append("note is required (one plain sentence)")
    if not isinstance(r.get("enabled"), bool):
        errs.append("enabled must be true or false")
    if r.get("mode") not in ("dry_run", "live"):
        errs.append('mode must be "dry_run" or "live"')
    has_every, has_weekly = "every_minutes" in r, "weekly_at" in r
    if has_every == has_weekly:
        errs.append('give exactly one schedule: "every_minutes" or "weekly_at"')
    elif has_every and (not isinstance(r["every_minutes"], int) or r["every_minutes"] < 5):
        errs.append("every_minutes must be a whole number, 5 or more")
    elif has_weekly and not (isinstance(r["weekly_at"], str) and WEEKLY_RE.match(r["weekly_at"])):
        errs.append('weekly_at must look like "Sun 18:00"')
    src = r.get("source")
    if not isinstance(src, dict) or src.get("kind") not in sources.REGISTRY:
        errs.append(f"source.kind must be one of {sorted(k for k in sources.REGISTRY if k != 'fake')}")
    if isinstance(src, dict) and src.get("kind") == "mcp_call":
        from sources import bridge
        if not bridge.is_allowed(src.get("server"), src.get("tool"), src.get("args") or {}):
            errs.append(f"{src.get('server')}.{src.get('tool')} (with these args) is not on the allowed list "
                        "(allowed_sources.json); adding a tool there needs the user's OK")
        for k in ("list_path", "id_field"):
            if k not in src:
                errs.append(f"mcp_call source needs {k}")
    act = r.get("action")
    if not isinstance(act, dict) or act.get("kind") != "push":
        errs.append('action.kind must be "push" (the only action autohub allows)')
    elif not isinstance(act.get("title"), str) or not act.get("title"):
        errs.append("action.title is required")
    return errs


def load_recipes(dir: Path = None):
    dir = Path(dir or RECIPES_DIR)
    good, errors = [], []
    for f in sorted(dir.glob("*.json")):
        try:
            r = json.loads(f.read_text())
        except Exception as e:
            errors.append(f"{f.name}: not valid JSON ({e})")
            continue
        errs = validate_recipe(r)
        if errs:
            errors.append(f"{f.name}: " + "; ".join(errs))
        else:
            r["_file"] = str(f)
            good.append(r)
    return good, errors


# ---------- state ----------

def new_recipe_state() -> dict:
    return {"seen": [], "groups": [], "last_run": None, "fails": 0, "alerted": False, "seeded": False}


def seeded(rs: dict) -> bool:
    return bool(rs.get("seeded"))


class State:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.data = {"recipes": {}}

    def load(self):
        if not self.path.exists():
            self.data = {"recipes": {}}
            return self
        try:
            d = json.loads(self.path.read_text())
            if not isinstance(d, dict) or not isinstance(d.get("recipes"), dict):
                raise ValueError("wrong shape")
            self.data = d
        except Exception:
            # Never treat a damaged file as "nothing seen" with pushes on: keep the damaged copy,
            # start empty, and every recipe re-seeds silently (seeded=False => no pushes).
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            self.path.rename(self.path.with_name(f"{self.path.name}.corrupt-{stamp}"))
            self.data = {"recipes": {}}
        return self

    def recipe(self, rid: str) -> dict:
        rs = self.data["recipes"].setdefault(rid, new_recipe_state())
        for k, v in new_recipe_state().items():
            rs.setdefault(k, v)
        return rs

    def save(self):
        atomic_write(self.path, json.dumps(self.data, indent=1))


def atomic_write(path: Path, text: str):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    with open(tmp, "w") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _cap(lst: list) -> list:
    return lst[-SEEN_CAP:] if len(lst) > SEEN_CAP else lst


def diff(rs: dict, items: list) -> list:
    """Return the items not seen before (texts prefixed "Updated: " when their group was seen),
    and record every item as seen. On the first run (not seeded yet) return nothing."""
    seen, groups = set(rs["seen"]), set(rs["groups"])
    first = not seeded(rs)
    new = []
    for it in items:
        k = it["key"]
        if k in seen:
            continue
        g = it.get("group")
        if not first:
            out = dict(it)
            if g and g in groups and out.get("text"):
                out["text"] = "Updated: " + out["text"]
            new.append(out)
        seen.add(k)
        rs["seen"].append(k)
        if g and g not in groups:
            groups.add(g)
            rs["groups"].append(g)
    rs["seen"] = _cap(rs["seen"])
    rs["groups"] = _cap(rs["groups"])
    rs["seeded"] = True
    return new


# ---------- scheduling ----------

QUIET_START, QUIET_END = 1, 7   # 01:00 to 07:00 local: hold pushes
SOURCE_TIMEOUT = 60
MAX_LINES = 5


def _parse(ts):
    return datetime.fromisoformat(ts) if ts else None


def due(r: dict, rs: dict, now: datetime) -> bool:
    last = _parse(rs.get("last_run"))
    if last is None:
        return True
    if "every_minutes" in r:
        return (now - last).total_seconds() >= r["every_minutes"] * 60
    m = WEEKLY_RE.match(r["weekly_at"])
    day, hh, mm = DAYS.index(m.group(1)), int(m.group(2)), int(m.group(3))
    slot = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    slot = slot - timedelta(days=(now.weekday() - day) % 7)
    if slot > now:
        slot -= timedelta(days=7)
    return last < slot


def in_quiet(now: datetime) -> bool:
    return QUIET_START <= now.hour < QUIET_END


def bundle(items: list) -> str:
    lines = [i["text"] for i in items if i.get("text")]
    extra = len(lines) - MAX_LINES
    lines = lines[:MAX_LINES]
    if extra > 0:
        lines.append(f"+{extra} more")
    return "\n".join(lines)


# ---------- delivery ----------
# Pushes never go straight out. A run puts them in the "outbox" inside state.json and saves that
# together with what it has seen, in one atomic write. Only then are they delivered. So a failed save
# can't cause the same push every tick, and a failed delivery stays in the outbox for the next tick.

def _deliver(title: str, body: str) -> bool:
    import push  # imported here so tests can patch this function
    return push.deliver(title, body)


def _online() -> bool:
    """One quick reachability check per tick. Right after the Mac wakes, Wi-Fi is often not up yet;
    a tick then does nothing, so sources don't count a failure and keep their schedule."""
    import socket
    for host in (__import__("os").environ.get("CANVAS_HOST", "{{CANVAS_HOST}}"), "ntfy.sh"):
        try:
            socket.create_connection((host, 443), timeout=5).close()
            return True
        except OSError:
            continue
    return False


def queue_push(state: State, title: str, body: str, now: datetime):
    state.data.setdefault("outbox", []).append(
        {"title": title, "body": body, "ts": now.isoformat(timespec="seconds"), "quiet": in_quiet(now)})


def flush_outbox(state: State, now: datetime) -> int:
    """Deliver what is queued (nothing during quiet hours). Overnight pushes go out as one bundle.
    Each batch is removed and saved BEFORE delivery, then put back if delivery fails, so a disk that
    can't be written never turns into repeated pushes. Returns how many pushes went out."""
    box = state.data.get("outbox") or []
    if not box or in_quiet(now):
        return 0
    quiet = [e for e in box if e.get("quiet")]
    batches = [[e] for e in box if not e.get("quiet")]
    if quiet:
        batches.insert(0, quiet)
    sent = 0
    for batch in batches:
        if len(batch) == 1:
            title, body = batch[0]["title"], batch[0]["body"]
        else:
            title = "autohub (overnight)"
            body = "\n".join(f"[{e['title']}] {e['body']}" for e in batch)
        state.data["outbox"] = [e for e in state.data["outbox"] if e not in batch]
        try:
            state.save()
        except OSError as err:
            state.data["outbox"] = batch + state.data["outbox"]
            log(f"outbox not saved ({err}); holding {len(batch)} push(es)")
            return sent
        try:
            ok = _deliver(title, body)
        except Exception:  # noqa: BLE001 - treat as not delivered; the batch goes back in the outbox
            ok = False
        if ok:
            sent += 1
            continue
        state.data["outbox"] = batch + state.data["outbox"]
        with contextlib.suppress(OSError):
            state.save()
        log("push failed; kept in outbox for the next tick")
        return sent
    return sent


# ---------- running ----------

@dataclass
class Ctx:
    now: datetime
    state_dir: Path
    preview: bool = False


def log(msg: str):
    print(f"{datetime.now().isoformat(timespec='seconds')} {msg}", flush=True)


def _fetch_with_timeout(r: dict, ctx: Ctx, timeout: int):
    out = {}

    def work():
        try:
            out["items"] = sources.REGISTRY[r["source"]["kind"]](r["source"], ctx)
        except BaseException as e:  # noqa: BLE001 - any failure of a source is a failed run
            out["error"] = e
    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError(f"source took longer than {timeout}s")
    if "error" in out:
        raise out["error"]
    return out["items"]


def _short(e: BaseException) -> str:
    s = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
    return s[:80]


def _log_run(rec: dict):
    try:
        with open(STATE_DIR / "runs.jsonl", "a") as fh:
            fh.write(json.dumps({k: v for k, v in rec.items() if k != "would_push"}) + "\n")
    except OSError:
        pass


def run_recipe(r: dict, state: State, ctx: Ctx) -> dict:
    """Run one recipe. Works on a copy of its state and commits it (plus any queued push) to `state`
    in memory at the end; the caller saves. Preview commits nothing."""
    rid, title = r["id"], r["action"]["title"]
    weekly = "weekly_at" in r
    rs = copy.deepcopy(state.recipe(rid))
    rec = {"ts": ctx.now.isoformat(timespec="seconds"), "id": rid, "mode": r["mode"],
           "found": 0, "new": 0, "pushed": None, "error": None}
    pending = []   # (title, body) to queue if this run commits
    if weekly and rs["last_run"] is None and not ctx.preview:
        # First ever run of a weekly recipe: start the schedule, push nothing.
        rs["last_run"], rec["note"] = ctx.now.isoformat(timespec="seconds"), "scheduled for the next slot"
        state.data["recipes"][rid] = rs
        _log_run(rec)
        return rec
    try:
        items = _fetch_with_timeout(r, ctx, SOURCE_TIMEOUT)
    except BaseException as e:  # noqa: BLE001
        rec["error"] = _short(e)
        rs["fails"] += 1
        if rs["fails"] >= 3 and not rs["alerted"]:
            rs["alerted"] = True
            pending.append(("autohub", f"autohub: {rid} is failing: {rec['error']}"))
        if not weekly:          # a weekly run that failed is retried on the next tick
            rs["last_run"] = ctx.now.isoformat(timespec="seconds")
    else:
        rec["found"] = len(items)
        if weekly:              # due() already limits it to once per slot; no seen-diff
            new = [i for i in items if i.get("text")]
        else:
            new = diff(rs, items)
        pushable = [i for i in new if i.get("text")]
        rec["new"] = len(pushable)
        rs["fails"], rs["alerted"] = 0, False
        rs["last_run"] = ctx.now.isoformat(timespec="seconds")
        if pushable:
            rec["would_push"] = bundle(pushable)
            pending.append((title, rec["would_push"]))
    if ctx.preview:
        return rec
    for t, body in pending:
        if r["mode"] == "live":
            queue_push(state, t, body, ctx.now)
            rec["pushed"] = "queued"
        else:
            log(f"WOULD PUSH [{t}] " + body.replace("\n", " | "))
    state.data["recipes"][rid] = rs
    _log_run(rec)
    return rec


def tick(now: datetime = None) -> list:
    now = now or datetime.now()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock = open(STATE_DIR / "tick.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return []
    try:
        if (STATE_DIR / "PAUSED").exists():
            return []
        if not _online():
            log("offline; skipping this tick")
            return []
        state = State(STATE_DIR / "state.json").load()
        recipes, errors = load_recipes(RECIPES_DIR)
        for e in errors:
            log(f"skipped recipe file: {e}")
        records = []
        for r in recipes:
            try:
                if not r["enabled"] or not due(r, state.recipe(r["id"]), now):
                    continue
                rec = run_recipe(r, state, Ctx(now, STATE_DIR, False))
                records.append(rec)
                state.save()
                log(f"{rec['id']} [{rec['mode']}] found={rec['found']} new={rec['new']}"
                    f"{' pushed=' + rec['pushed'] if rec['pushed'] else ''}"
                    f"{' ERROR ' + rec['error'] if rec['error'] else ''}")
            except Exception as e:  # noqa: BLE001 - one recipe must never stop the others
                log(f"{r['id']}: run not saved ({_short(e)})")
                state = State(STATE_DIR / "state.json").load()   # drop unsaved changes, incl. queued pushes
        try:
            sent = flush_outbox(state, now)
            if sent:
                log(f"delivered {sent} push(es)")
        except Exception as e:  # noqa: BLE001
            log(f"delivery step failed ({_short(e)})")
        return records
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


# ---------- operations shared by the CLI and the MCP ----------

class HubError(Exception):
    pass


def _find(rid: str) -> dict:
    recipes, _ = load_recipes(RECIPES_DIR)
    for r in recipes:
        if r["id"] == rid:
            return r
    raise HubError(f"no valid recipe called {rid!r} (see: autohub.py list)")


def _write_recipe(r: dict):
    path = Path(r.get("_file") or RECIPES_DIR / f"{r['id']}.json")
    clean = {k: v for k, v in r.items() if k != "_file"}
    atomic_write(path, json.dumps(clean, indent=2) + "\n")


def _schedule(r: dict) -> str:
    return f"every {r['every_minutes']} min" if "every_minutes" in r else f"weekly {r['weekly_at']}"


def summary() -> list:
    recipes, errors = load_recipes(RECIPES_DIR)
    state = State(STATE_DIR / "state.json").load()
    rows = []
    for r in recipes:
        rs = state.data["recipes"].get(r["id"], new_recipe_state())
        rows.append({"id": r["id"], "enabled": r["enabled"], "mode": r["mode"], "schedule": _schedule(r),
                     "last_run": rs.get("last_run"), "fails": rs.get("fails", 0), "note": r["note"]})
    return rows + [{"error": e} for e in errors]


def set_mode(rid: str, mode: str):
    if mode not in ("dry_run", "live"):
        raise HubError('mode must be "dry_run" or "live"')
    r = _find(rid)
    r["mode"] = mode
    _write_recipe(r)


def set_enabled(rid: str, on: bool):
    r = _find(rid)
    r["enabled"] = bool(on)
    _write_recipe(r)


@contextlib.contextmanager
def _locked():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_DIR / "tick.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)   # waits for a running tick to finish
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def run_one(rid: str, preview: bool = False, reseed: bool = False) -> dict:
    r = _find(rid)
    now = datetime.now()
    if preview:
        state = State(STATE_DIR / "state.json").load()
        return run_recipe(r, state, Ctx(now, STATE_DIR, True))
    with _locked():
        state = State(STATE_DIR / "state.json").load()
        if reseed:
            state.data["recipes"][rid] = new_recipe_state()
        rec = run_recipe(r, state, Ctx(now, STATE_DIR, False))
        state.save()
        flush_outbox(state, now)
        return rec


def recent_runs(n: int = 20, rid: str = None) -> list:
    p = STATE_DIR / "runs.jsonl"
    if not p.exists():
        return []
    rows = []
    for line in p.read_text().splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if rid is None or row.get("id") == rid:
            rows.append(row)
    return rows[-n:]


def pause():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / "PAUSED").write_text(datetime.now().isoformat(timespec="seconds") + "\n")


def resume():
    with contextlib.suppress(FileNotFoundError):
        (STATE_DIR / "PAUSED").unlink()


# ---------- command line ----------

USAGE = """usage: autohub.py <command>
  tick                         run every due recipe (launchd does this every 5 min)
  run <id> [--preview]         run one recipe now (--preview: show what it would push, change nothing)
  list                         recipes with on/off, mode, schedule, last run, failures
  runs [-n 20] [--id X]        recent runs
  pause | resume               stop / restart everything (the launchd job keeps ticking but does nothing)
  init <id>                    forget what a recipe has seen and re-seed silently (no pushes)
  set-mode <id> dry_run|live   live only after the user says so in chat"""


def main(argv: list) -> int:
    try:
        cmd = argv[0] if argv else "help"
        if cmd == "tick":
            tick()
        elif cmd == "run" and len(argv) >= 2:
            rec = run_one(argv[1], preview="--preview" in argv)
            print(json.dumps(rec, indent=1))
        elif cmd == "init" and len(argv) == 2:
            rec = run_one(argv[1], reseed=True)
            print(f"{argv[1]}: re-seeded with {rec['found']} items, nothing pushed"
                  + (f" (error: {rec['error']})" if rec["error"] else ""))
        elif cmd == "list":
            for row in summary():
                if "error" in row:
                    print(f"BROKEN  {row['error']}")
                    continue
                print(f"{row['id']:<18} {'on ' if row['enabled'] else 'off'} {row['mode']:<8} "
                      f"{row['schedule']:<18} last={row['last_run'] or '-'} fails={row['fails']}")
        elif cmd == "runs":
            n = int(argv[argv.index("-n") + 1]) if "-n" in argv else 20
            rid = argv[argv.index("--id") + 1] if "--id" in argv else None
            for row in recent_runs(n, rid):
                print(json.dumps(row))
        elif cmd == "pause":
            pause()
            print("autohub paused (resume: autohub.py resume)")
        elif cmd == "resume":
            resume()
            print("autohub resumed")
        elif cmd == "set-mode" and len(argv) == 3:
            set_mode(argv[1], argv[2])
            print(f"{argv[1]} is now {argv[2]}")
        else:
            print(USAGE)
            return 0 if cmd in ("help", "-h", "--help") else 1
        return 0
    except HubError as e:
        print(f"error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
