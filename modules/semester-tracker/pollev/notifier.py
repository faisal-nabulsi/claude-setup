#!/usr/bin/env python3
"""PollEv notifier: pushes the user's phone when a poll opens during lecture.

It only WATCHES. It never logs in, never answers a poll and never sends
anything to Poll Everywhere except the same public "is anything open?" request
the pollev.com page makes. The push links to pollev.com/<presenter> so the user
answers it themselves, in the room.

How it works: PollEv's public firehose endpoint
  https://firehose-production.polleverywhere.com/users/<presenter>/activity/current.json
is a long poll. It answers 204 after ~20 s when nothing is open, and 200 with
{"message": "{\"uid\": ...}"} while a poll is live. One thread per presenter
keeps asking during the lecture window; each new poll uid triggers one push.

launchd ({{LABEL_PREFIX}}.pollev-notifier) starts it a few minutes before
each lecture window in config.json. It exits when the window
ends, so it never runs outside lecture. The Mac must be awake for it to work.

Usage:
  notifier.py              watch the lecture happening now (exits if none)
  notifier.py --check      one request per presenter; prints status, no push
  notifier.py --test-push  send one test push to the phone
"""
import argparse, json, subprocess, sys, threading, time
import urllib.error, urllib.request
from datetime import datetime, timedelta

from config import COURSES, SEND, STATE, TZ, course_at

FIREHOSE = ("https://firehose-production.polleverywhere.com/users/{host}/activity/"
            "current.json?last_message_sequence=0&_={ts}")
SEEN = STATE / "notifier-seen.json"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Safari/605.1.15"
lock = threading.Lock()


def log(msg):
    print(f"{datetime.now(TZ):%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def load_seen():
    try:
        return set(json.loads(SEEN.read_text()))
    except (OSError, ValueError):
        return set()


def save_seen(seen):
    STATE.mkdir(parents=True, exist_ok=True)
    SEEN.write_text(json.dumps(sorted(seen)[-500:]))


def poll_uid(host, timeout=30):
    """Return (status, uid). uid is None when nothing is open."""
    url = FIREHOSE.format(host=host, ts=int(time.time() * 1000))
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            if r.status != 200 or not body:
                return r.status, None
            msg = json.loads(body).get("message")
            data = json.loads(msg) if isinstance(msg, str) else (msg or {})
            return 200, data.get("uid")
    except urllib.error.HTTPError as e:
        return e.code, None
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, None          # long poll timed out or network blip
    except (ValueError, AttributeError):
        return 200, None        # 200 without a parseable poll


def push(text):
    subprocess.run(["bash", str(SEND), text], check=False, timeout=60)


def watch(host, course, until, seen):
    errors = 0
    while datetime.now(TZ) < until:
        status, uid = poll_uid(host)
        if uid:
            with lock:
                new = uid not in seen
                if new:
                    seen.add(uid)
                    save_seen(seen)
            if new:
                log(f"{course}: poll open on {host} ({uid}); pushing")
                push(f"📊 {course} poll is open: pollev.com/{host}")
            time.sleep(5)       # the endpoint repeats an open poll right away
        elif status in (0, 204, 200):
            errors = 0
        else:
            errors += 1
            log(f"{host}: HTTP {status}")
            time.sleep(min(60, 5 * errors))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--test-push", action="store_true")
    a = ap.parse_args()

    if a.test_push:
        push("📊 PollEv notifier test: if you see this, lecture poll alerts will reach you.")
        return
    if a.check:
        for c in COURSES:
            for h in c["hosts"]:
                status, uid = poll_uid(h)
                print(f"{c['course']:10} {h:12} status={status} open_poll={'yes' if uid else 'no'}")
        return

    now = datetime.now(TZ)
    c = course_at(now + timedelta(minutes=5))   # launchd starts us ~5 min early
    if not c:
        log("no lecture in this window; exiting")
        return
    until = datetime.combine(now.date(), c["end"], TZ) + timedelta(minutes=10)
    log(f"watching {c['course']} ({', '.join(c['hosts'])}) until {until:%H:%M}")
    seen = load_seen()
    threads = [threading.Thread(target=watch, args=(h, c["course"], until, seen), daemon=True)
               for h in c["hosts"]]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    log("lecture window over; exiting")


if __name__ == "__main__":
    sys.exit(main())
