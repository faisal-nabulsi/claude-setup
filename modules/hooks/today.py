#!/usr/bin/env python3
"""SessionStart hook: print today's to-do state so Claude starts every session
(and every post-compaction context) already knowing it.

Reads both deadlines.json copies (done wins, nothing is written) plus the
"Reminders (no due date)" section of schedule.md. Personal sessions get the
full list; coding sessions elsewhere get a 3-line summary. Labels are trimmed
and stripped of control characters because Canvas text comes from outside.
"""
import json, os, re, sys
from datetime import datetime, timedelta
from pathlib import Path

TRACKER = Path(os.environ.get("SEMESTER_TRACKER", Path.home() / "semester-tracker"))
sys.path.insert(0, str(TRACKER / "verify"))
HOME = str(Path.home())
FULL_ROOTS = [str(TRACKER), HOME + "/life", HOME + "/Library/Application Support/Claude"]


def clean(label: str, n: int = 105) -> str:
    s = re.sub(r"[\x00-\x1f\x7f]", " ", label).replace("<", "‹").replace(">", "›")
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def short_title(r: dict, n: int = 30) -> str:
    """A few readable words for the band: the row's own "short" if it has one,
    else the label up to its first dash/paren/colon-detail, minus "due ..."
    phrases and dates, with SHOUTED words title-cased."""
    if r.get("short"):
        return clean(r["short"], n)
    s = clean(r["label"], 200)
    s = re.split(r" — | – | \(|; ", s)[0]
    s = re.sub(r"\b(due|by)\b.*$", "", s, flags=re.I)
    s = re.sub(r"\b\d{1,2}/\d{1,2}\b|\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b|\bEoD\b", "", s)
    keep = {k for k, _ in TAGS} | {"MT1", "MT2", "HW"}
    words = [w if w.strip(":,") in keep else (w.capitalize() if w.isupper() and len(w) > 1 else w) for w in s.split()]
    s = re.sub(r"\s+", " ", " ".join(words)).strip(" :-,")
    if len(s) > n:
        s = s[: n].rsplit(" ", 1)[0] + "…"
    return s


# Tags for the /todo panel and band: (text found in a label, tag shown). Put your own in
# <tracker>/tags.json as [["CS 101", "CS101"], ["CLUB", "Club"]]; anything unmatched is "Other".
try:
    TAGS = [tuple(x) for x in json.loads((TRACKER / "tags.json").read_text())]
except Exception:
    TAGS = []


def tag_of(r: dict) -> str:
    up = r["label"].upper()
    hits = [(up.find(k), t) for k, t in TAGS if k in up]
    return min(hits)[1] if hits else "Other"


def pretty_json(rows, now) -> dict:
    """Structured view for the /todo panel: missed items, then each of the next 7 days."""
    def item(t, r):
        return {"time": t.strftime("%-I:%M%p").lower().replace(":00", ""), "title": short_title(r, 40),
                "tag": tag_of(r), "soft": bool(r.get("soft")), "urgent": not r.get("soft") and 0 <= (t - now).total_seconds() <= 3 * 3600}
    items = sorted(((datetime.fromisoformat(r["when"]), r) for r in rows if not r.get("done")), key=lambda x: x[0])
    missed = [item(t, r) for t, r in items if not r.get("soft") and now - timedelta(days=3) <= t < now]
    days, shown = [], 0
    for d in range(7):
        day = (now + timedelta(days=d)).date()
        todays = [(t, r) for t, r in items if t.date() == day and t >= now]
        if not todays:
            continue
        name = "Today" if d == 0 else "Tomorrow" if d == 1 else (now + timedelta(days=d)).strftime("%A")
        days.append({"label": f"{name} · {(now + timedelta(days=d)).strftime('%b %-d')}", "items": [item(t, r) for t, r in todays]})
        shown += len(todays)
    later = [1 for t, r in items if t.date() > (now + timedelta(days=6)).date() and t <= now + timedelta(days=14) and not r.get("soft")]
    return {"now": now.strftime("%a %b %-d · %-I:%M") + now.strftime("%p").lower(), "missed": missed,
            "days": days, "later": len(later)}


def pretty_text(rows, now) -> str:
    """Human view for `/todo text`: grouped by day, times aligned, short titles, nothing cut off."""
    d = pretty_json(rows, now)
    lines = [f"TO-DO  ·  {d['now']}", ""]
    def fmt(i):
        mark = "· " if i["soft"] else ("! " if i["urgent"] else "  ")
        return f"  {i['time']:>7}   {mark}{i['title']:<36} {i['tag']}"
    if d["missed"]:
        lines.append("MISSED?  (passed, not marked done)")
        lines += [fmt(i) for i in d["missed"]]
        lines.append("")
    for day in d["days"]:
        lines.append(day["label"].upper())
        lines += [fmt(i) for i in day["items"]]
        lines.append("")
    if d["later"]:
        lines.append(f"+{d['later']} more the week after")
    lines.append("·  = self-imposed      !  = due within 3 hours")
    return "\n".join(lines)


def when_str(dt: datetime) -> str:
    t = dt.strftime("%-I:%M%p").lower().replace(":00", "")
    return f"{dt.strftime('%a')} {dt.month}/{dt.day} {t}"


