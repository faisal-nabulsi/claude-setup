#!/usr/bin/env python3
"""Gradescope reader: copies the user's Gradescope due dates into the tracker.

Runs on the Mac 4 times a day (launchd job {{LABEL_PREFIX}}.gradescope-reader)
in its own Python environment (~/.local/share/gradescope-reader/venv, with
gradescopeapi 1.8.1 pinned). It only reads Gradescope: it logs in, lists this
term's student courses and their assignments, and logs out. It never calls a
function that changes anything there (no uploads, no date or extension edits).

What it does with what it finds (both deadlines.json copies, via deadlines_lib):
  - An assignment the tracker doesn't have yet, due between 3 days ago and 60
    days ahead and not yet submitted, becomes a new row with id "gs-<id>".
  - A row it created earlier gets done when Gradescope shows a submission, and
    follows Gradescope if the due date moves.
  - An assignment that looks like an existing hand-made or Canvas row (same
    course, due within 36 hours, at least two shared title words) is not added
    again; the row is linked to it instead. If Gradescope shows everything
    linked to such a row submitted while the row is still open, the run
    REPORTS it instead of marking it done, because one tracker row can cover
    more than the Gradescope part (a homework may be Gradescope + another site).
  - It never removes a row and never un-dones one.

Report: ~/.local/state/gradescope-reader/report.json, which the SessionStart
hook (hooks/today.py) shows. Credentials come from the Keychain items
"gradescope-email" and "gradescope-password" (gradescope/setup.sh stores them).
They are never printed or written anywhere, and the password is sent only in
the body of the login request, never in a URL.

Usage (with the venv's python):
  reader.py --check     log in and list this term's courses; changes nothing
  reader.py --dry-run   fetch and print what would change; writes nothing
  reader.py             fetch, update both copies, commit + push both repos
"""
import argparse, getpass, hashlib, json, re, subprocess, sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TRACKER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TRACKER / "verify"))
from deadlines_lib import PATHS, dump, load, merge_rows, pull_heartbeat  # noqa: E402

BASE = "https://www.gradescope.com"
# config.json (next to the tracker's schedule.md) holds everything that is yours:
#   {"timezone": "America/New_York", "term": ["Fall", "2026"],
#    "courses": [["CS101", "CS 101"], ["MATH2", "MATH 2"]]}
# Each pair is [text found in the Gradescope course short name, name your tracker labels use].
import os
_CFG = json.loads((TRACKER / "config.json").read_text()) if (TRACKER / "config.json").exists() else {}
TZ = ZoneInfo(_CFG.get("timezone", "America/Los_Angeles"))
TERM = tuple(_CFG.get("term", ["Fall", "2026"]))
STATE = Path.home() / ".local/state/gradescope-reader"
REPORT = STATE / "report.json"
ADD_BACK, ADD_AHEAD = timedelta(days=3), timedelta(days=60)
LOOK_BACK = timedelta(days=14)  # submitted items this recent can still close the loop on a linked row
MATCH_HOURS = 36

# Gradescope short name -> the course name the tracker's labels use (today.py tags by these)
COURSES = [tuple(c) for c in _CFG.get("courses", [["CS101", "CS 101"]])]
TAG_KEYS = {name: [short.upper()] for short, name in COURSES}


STOP = {"THE", "AND", "FOR", "DUE", "GRADESCOPE", "SUBMIT", "SUBMISSION", "WITH", "FROM",
        "TONIGHT", "TODAY", "NOT", "ONLY", "PART",
        "MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN", "DONE", "READD"}


class SetupError(Exception):
    """Missing Keychain item or similar; fixed by running gradescope/setup.sh."""


class LoginError(Exception):
    """Gradescope refused the login (wrong password, or school login required)."""


@dataclass
class Item:
    """One Gradescope assignment as the tracker needs it."""
    course_id: str
    course: str
    key: str            # Gradescope assignment id, or "x" + hash when the page shows none
    name: str
    due: datetime | None
    late_due: datetime | None
    submitted: bool


@dataclass
class Plan:
    rows: list[dict]
    added: list[dict] = field(default_factory=list)
    done: list[dict] = field(default_factory=list)
    moved: list[tuple[str, str, str]] = field(default_factory=list)       # (label, old when, new when)
    linked: list[tuple[str, str]] = field(default_factory=list)          # (row label, assignment name)
    open_submitted: list[dict] = field(default_factory=list)
    ambiguous: list[dict] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.added or self.done or self.moved or self.linked)


# ---------- pure logic (tested in test_reader.py) ----------

def course_name(short: str, full: str = "") -> str:
    up = f"{short} {full}".upper()
    for key, name in COURSES:
        if key in up:
            return name
    return re.sub(r"\s+", " ", short).strip()


def local(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ).strftime("%Y-%m-%dT%H:%M")


