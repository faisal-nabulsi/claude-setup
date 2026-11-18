"""Feed new outgoing iMessages into the same evidence spool the sweep drains.

The capture loop already turns the messages the user types to Claude into facts.
Their texts are the same kind of source -- their own words, `conf/stated` quality --
so they go through the same door rather than a parallel one: redact, spool,
and let the nightly sweep apply the identical grounding, quote-anchoring,
supersession and held-for-review guarantees. Nothing here decides what a
message MEANS; that stays the sweep's job, behind its two string-checks.

**Only outgoing messages, in v1, and that is a correctness choice not a
shortcut.** The sweep's extractor is written for "messages the user typed" -- it
reads every line as their own words. That is exactly true of `is_from_me`
messages and exactly false of incoming ones, where "i got the job" is someone
else's news. Feeding incoming texts through a prompt that reads them as their
would manufacture biography out of other people's lives. Incoming messages are
worth reading, but they need the extractor reframed first; until then this
door stays the one it can be trusted with.

**A watermark, so a nightly run reads only what is new.** `chat.db` grows for
years; re-reading it whole every night would re-propose the same facts forever
and cost more each night than the last. The watermark is the timestamp of the
newest message already spooled; the next run asks `read_messages` for messages
strictly after it. The ledger is the second line of defence -- a message whose
uuid is already banked is skipped even if the watermark is stale -- so a
crash between spooling and watermark-writing costs a re-read, never a
duplicate.

**Full Disk Access is required and cannot be granted from here.** `chat.db`
lives under Messages' TCC protection, so the launchd job's interpreter
(`/usr/bin/python3`) has to be granted Full Disk Access in System Settings ->
Privacy & Security, or every read returns an empty database and the ingest
silently banks nothing. `probe_access` turns that into a checkable condition
instead of a silent zero.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from ..sources import imessage
from ..sources.redact import is_sensitive, mask_soft, scan_hard
from . import harvest

#: Where the newest-spooled-message timestamp is kept. Under `_queue/` because
#: it is per-machine runtime state, not a fact -- nothing under `sources/` may
#: be gitignored (that tree is the vault's only backup), and a mutable cursor
#: does not belong in a backup. If a `git clean` ever removes it the next run
#: cold-starts forward-only, losing at most the few days since, which is
#: self-healing rather than a re-proposal of years of history.
def watermark_path(vault: Path) -> Path:
    return vault / "_queue" / "imessage-watermark"


def default_db() -> Path:
    return Path.home() / "Library" / "Messages" / "chat.db"


def load_watermark(vault: Path) -> datetime | None:
    """The timestamp of the newest message already spooled, or None the first
    time. A malformed file reads as None rather than crashing the nightly job:
    the ledger still prevents duplicates, so the cost is a one-time re-read."""
    path = watermark_path(vault)
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def save_watermark(vault: Path, when: datetime) -> None:
    path = watermark_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(when.isoformat(), encoding="utf-8")
    tmp.replace(path)


def probe_access(db_path: Path) -> tuple[bool, str]:
    """Can this process actually read message text, or is TCC blocking it?

    A missing Full Disk Access grant does not raise -- it opens a database that
    is present but returns zero rows -- so 'read 0 new messages' is ambiguous
    between 'nothing new' and 'not allowed to see anything'. This distinguishes
    them: an accessible db answers a COUNT, a blocked one raises
    OperationalError on the same query.
    """
    if not db_path.exists():
        return False, f"no database at {db_path}"
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            con.execute("SELECT COUNT(*) FROM message").fetchone()
        finally:
            con.close()
    except sqlite3.Error as exc:
        # sqlite3.Error, not just OperationalError: a TCC denial surfaces as
        # `DatabaseError: authorization denied`, which OperationalError would
        # miss -- and it did, leaking as an opaque exit 2 from the launchd job
        # instead of this actionable message.
        return False, (f"cannot read {db_path} ({exc}). Grant Full Disk Access "
                       f"to /usr/bin/python3 in System Settings -> Privacy & "
                       f"Security -> Full Disk Access, then re-run.")
    return True, "ok"


def _record(msg: imessage.Message) -> dict | None:
    """One evidence record from one outgoing message, or None if it is dropped.

    Same shape and same gates as `harvest`: a hard secret drops the whole
    message (a masked credential is still re-derivable, per spec 12.6), soft
    identifiers are masked in place, and `sensitive` is computed from the words
    as sent so a later paraphrase cannot launder a private text past the gate.
    """
    text = msg.text or ""
    if not text.strip():
        return None
    if scan_hard(text):
        return None
    ts = msg.timestamp.astimezone(timezone.utc).isoformat()
    masked, _ = mask_soft(text)
    return {
        "uuid": harvest._sha(f"imessage|{msg.handle}|{ts}|{text}"),
        "session": f"imessage-{msg.timestamp.date().isoformat()}",
        "ts": ts,
        "cwd": "imessage",
        "sha256": harvest._sha(masked),
        # Normal by default (the user's choice for mail/texts); the detector
        # still upgrades health/money/legal/relationship content on its own.
        "sensitive": is_sensitive(text),
        # The correspondent as context, never as a fact. It gives the extractor
        # someone to attribute a "we"/"you" to without the schema carrying a
        # field the Claude-Code records do not have.
        "before": f"[text the user sent to {msg.handle}]" if msg.handle else "",
        "text": masked,
        "after": "",
    }


def ingest(vault: Path, messages: Iterable[imessage.Message]) -> dict:
    """Spool new outgoing messages; return a one-line-able summary.

    Pure of the database on purpose: `messages` is any iterable of Message, so
    the whole watermark/dedup/redact/spool path is testable without a chat.db.
    `ingest_from_db` is the thin wrapper that supplies the real reader.
    """
    summary = {"seen": 0, "spooled": 0, "secrets_dropped": 0,
               "sensitive_flagged": 0, "duplicates": 0}
    ledger = harvest.load_ledger(vault)
    by_session: dict[str, list[dict]] = {}
    newest: datetime | None = None

    for msg in messages:
        if not msg.is_from_me:
            continue
        summary["seen"] += 1
        if newest is None or msg.timestamp > newest:
            newest = msg.timestamp
        rec = _record(msg)
        if rec is None:
            summary["secrets_dropped"] += 1
            continue
        if rec["uuid"] in ledger:
            summary["duplicates"] += 1
            continue
        ledger.add(rec["uuid"])
        by_session.setdefault(rec["session"], []).append(rec)
        if rec["sensitive"]:
            summary["sensitive_flagged"] += 1

    for session_id, records in by_session.items():
        written = harvest._append_evidence(vault, session_id, records)
        for rec in written:
            harvest._append_line(harvest.ledger_path(vault), rec["uuid"])
        summary["spooled"] += len(written)

    if newest is not None:
        save_watermark(vault, newest)
    return summary


def ingest_from_db(vault: Path, db_path: Path | None = None,
                   now: datetime | None = None) -> dict:
    """Read chat.db from the last watermark forward and spool what is new.

    COLD START IS FORWARD-ONLY. With no watermark, reading `since=None` would
    stream the whole history -- about 37,000 outgoing messages -- into the
    spool, and the sweep would then try to extract a fact from every one. That
    history was already mined in the one-time batch read, so the first run
    instead sets the watermark to now and spools nothing; only messages that
    arrive after this run are captured. A deliberate historical backfill is a
    separate, explicit act, not something a nightly job should do by surprise.
    """
    db_path = db_path or default_db()
    ok, reason = probe_access(db_path)
    if not ok:
        return {"error": reason}
    since = load_watermark(vault)
    if since is None:
        now = now or datetime.now(timezone.utc)
        save_watermark(vault, now)
        return {"initialized": now.isoformat(), "seen": 0, "spooled": 0,
                "secrets_dropped": 0, "sensitive_flagged": 0, "duplicates": 0}
    return ingest(vault, imessage.read_messages(db_path, since=since))