def gradescope_lines(rows, now) -> list[str]:
    """What the Gradescope reader (semester-tracker/gradescope/reader.py) last reported, when it needs attention."""
    p = Path.home() / ".local/state/gradescope-reader/report.json"
    if not p.exists():
        return []
    try:
        rep = json.loads(p.read_text())
        ran = datetime.fromisoformat(rep["ran_at"])
    except Exception:
        return ["Gradescope reader: its report is unreadable (~/.local/state/gradescope-reader/report.json)."]
    done = {r["id"] for r in rows if r.get("done")}
    out = []
    if not rep.get("ok"):
        out.append(f"Gradescope reader FAILED {when_str(ran)}: {clean(rep.get('error', ''), 120)} "
                   "(if it's the login, the user re-runs semester-tracker/gradescope/setup.sh).")
    elif now - ran > timedelta(hours=36):
        out.append(f"Gradescope reader last ran {when_str(ran)}; see ~/Library/Logs/gradescope-reader.log.")
    for o in rep.get("open_submitted", []):
        if o.get("id") not in done:
            out.append(f"Gradescope shows submitted, tracker row still open (confirm with the user, then "
                       f"mark_done.py {o['id']}): {clean(o['label'], 90)}")
    for a in rep.get("ambiguous", []):
        out.append(f"Gradescope assignment not added (matches several rows): {clean(a['assignment'], 80)} due {a['due']}")
    return out


def main() -> None:
    # --full / --band: called by the todo mod (no stdin); otherwise a SessionStart hook
    flags = set(sys.argv[1:])
    raw = "" if flags & {"--full", "--band", "--json", "--pretty"} else sys.stdin.read()
    data = json.loads(raw) if raw.strip() else {}
    cwd = os.path.realpath(data.get("cwd") or os.getcwd())
    source = data.get("source", "startup")
    from deadlines_lib import PATHS, load, merge_rows
    rows, _ = merge_rows(load(PATHS["tracker"]), load(PATHS["heartbeat"]))
    now = datetime.now()
    week = now + timedelta(days=7)
    items = [(datetime.fromisoformat(r["when"]), r) for r in rows]
    upcoming = [(t, r) for t, r in items if not r.get("done") and now <= t <= week]
    hard = [(t, r) for t, r in upcoming if not r.get("soft")]
    soft = [(t, r) for t, r in upcoming if r.get("soft")]

    if "--pretty" in flags:  # human text view for `/todo text`
        print(pretty_text(rows, now))
        return
    if "--json" in flags:  # structured view for the /todo panel
        print(json.dumps(pretty_json(rows, now)))
        return
    if "--band" in flags:  # compact JSON for the status-band mod above the prompt
        soon = [(t, r) for t, r in hard if t <= now + timedelta(hours=48)][:4]
        items = []
        for t, r in soon:
            hrs = (t - now).total_seconds() / 3600
            day = "" if t.date() == now.date() else ("Tmrw " if t.date() == (now + timedelta(days=1)).date() else t.strftime("%a "))
            items.append({"time": day + t.strftime("%-I:%M%p").lower().replace(":00", ""),
                          "title": short_title(r), "urgent": hrs <= 3, "today": t.date() == now.date()})
        print(json.dumps({"items": items, "week": len(hard)}))
        return
    full = ("--full" in flags or cwd in (HOME, HOME + "/Desktop")
            or any(cwd.startswith(os.path.realpath(p)) for p in FULL_ROOTS))
    if not full:
        nxt = "; ".join(f"{when_str(t)} {clean(r['label'], 60)}" for t, r in hard[:3])
        print(f"Next deadlines (tracker): {nxt or 'none in the next 7 days'}. "
              f"Full list: ask for the to-do list or run /todo.")
        return

    out = [f"TODAY: {now.strftime('%a %b %-d, %-I:%M')}{now.strftime('%p').lower()} — your tracker "
           f"({'re-injected after compaction' if source == 'compact' else 'loaded at session start'}). "
           f"Source of truth: {TRACKER} (schedule.md + deadlines.json; done wins). "
           "Mark finished items with: python3 verify/mark_done.py \"<text>\" (updates both copies)."]
    past = [(t, r) for t, r in items if not r.get("done") and not r.get("soft") and now - timedelta(days=3) <= t < now]
    if past:
        out.append("Past in the last 3 days, not marked done (ask the user before assuming either way):")
        out += [f"  {when_str(t)}  {clean(r['label'])}" for t, r in past[-8:]]
    out.append("Due / happening in the next 7 days:")
    out += [f"  {when_str(t)}  {clean(r['label'])}" for t, r in hard] or ["  (nothing hard-dated)"]
    if soft:
        out.append("Soft targets (self-imposed):")
        out += [f"  {when_str(t)}  {clean(r['label'], 80)}" for t, r in soft[:14]]
    recent = [r for r in rows if r.get("done") and r.get("done_at")
              and datetime.fromisoformat(r["done_at"]) >= now - timedelta(hours=72)]
    if recent:
        out.append("Done in the last 72h — DO NOT re-add:")
        out += [f"  {clean(r['label'], 90)}" for r in recent]
    out += gradescope_lines(rows, now)
    sched = (TRACKER / "schedule.md").read_text()
    m = re.search(r"## Reminders \(no due date\)\n(.*?)(?=\n## |\Z)", sched, re.S)
    rem = [clean(l[2:], 90) for l in (m.group(1).splitlines() if m else []) if l.startswith("- ")]
    if rem:
        out.append("Reminders (no due date): " + "; ".join(rem))
    print("\n".join(out)[:6000])


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never block a session start
        print(f"(today hook could not read the tracker: {e!r})")
