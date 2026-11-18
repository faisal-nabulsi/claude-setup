"""Push a short message to the user's phone.

1) ntfy.sh, on the topic their phone already subscribes to (read from semester-tracker/.ntfy.env, never logged).
2) If that fails: semester-tracker/send-push.sh (its optional iMessage-to-self fallback).
Pushes carry only short facts (course, title, score, amount), never message bodies or customer data.
"""
import base64
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

FALL = Path(os.environ.get("SEMESTER_TRACKER", Path.home() / "semester-tracker"))
NTFY_ENV = FALL / ".ntfy.env"
SEND_SMS = FALL / "send-push.sh"


def _topic():
    try:
        for line in Path(NTFY_ENV).read_text().splitlines():
            if line.startswith("NTFY_TOPIC="):
                return line.split("=", 1)[1].strip().strip('"').strip("'") or None
    except OSError:
        return None
    return None


def _header(value: str) -> str:
    try:
        value.encode("latin-1")
        return value
    except UnicodeEncodeError:  # RFC 2047, which ntfy understands
        return "=?UTF-8?B?" + base64.b64encode(value.encode()).decode() + "?="


def _ntfy(title: str, body: str) -> bool:
    topic = _topic()
    if not topic:
        return False
    req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=body.encode(), method="POST",
                                 headers={"Title": _header(title)})
    with urllib.request.urlopen(req, timeout=15) as r:
        return getattr(r, "status", 0) == 200


def _sms(title: str, body: str) -> bool:
    # send-push.sh always exits 0 (even when nothing was sent), so success is read from what it prints.
    r = subprocess.run([str(SEND_SMS), f"{title}: {body}"], capture_output=True, text=True, timeout=30)
    out = r.stdout or ""
    return r.returncode == 0 and ("ntfy push sent" in out or "iMessage sent" in out)


def deliver(title: str, body: str) -> bool:
    """True only when a push really went out. Never raises: a False keeps the push queued."""
    for channel in (_ntfy, _sms):
        try:
            if channel(title, body):
                return True
        except Exception:  # noqa: BLE001 - any failure here just means "not delivered"
            pass
    return False
