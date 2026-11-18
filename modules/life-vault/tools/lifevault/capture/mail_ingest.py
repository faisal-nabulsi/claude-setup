"""Feed new mail into the same evidence spool the sweep drains, via the Google connector.

Every inbox in LIFE_VAULT_MAIL_ACCOUNTS is
read through the user's Google connector (the `google-workspace` MCP server; see
`google_connector`), which should have those accounts authorised READ-ONLY. The
messages drop into the same pipeline as Claude Code turns and iMessage: redact,
spool, let the nightly sweep ground and file them.

**Why not Mail.app any more.** Until 2026-10-09 this asked Mail.app for
`messages whose date received > cutoff` over AppleScript. The Gmail inbox holds
~49,000 messages, so that query walked all of them; at 02:40 it also had to
cold-launch Mail, and it timed out at 120 s on almost every run from mid
September. A timeout on the first account aborted the whole run, so the second
account was never read either, and because the cursor only moved when mail was saved,
each failure widened the next night's window (2 days -> 36). Gmail search
answers by index instead, and nothing here opens Mail.app or runs osascript.

**Mail is different from iMessage in who the source is, and that is on
purpose.** For texts the vault keeps only the user's OUTGOING messages, because
the extractor reads every line as their words and an incoming text is someone
else's news. Mail is the opposite: the facts live in the INCOMING mail -- a
registrar confirming enrolment, a grade, an appointment, a receipt -- written
by an institution ABOUT them. So this reads incoming mail (`-from:me`: not their
sent mail or drafts, and archived mail still counts), and every record carries
an `[email from <sender>, subject: <subject>]` context line so the extractor
attributes an institutional statement to its author rather than to the user. The
mechanical grounding gate still does the real work: a proper noun or number in
a proposed fact must appear in the mail's own text.

**Everything, per the user's choice -- no marketing filter.** `mail.py` has a
biography-vs-marketing classifier and it is deliberately NOT used here: they
asked for all of it, and the sweep's own no-signal path is what discards a
promo email, at the cost of one model call per message.

**The suppression list applies before anything is spooled.** A message that
matches a `pattern:` line in `_review/deletions.md` is dropped whole, the same
standing instruction `lv-forget` enforces on filed entries.

**The cursor is the last successful run, per account.** `_queue/mail-state-*`
records when the last run for that account finished cleanly; the next run asks
Gmail for mail after that time minus `OVERLAP`, and the ledger turns the
overlap into duplicates, never double spooling. No state file means a first
run, which looks back `FIRST_RUN_DAYS`. A run that fails for one account leaves
that account's cursor alone and still reads the other. A message the connector
lists but cannot read goes on a retry list and is tried again on the next
`MAX_RETRIES` runs.

**The vault keeps the facts, not the mail.** Gmail already keeps every email,
and the sweep files a fact from roughly one record in forty, so once the sweep
has read an email and found nothing (`no-signal`) its text is deleted after
`PRUNE_AFTER` (`prune`, run by lv-mail every night). Mail that produced a fact,
was held for review, failed, or has not been swept yet is kept. Per the user,
2026-10-09.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..atomic import write_atomic
from ..sources.receipts import ReceiptLog
from ..sources.redact import is_sensitive, mask_soft, scan_hard
from . import harvest
from .google_connector import ConnectorError, GoogleConnector

import os
#: Which inboxes to read. Set LIFE_VAULT_MAIL_ACCOUNTS="you@gmail.com,you@school.edu"
#: (comma-separated) in the launchd plist; each must be authorised in google-workspace.
ACCOUNTS = tuple(a.strip() for a in os.environ.get("LIFE_VAULT_MAIL_ACCOUNTS", "").split(",") if a.strip())
QUERY = "-from:me"
FIRST_RUN_DAYS = 2
#: Re-read this far behind the last successful run. Gmail's `after:` uses the
#: time Google received the message; a little slack covers clock skew and mail
#: that lands while a run is in progress. The ledger absorbs the repeats.
OVERLAP = timedelta(hours=1)
PAGE = 50            # the connector's maximum per search
WINDOW = timedelta(hours=6)  # first slice size; a day of personal mail fits in a few
MAX_SEARCHES = 200   # per account per run, a backstop only
MAX_RETRIES = 3
#: Gmail's quota is per user per MINUTE (403 rateLimitExceeded). Every search
#: also costs a metadata fetch per result, so a burst of reads can hit it; the
#: answer is to wait the minute out, a bounded number of times per run.
RATE_LIMIT_PAUSE = 65.0
MAX_RATE_LIMIT_PAUSES = 10
_RATE_LIMITED = re.compile(r"rateLimitExceeded|Quota exceeded|\b429\b", re.IGNORECASE)

#: Swept mail with nothing in it is deleted this long after it was sent.
PRUNE_AFTER = timedelta(days=14)
_PRUNABLE = "no-signal"

_PATTERN_LINE = re.compile(r"^pattern:\s*(.+?)\s*$", re.MULTILINE)  # as in lv-forget

SUMMARY_FIELDS = ("seen", "spooled", "secrets_dropped", "sensitive_flagged",
                  "duplicates", "suppressed")


# --------------------------------------------------------------------------
# Per-account state
# --------------------------------------------------------------------------

def state_path(vault: Path, account: str) -> Path:
    # Under _queue/ (gitignored runtime state): a mutable cursor is not a fact
    # and nothing under sources/ may be gitignored. A lost state file costs one
    # FIRST_RUN_DAYS look-back, which the ledger dedupes.
    safe = "".join(c if c.isalnum() or c in "-_.@" else "_" for c in account)
    return vault / "_queue" / f"mail-state-{safe}.json"


def load_state(vault: Path, account: str) -> dict:
    """{"last_success": datetime | None, "retry": {message_id: attempts}}."""
    try:
        raw = json.loads(state_path(vault, account).read_text(encoding="utf-8"))
        last = datetime.fromisoformat(raw["last_success"])
        retry = {str(k): int(v) for k, v in (raw.get("retry") or {}).items()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {"last_success": None, "retry": {}}
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return {"last_success": last, "retry": retry}


def save_state(vault: Path, account: str, last_success: datetime, retry: dict) -> None:
    path = state_path(vault, account)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"last_success": last_success.isoformat(), "retry": retry},
                              sort_keys=True), encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------
# Suppression
# --------------------------------------------------------------------------

def load_suppressions(vault: Path) -> list[re.Pattern]:
    """The `pattern:` regexes from _review/deletions.md, case-insensitive.

    A broken regex raises ValueError: capture must refuse rather than spool
    material the list was meant to keep out."""
    path = vault / "_review" / "deletions.md"
    if not path.exists():
        return []
    out = []
    for raw in _PATTERN_LINE.findall(path.read_text(encoding="utf-8")):
        try:
            out.append(re.compile(raw, re.IGNORECASE))
        except re.error as exc:
            raise ValueError(f"bad pattern in {path}: {raw!r} ({exc})") from None
    return out


def _suppressed(m: dict, patterns: list[re.Pattern]) -> bool:
    haystack = "\n".join((m.get("subject", ""), m.get("sender", ""), m.get("body", "")))
    return any(p.search(haystack) for p in patterns)


# --------------------------------------------------------------------------
# Spooling
# --------------------------------------------------------------------------

def _record(m: dict) -> dict | None:
    subject, sender, body = m.get("subject", ""), m.get("sender", ""), m.get("body", "")
    text = body.strip() or subject.strip()
    if not text:
        return None
    if scan_hard(subject) or scan_hard(body):
        return None
    ts = m["date"].astimezone(timezone.utc).isoformat()
    masked, _ = mask_soft(text)
    account = m.get("account", "")
    # Gmail's message id is the stable identity; the header tuple is the
    # fallback for a message dict that has none.
    identity = m.get("id") or f"{ts}|{sender}|{subject}"
    return {
        "uuid": harvest._sha(f"mail|{account}|{identity}"),
        "session": f"mail-{m['date'].astimezone(timezone.utc).date().isoformat()}",
        "ts": ts,
        "cwd": "mail",
        "sha256": harvest._sha(masked),
        "sensitive": is_sensitive(f"{subject}\n{body}"),
        "before": f"[email from {sender}, subject: {subject}]",
        "text": masked,
        "after": "",
    }


def ingest(vault: Path, messages: Iterable[dict], account: str,
           patterns: list[re.Pattern] | None = None) -> dict:
    """Spool messages for one account; return a summary. Connector-free and
    pure, so the whole path is testable on synthetic mail. Every message passed
    in counts as seen; the ledger, not a date, decides what is new."""
    summary = dict.fromkeys(SUMMARY_FIELDS, 0)
    ledger = harvest.load_ledger(vault)
    by_session: dict[str, list[dict]] = {}

    for m in messages:
        summary["seen"] += 1
        if patterns and _suppressed(m, patterns):
            summary["suppressed"] += 1
            continue
        rec = _record(m)
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
    return summary


# --------------------------------------------------------------------------
# Reading through the connector
# --------------------------------------------------------------------------

class _Patience:
    """Waits out Gmail's per-minute quota, at most `pauses` times per run."""

    def __init__(self, pauses: int = MAX_RATE_LIMIT_PAUSES):
        self.left = pauses

    def call(self, fn, *args):
        while True:
            try:
                return fn(*args)
            except ConnectorError as exc:
                if self.left <= 0 or not _RATE_LIMITED.search(str(exc)):
                    raise
                self.left -= 1
                time.sleep(RATE_LIMIT_PAUSE)