def words(text: str) -> set[str]:
    t = re.sub(r"\b\d{1,2}:\d{2}\s*(?:am|pm)?|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b", " ", text, flags=re.I).upper()
    return {w for w in re.findall(r"[A-Z]{3,}|\d+", t) if w not in STOP}


def is_submitted(status: str, grade) -> bool:
    s = (status or "").strip().lower()
    return grade is not None or (s.startswith("submitted") or s.startswith("graded")) and "no submission" not in s


def fmt_when(when: str) -> str:
    dt = datetime.fromisoformat(when)
    return f"{dt.strftime('%a')} {dt.month}/{dt.day} " + dt.strftime("%-I:%M%p").lower().replace(":00", "")


def new_row(it: Item, now: datetime) -> dict:
    when = local(it.due)
    label = f"{it.course} {it.name} (Gradescope)"
    late = local(it.late_due)
    if late and late != when:
        label += f" — late until {fmt_when(late)}"
    return {"id": f"gs-{it.key}", "when": when, "label": label,
            "short": f"{it.course.split()[0]} {it.name}"[:40].strip(),
            "gradescope_id": it.key, "gradescope_course": it.course_id,
            "source": "gradescope", "updated": now.strftime("%Y-%m-%dT%H:%M")}


def _candidates(rows: list[dict], it: Item) -> list[tuple[int, dict]]:
    """Existing non-Gradescope rows this assignment probably is, best first."""
    due = datetime.fromisoformat(local(it.due))
    keys = TAG_KEYS.get(it.course, [it.course.split()[0].upper()])
    mine = words(it.name)
    out = []
    for r in rows:
        if r.get("source") == "gradescope":
            continue
        label = r["label"].upper()
        if not any(k in label for k in keys):
            continue
        if abs((datetime.fromisoformat(r["when"]) - due).total_seconds()) > MATCH_HOURS * 3600:
            continue
        overlap = len(mine & words(r["label"]))
        if overlap >= 2 or (overlap >= 1 and r["when"] == local(it.due)):
            out.append((overlap, r))
    return sorted(out, key=lambda x: -x[0])


def plan(rows: list[dict], items: list[Item], now: datetime) -> Plan:
    """What to change. Rows are never removed and done never flips back.
    """
    stamp = now.strftime("%Y-%m-%dT%H:%M")
    by_id = {r["id"]: dict(r) for r in rows}
    order = [r["id"] for r in rows]
    p = Plan(rows=[])
    linked_items: dict[str, list[Item]] = {}
    for it in items:
        if it.due is None:
            continue
        due = datetime.fromisoformat(local(it.due))
        if not (now - LOOK_BACK <= due <= now + ADD_AHEAD):
            continue
        gid = f"gs-{it.key}"
        if gid in by_id:  # a row this reader made
            r = by_id[gid]
            if r.get("done"):
                continue
            if it.submitted:
                lbl = f"DONE {now.month}/{now.day} (submitted on Gradescope) — {r['label']} DO NOT RE-ADD."
                by_id[gid] = {**r, "label": lbl, "done": True, "done_at": stamp, "updated": stamp}
                p.done.append(by_id[gid])
            elif r["when"] != local(it.due):
                p.moved.append((r["label"], r["when"], local(it.due)))
                by_id[gid] = {**r, "when": local(it.due), "updated": stamp}
            continue
        known = [r for r in by_id.values() if it.key in (r.get("gradescope_ids") or [])]
        if known:
            linked_items.setdefault(known[0]["id"], []).append(it)
            continue
        cands = _candidates(list(by_id.values()), it)
        if len(cands) > 1 and cands[0][0] == cands[1][0]:
            p.ambiguous.append({"assignment": f"{it.course} {it.name}", "due": local(it.due),
                                "rows": [c[1]["label"][:80] for c in cands[:3]]})
            continue
        if cands:
            r = cands[0][1]
            if it.key not in (r.get("gradescope_ids") or []):
                upd = {**r, "gradescope_ids": [*(r.get("gradescope_ids") or []), it.key]}
                if not r.get("done"):
                    upd["updated"] = stamp  # done rows win the merge anyway; no stamp needed
                by_id[r["id"]] = upd
                p.linked.append((r["label"][:80], it.name))
            linked_items.setdefault(r["id"], []).append(it)
            continue
        if it.submitted or due < now - ADD_BACK:
            continue
        row = new_row(it, now)
        by_id[row["id"]] = row
        order.append(row["id"])
        p.added.append(row)
    for rid, its in linked_items.items():
        r = by_id[rid]
        if not r.get("done") and its and all(i.submitted for i in its):
            p.open_submitted.append({"id": rid, "label": r["label"][:120],
                                     "assignments": [f"{i.course} {i.name}" for i in its]})
    p.rows = sorted((by_id[i] for i in order), key=lambda r: (r["when"], r["id"]))
    return p


