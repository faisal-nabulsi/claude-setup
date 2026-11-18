#!/usr/bin/env python3
"""Example ntfy job alert. Reads a public JSON list of postings, keeps the ones that match
config.json, and pushes one ntfy message per run listing the NEW ones.

Many community-maintained internship lists publish a listings.json in their GitHub repo;
point SOURCE_URL at one of those (or any URL that returns a JSON list of objects).
Standard library only, so the Actions runner needs no installs.
"""
import json
import os
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CFG = json.loads((HERE / "config.json").read_text())
SEEN_PATH = HERE / "seen.json"


def fetch():
    req = urllib.request.Request(CFG["source_url"], headers={"User-Agent": "job-alert"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def matches(row):
    text = json.dumps(row).lower()
    if any(w.lower() in text for w in CFG.get("exclude", [])):
        return False
    return any(w.lower() in text for w in CFG.get("include", [""]))


def push(body):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        print("NTFY_TOPIC not set; would push:\n" + body)
        return
    req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=body.encode(),
                                 headers={"Title": "New postings", "Tags": "briefcase"})
    urllib.request.urlopen(req, timeout=15)


def main():
    seen = set(json.loads(SEEN_PATH.read_text())) if SEEN_PATH.exists() else set()
    first_run = not seen
    new = []
    for row in fetch():
        key = str(row.get(CFG.get("id_field", "id")) or row.get("url"))
        if key in seen or not matches(row):
            continue
        seen.add(key)
        new.append(row)
    SEEN_PATH.write_text(json.dumps(sorted(seen)))
    if first_run:  # the first run only records what exists, so you never get a flood
        print(f"first run: recorded {len(new)} existing postings, pushed nothing")
        return
    if new:
        lines = [f"{r.get('company_name', r.get('company', '?'))}: {r.get('title', '?')}" for r in new[:5]]
        more = f"\n+{len(new) - 5} more" if len(new) > 5 else ""
        push("\n".join(lines) + more)
    print(f"{len(new)} new")


if __name__ == "__main__":
    main()