def list_new(conn, account: str, since: datetime, now: datetime,
             patience: _Patience | None = None) -> tuple[list[str], list[str], bool]:
    """Ids of incoming messages Gmail received after `since`.

    Returns (ids, ids the server listed but could not fetch, complete). Search
    returns at most PAGE results and has no page token, so a full answer means
    "there may be more": that window is split in half and each half is
    searched again, down to one minute. The range starts as WINDOW-sized
    slices (at most ~40 of them) because a full answer is the expensive one:
    the server fetches metadata for every result it returns. Windows are bounded by Gmail's own
    received time (`after:`/`before:` in epoch seconds), never by a sender's
    Date header, so a wrong clock cannot open a gap. The halves overlap by one
    second because the boundary's inclusivity is not documented; the id set
    removes the repeat. `complete` is False only if MAX_SEARCHES ran out first
    or more than PAGE messages arrived within one minute."""
    ids: list[str] = []
    unreadable: list[str] = []
    seen: set[str] = set()
    patience = patience or _Patience(0)
    lo0, hi0 = int(since.timestamp()), int(now.timestamp()) + 3600
    step = max(int(WINDOW.total_seconds()), math.ceil((hi0 - lo0) / 40))
    windows = [(t, min(t + step + 1, hi0)) for t in range(lo0, hi0, step)]
    complete, searches = True, 0
    while windows:
        if searches >= MAX_SEARCHES:
            return ids, unreadable, False
        lo, hi = windows.pop()
        searches += 1
        got, bad = patience.call(conn.search, account, f"{QUERY} after:{lo} before:{hi}", PAGE)
        if len(got) + len(bad) >= PAGE:
            if hi - lo > 60:
                mid = (lo + hi) // 2
                windows += [(lo, mid + 1), (mid, hi)]
                continue
            complete = False
        for msg_id in got:
            if msg_id not in seen:
                seen.add(msg_id)
                ids.append(msg_id)
        for msg_id in bad:
            if msg_id not in seen:
                seen.add(msg_id)
                unreadable.append(msg_id)
    return ids, unreadable, complete