# ---------- Gradescope (network) ----------

def keychain(service: str) -> str:
    r = subprocess.run(["/usr/bin/security", "find-generic-password", "-a", getpass.getuser(),
                        "-s", service, "-w"], capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        raise SetupError(f"Keychain item {service!r} is missing; run gradescope/setup.sh")
    return r.stdout.rstrip("\n")


def make_session():
    import requests

    class Session(requests.Session):
        def request(self, *a, **kw):  # the library sets no timeouts; never hang a background job
            kw.setdefault("timeout", 30)
            return super().request(*a, **kw)
    return Session()


def login(session, email: str, password: str) -> None:
    """Gradescope's login form, sent the way a browser sends it: in the POST body.
    (gradescopeapi's own login puts the password in the URL, so it isn't used.)"""
    from bs4 import BeautifulSoup
    home = session.get(BASE)
    home.raise_for_status()
    tok = BeautifulSoup(home.text, "html.parser").select_one(
        'form[action="/login"] input[name="authenticity_token"]')
    if tok is None:
        raise LoginError("Gradescope's login page changed (no login form found)")
    resp = session.post(f"{BASE}/login", data={
        "utf8": "✓", "session[email]": email, "session[password]": password,
        "session[remember_me]": "0", "commit": "Log In", "session[remember_me_sso]": "0",
        "authenticity_token": tok["value"]})
    if not resp.history or resp.history[0].status_code != 302 or "/login" in resp.url:
        raise LoginError("Gradescope refused the email/password (changed password, or school login only?)")
    csrf = BeautifulSoup(resp.text, "html.parser").select_one('meta[name="csrf-token"]')
    if csrf is not None:
        session.headers["X-CSRF-Token"] = csrf["content"]


def account_page_courses(session) -> dict:
    """This term's courses read straight from the /account page's course list.
    Fallback for when gradescopeapi's parser misses them (it looks for a
    "Student Courses" heading that Gradescope replaced with "Your Courses" in Nov 2026)."""
    from types import SimpleNamespace
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(session.get(f"{BASE}/account", headers={"Accept": "text/html"}).text, "html.parser")
    out = {}
    for head in soup.select("h2.courseList--term"):
        parts = head.get_text(" ", strip=True).split()
        if len(parts) != 2 or tuple(parts) != TERM:
            continue
        box = head.find_next_sibling("div", class_="courseList--coursesForTerm")
        for a in (box.select("a.courseBox") if box else []):
            m = re.search(r"/courses/(\d+)", a.get("href", ""))
            if not m:
                continue
            short = a.select_one(".courseBox--shortname")
            full = a.select_one(".courseBox--name")
            out[m.group(1)] = SimpleNamespace(
                name=short.get_text(strip=True) if short else "",
                full_name=full.get_text(strip=True) if full else "",
                semester=parts[0], year=parts[1])
    return out


def fetch(session) -> tuple[list[Item], list[str], list[str]]:
    """This term's student courses and assignments. Returns (items, course names, errors)."""
    from gradescopeapi.classes.account import Account
    acct = Account(session, BASE)
    courses = {cid: c for cid, c in acct.get_courses()["student"].items()
               if (c.semester, c.year) == TERM}
    if not courses:  # Nov 2026: Gradescope renamed "Student Courses" to "Your Courses"; the library then finds none
        courses = account_page_courses(session)
    items, names, errors = [], [], []
    for cid, c in courses.items():
        name = course_name(c.name, c.full_name)
        names.append(name)
        try:
            for a in acct.get_assignments(cid):
                title = re.sub(r"\s+", " ", a.name or "").strip()
                key = a.assignment_id or "x" + hashlib.sha1(f"{cid}|{title}".encode()).hexdigest()[:10]
                items.append(Item(cid, name, str(key), title, a.due_date, a.late_due_date,
                                  is_submitted(a.submissions_status, a.grade)))
        except Exception as e:  # one course's page failing shouldn't hide the others
            errors.append(f"{name}: {type(e).__name__}: {str(e)[:120]}")
    return items, names, errors


# ---------- git ----------

def git(repo: Path, *args, check=False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          timeout=60, check=check)


def commit_push(repo: Path, msg: str) -> str:
    """Commit deadlines.json only, on main only, then push. If the remote moved,
    merge it with the union rules (deadlines_lib) instead of failing or overwriting."""
    branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if branch != "main":
        return f"not committed: on branch {branch!r}, not main"
    if not git(repo, "status", "--porcelain", "--", "deadlines.json").stdout.strip():
        return "nothing to commit"
    r = git(repo, "commit", "-q", "-m", msg, "--", "deadlines.json")
    if r.returncode != 0:
        return f"commit failed: {(r.stderr or r.stdout).strip()[:150]}"
    if git(repo, "push", "-q").returncode == 0:
        return "committed + pushed"
    if git(repo, "fetch", "-q").returncode != 0:
        return "committed; push failed (offline?)"
    m = git(repo, "merge", "--no-ff", "--no-commit", "origin/main")
    conflicted = set(git(repo, "diff", "--name-only", "--diff-filter=U").stdout.split())
    if conflicted - {"deadlines.json"} or (m.returncode != 0 and not conflicted):
        git(repo, "merge", "--abort")
        return f"committed; push failed and the merge needs a human ({', '.join(sorted(conflicted)) or m.stderr.strip()[:80]})"
    ours = json.loads(git(repo, "show", "HEAD:deadlines.json").stdout)
    theirs = json.loads(git(repo, "show", "origin/main:deadlines.json").stdout)
    merged, _ = merge_rows(ours, theirs)
    dump(repo / "deadlines.json", merged)
    git(repo, "add", "deadlines.json")
    git(repo, "commit", "-q", "--no-edit")
    if git(repo, "push", "-q").returncode == 0:
        return "committed + merged with remote (union) + pushed"
    return "committed + merged; push failed"


# ---------- run ----------

def summary(p: Plan) -> list[str]:
    out = [f"+ {r['when']}  {r['label']}" for r in p.added]
    out += [f"✓ done: {r['label'][:100]}" for r in p.done]
    out += [f"↻ moved: {lbl[:70]}  {a} → {b}" for lbl, a, b in p.moved]
    out += [f"↔ linked: {name}  →  {lbl}" for lbl, name in p.linked]
    out += [f"? submitted on Gradescope, tracker row still open: {o['label'][:90]} ({o['id']})" for o in p.open_submitted]
    out += [f"? not added (matches several rows): {a['assignment']} due {a['due']}" for a in p.ambiguous]
    out += [f"- skipped: {x}" for x in p.skipped]
    return out or ["no changes"]


def alert_once_a_day(msg: str, now: datetime) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    flag = STATE / "last-alert"
    if flag.exists() and flag.read_text().strip() == now.date().isoformat():
        return
    flag.write_text(now.date().isoformat())
    subprocess.run(["bash", str(TRACKER / "send-push.sh"), msg], capture_output=True, timeout=60)


def write_report(rep: dict) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(rep, indent=1, ensure_ascii=False) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    now = datetime.now(TZ).replace(tzinfo=None, second=0, microsecond=0)
    rep = {"ran_at": now.strftime("%Y-%m-%dT%H:%M"), "ok": False}
    session = make_session()
    try:
        login(session, keychain("gradescope-email"), keychain("gradescope-password"))
        items, names, errors = fetch(session)
    except (SetupError, LoginError) as e:
        print(f"gradescope reader: {e}")
        if not (a.check or a.dry_run):
            write_report({**rep, "error": str(e)})
            alert_once_a_day(f"Gradescope reader can't log in: {e}. Fix: run gradescope/setup.sh", now)
        return 1
    except Exception as e:
        msg = f"{type(e).__name__}: {str(e)[:160]}"
        print(f"gradescope reader failed: {msg}")
        if not (a.check or a.dry_run):
            write_report({**rep, "error": msg})
        return 1
    finally:
        try:
            session.get(f"{BASE}/logout")
        except Exception:
            pass
    print(f"logged in; {len(names)} course(s) this term: {', '.join(names) or 'none'}; {len(items)} assignment(s)")
    for e in errors:
        print(f"  ! {e}")
    if a.check:
        return 0
    if not a.dry_run:
        pull_heartbeat()
    merged, _ = merge_rows(load(PATHS["tracker"]), load(PATHS["heartbeat"]))
    p = plan(merged, items, now)
    print("\n".join(summary(p)))
    if a.dry_run:
        return 0
    commits = {}
    if p.changed:
        for path in PATHS.values():
            dump(path, p.rows)
        bits = [f"+{len(p.added)}" if p.added else "", f"{len(p.done)} done" if p.done else "",
                f"{len(p.moved)} moved" if p.moved else "", f"{len(p.linked)} linked" if p.linked else ""]
        msg = "gradescope: " + ", ".join(b for b in bits if b)
        for name, path in PATHS.items():
            commits[name] = commit_push(path.parent, msg)
            print(f"{name}: {commits[name]}")
    write_report({**rep, "ok": True, "courses": names, "assignments": len(items), "errors": errors,
                  "added": [r["label"] for r in p.added], "done": [r["label"] for r in p.done],
                  "moved": [list(m) for m in p.moved], "linked": [list(x) for x in p.linked],
                  "open_submitted": p.open_submitted, "ambiguous": p.ambiguous, "skipped": p.skipped, "commits": commits})
    return 0


if __name__ == "__main__":
    sys.exit(main())