def fetch_new(conn, account: str, since: datetime, retry: Iterable[str] = (),
              now: datetime | None = None,
              patience: _Patience | None = None) -> tuple[list[dict], list[str], bool, dict]:
    """Read every message `list_new` finds, plus the `retry` ids first.

    Returns (messages, ids that could not be read, complete, {reason: count})
    -- the reasons so a failed read is never just a number in the log."""
    now = now or datetime.now(timezone.utc)
    patience = patience or _Patience(0)
    ids, failed, complete = list_new(conn, account, since, now, patience)
    reasons: dict[str, int] = {}
    if failed:
        reasons["listed but not fetchable"] = len(failed)
    messages = []
    for msg_id in [*[r for r in retry if r not in ids], *ids]:
        try:
            m = patience.call(conn.read, account, msg_id)
        except ConnectorError as exc:
            if not getattr(conn, "alive", True):
                raise                  # the server died: fail the account, keep its cursor
            failed.append(msg_id)
            reason = str(exc)[:80]
            reasons[reason] = reasons.get(reason, 0) + 1
            continue
        m["id"], m["account"] = msg_id, account
        if m["date"] is None:      # no usable Date header: file it under today
            m["date"] = now
        messages.append(m)
    return messages, failed, complete, reasons


def ingest_all(vault: Path, now: datetime | None = None, connector=GoogleConnector) -> dict:
    """Both accounts, each from its last successful run forward.

    Returns totals over SUMMARY_FIELDS plus `accounts` and `errors`, and a
    `failures` list of one-line reasons. {"error": ...} means nothing ran."""
    now = now or datetime.now(timezone.utc)
    try:
        patterns = load_suppressions(vault)
    except ValueError as exc:
        return {"error": str(exc)}
    totals = dict.fromkeys(("accounts", *SUMMARY_FIELDS, "errors"), 0)
    totals["failures"] = []
    try:
        conn = connector()
    except ConnectorError as exc:
        return {"error": f"Google connector unavailable: {exc}"}
    patience = _Patience()                    # one budget for the whole run
    with conn:
        for account in ACCOUNTS:
            totals["accounts"] += 1
            state = load_state(vault, account)
            since = (state["last_success"] - OVERLAP if state["last_success"]
                     else now - timedelta(days=FIRST_RUN_DAYS))
            try:
                messages, failed, complete, reasons = fetch_new(
                    conn, account, since, retry=state["retry"], now=now, patience=patience)
            except Exception as exc:  # noqa: BLE001 -- unattended: one account must not sink the other
                totals["errors"] += 1
                totals["failures"].append(f"{account}: {type(exc).__name__}: {exc}")
                continue                        # cursor stays; next run re-reads
            summary = ingest(vault, messages, account, patterns)
            for k in SUMMARY_FIELDS:
                totals[k] += summary[k]
            retry = {}
            for mid in failed:
                attempts = state["retry"].get(mid, 0) + 1
                if attempts < MAX_RETRIES:
                    retry[mid] = attempts
                else:
                    totals["failures"].append(f"{account}: gave up on message {mid} "
                                              f"after {attempts} failed reads")
            totals["errors"] += len(failed)
            for reason, count in sorted(reasons.items()):
                totals["failures"].append(f"{account}: could not read {count}: {reason}")
            if not complete:
                totals["errors"] += 1
                totals["failures"].append(f"{account}: listing stopped short "
                                          f"({MAX_SEARCHES} searches); some mail was not read")
            save_state(vault, account, now, retry)
    return totals


# --------------------------------------------------------------------------
# Pruning: keep the facts, not the mail
# --------------------------------------------------------------------------

def prune(vault: Path, now: datetime | None = None) -> int:
    """Delete mail records the sweep marked `no-signal`, once older than
    PRUNE_AFTER. Returns how many records went.

    Only `mail-*.jsonl` is touched; texts and Claude Code turns are not. Like
    `harvest.scrub`, this is a documented exception to the spool being
    append-only, so it rewrites atomically and logs every file it changes to
    `pruned.tsv`: an unlogged deletion is indistinguishable from corruption.
    Each file keeps its mtime, because the health check dates the last harvest
    by the spool's newest mtime and a nightly rewrite must not make a dead
    harvester look alive. The uuids stay in the ledger, so a pruned email is
    never captured again. A corrupt receipts.tsv raises ReceiptError and
    nothing is deleted."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - PRUNE_AFTER
    source = harvest.source_dir(vault)
    status = {r.item_id: r.status for r in ReceiptLog.load(source / "receipts.tsv").receipts}
    total = 0
    for path in sorted((source / "evidence").glob("mail-*.jsonl")):
        kept, removed = [], 0
        for line in path.read_text(encoding="utf-8").split("\n"):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                sent = datetime.fromisoformat(record["ts"])
            except (ValueError, KeyError, TypeError):
                kept.append(line)
                continue
            if sent.tzinfo is None:
                sent = sent.replace(tzinfo=timezone.utc)
            if status.get(record.get("uuid")) == _PRUNABLE and sent < cutoff:
                removed += 1
            else:
                kept.append(line)
        if not removed:
            continue
        before = path.stat()
        if kept:
            write_atomic(path, lambda fh: fh.write("".join(l + "\n" for l in kept)))
            os.utime(path, (before.st_atime, before.st_mtime))
        else:
            path.unlink()
        harvest._append_line(source / "pruned.tsv",
                             f"{now.astimezone():%Y-%m-%d %H:%M}\t{path.stem}\t{removed}\t{_PRUNABLE}")
        total += removed
    return total
