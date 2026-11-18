# tools/lifevault/capture/harvest.py
"""Transcript -> durable, redacted, git-tracked evidence. No model involved.

This stage is deliberately the only one that can run today: `claude -p` returns
401 on this machine, and Claude Code deletes transcripts after roughly three
weeks. Keeping harvest model-free converts that GC from a data-loss risk into a
latency budget, which is the difference between "extraction was down for a
month" and "a month of the user's life is gone".

The plan this module implements was written against two modules that never
landed as named: a `lifevault.filekit` (Task 1 reconciled pre-existing work
into `lifevault.vaultlock` instead, which has no `append_line` or
`write_text_atomic`) and a top-level `lifevault.redact` (Task 4 landed at
`lifevault.sources.redact`, later extended with the hard/soft tier this module
needs). This file is written against what actually exists:
`lifevault.atomic.write_atomic` for the rare whole-file rewrite (the hook
heartbeat marker, `scrub`'s removal), a local `_append_line` for the routine
append path, and `lifevault.sources.redact.{scan_hard,mask_soft,is_sensitive}`.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from ..atomic import write_atomic
from ..sources.redact import is_sensitive, mask_soft, scan_hard
from . import transcript as tx

SOURCE = "claude-code"
# Transcript writes lag real time by 3-12 minutes (measured twice, different
# sessions). A session that just ended is left for the next hourly run rather
# than harvested half-written.
SETTLE_SECONDS = 1800


def source_dir(vault: str | Path) -> Path:
    return Path(vault) / "sources" / SOURCE


def evidence_path(vault: str | Path, session_id: str) -> Path:
    return source_dir(vault) / "evidence" / f"{session_id}.jsonl"


def ledger_path(vault: str | Path) -> Path:
    return source_dir(vault) / "harvested-uuids.txt"


def load_ledger(vault: str | Path) -> set[str]:
    """Loaded as a SET. uuid is not unique in-file or across forks."""
    path = ledger_path(vault)
    if not path.exists():
        return set()
    return {l.strip() for l in path.read_text(encoding="utf-8").split("\n") if l.strip()}


def exclude_paths(vault: str | Path) -> tuple[Path, Path]:
    """(tracked baseline, local overlay). The baseline is authoritative."""
    vault = Path(vault)
    return (source_dir(vault) / "exclude.default.txt",
            vault / "_queue" / "exclude.txt")


def _read_prefixes(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [l.strip() for l in path.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.startswith("#")]


def sweep_sandbox(vault: str | Path) -> str:
    """Where lv-sweep runs its extraction child. COMPUTED, never typed.

    lv-sweep.sandbox() derives this from vault_root(), which honours
    LIFE_VAULT, so storing it as a literal line in a data file meant Layer 4
    silently stopped working the moment the vault was moved or renamed — with
    no error anywhere, and precisely in the configuration where Task 0 Q6 says
    the exclusion becomes load-bearing rather than redundant.
    """
    return str(Path(vault) / "_sweep")


def load_excludes(vault: str | Path) -> list[str] | None:
    """Union of the computed sandbox, the git-TRACKED baseline, and the overlay.

    Returns None when neither file exists, and the caller then refuses to
    harvest at all. Two reasons this fails closed rather than open:

    * `_queue/` is gitignored (verified: .gitignore line 3), and `git clean -xdf`
      in ~/life removes the whole queue — which happens, because the user develops
      there. Reading the exclusion list only from `_queue/` meant the only
      durable copy of it was never consulted, so restoring the vault from its
      one backup produced a vault with NO exclusions.
    * With no exclusions, the very next harvest ingests all eleven agent
      scratchpad project directories and files "Reply with exactly the word OK"
      as something the user said, permanently, in an append-only store.

    An empty file is a deliberate "exclude nothing" and is honoured; a MISSING
    pair is an accident and is refused. The sweep's own sandbox is unioned in
    unconditionally and is not "exclude nothing"'s business: it is a structural
    property of where this vault is, so it follows the vault rather than a
    string somebody typed into a file once.
    """
    tracked, overlay = exclude_paths(vault)
    if not tracked.exists() and not overlay.exists():
        return None
    seen: list[str] = [sweep_sandbox(vault)]
    for prefix in _read_prefixes(tracked) + _read_prefixes(overlay):
        if prefix not in seen:
            seen.append(prefix)
    return seen


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _excluded(cwd: str, project_slug: str, excludes: list[str]) -> bool:
    for prefix in excludes:
        if cwd.startswith(prefix):
            return True
        # The cwd -> slug mapping is re.sub(r"[^a-zA-Z0-9]", "-", cwd) and is
        # lossy, so it is never reversed. Comparing the forward-transformed
        # prefix against the slug is safe and catches transcripts whose records
        # carry no cwd at all.
        transformed = "".join(c if c.isalnum() else "-" for c in prefix)
        if project_slug.startswith(transformed):
            return True
    return False


def _append_line(path: Path, line: str) -> None:
    """Append one line, creating the parent directory if needed.

    Stands in for the plan's `filekit.append_line`, which was never built.
    Not atomic in the fsync+rename sense: a crash mid-write can leave a torn
    trailing line. That failure mode is already tolerated by every reader that
    walks one of these files line-by-line (`load_ledger`, `_append_evidence`,
    `load_evidence` all skip a line that fails `json.loads`/is blank), the same
    way `capture.transcript.iter_records` tolerates a Claude Code transcript
    being written to concurrently. A worst case duplicate line is caught by the
    set/dict dedup every caller already does.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def harvest(
    vault: str | Path,
    projects: str | Path | None = None,
    now: float | None = None,
) -> dict:
    """Read every settled transcript, redact, spool, and clear its pointer.

    Returns a summary dict for the health file. Never raises on a single bad
    transcript: a session that cannot be read is counted and skipped, because
    one unreadable file must not stop the other seventy-one.
    """
    vault = Path(vault)
    now = time.time() if now is None else now
    pending = vault / "_queue" / "pending"

    summary = {
        "sessions_seen": 0, "sessions_harvested": 0, "turns_written": 0,
        "secrets_dropped": 0, "soft_masked": 0, "unreadable": [],
        "missing": [], "missing_first": "", "missing_last": "",
        "unknown_origin_kinds": [], "excluded": 0,
        "user_records_seen": 0, "human_turns_seen": 0, "origin_absent": 0,
    }
    unknown: set[str] = set()
    # Every pointer mtime this run observed, so the hook's heartbeat outlives
    # the pointers that carry it. See _stamp_hook_heartbeat.
    seen_mtimes: list[float] = []
    # mtimes of pointers whose transcript is already gone, so lv-harvest can
    # name the window in _health/gaps.tsv instead of the loss being a list
    # buried in status.json.
    missing_mtimes: list[float] = []

    # The kill switch has to stop the WHOLE pipeline, not just the pointers.
    # harvest reads the transcripts directly and never consults the queue, so
    # without a check here, switching capture off would leave the user believing
    # it was off while the hourly job kept permanently spooling everything they
    # typed.
    #
    # But OFF is now an explicit marker file, not an absent directory, and that
    # distinction is the whole point. `_queue/` is gitignored (.gitignore line
    # 3), the user develops inside ~/life, and `git clean -xdf` removes the whole
    # queue — so under the old rule a single stray clean, an over-broad rm, or a
    # restore from the one backup that exists switched capture off PERMANENTLY
    # and health printed `capture is DISABLED` as an ordinary line rather than a
    # problem. BANNER.md stayed empty, MEMORY.md kept saying Capture OK, and
    # nothing ever recovered. An accident and a decision must not have the same
    # on-disk representation.
    if (vault / "_queue" / "CAPTURE-OFF").exists():
        return {**summary, "disabled": True}
    if not pending.is_dir():
        pending.mkdir(parents=True, exist_ok=True)
        try:
            pending.chmod(0o700)
        except OSError:
            pass
        summary["recreated_queue"] = True

    excludes = load_excludes(vault)
    if excludes is None:
        # Fails CLOSED. See load_excludes for why an absent list is an accident
        # rather than an instruction, and Task 11 raises it as a problem.
        return {**summary, "error": "no exclusion list"}

    ledger = load_ledger(vault)

    for path in tx.depth1_transcripts(projects):
        session_id = path.stem
        summary["sessions_seen"] += 1
        try:
            if now - path.stat().st_mtime < SETTLE_SECONDS:
                continue
            turns = tx.human_turns(path)
            stats = tx.origin_stats(path)
        except OSError:
            summary["unreadable"].append(session_id)
            continue

        # Counted for EVERY settled transcript, including excluded ones, because
        # this is the signal that tells a schema change apart from a quiet week.
        summary["user_records_seen"] += stats["user_records"]
        summary["human_turns_seen"] += stats["human_turns"]
        summary["origin_absent"] += stats["origin_absent"]
        unknown |= stats["unknown_kinds"]

        slug = path.parent.name
        # Session-level fast path: skip the file entirely when the whole session
        # is excluded. The per-turn check below is the one that is load-bearing.
        if turns and all(_excluded(t.cwd, slug, excludes) for t in turns):
            summary["excluded"] += 1
            _clear_pointer(pending, session_id, seen_mtimes)
            continue

        context = tx.adjacent_context(path) if turns else {}
        fresh: list[dict] = []
        seen_here: set[str] = set()

        for turn in turns:
            # Exclusion is decided PER TURN, not once from the first turn's cwd.
            # Claude Code records cwd on every record, and a session that starts
            # in an ordinary directory and later cd's into an agent scratchpad
            # or a work repository would otherwise be harvested in full — a
            # fail-OPEN hole in the one control that keeps those out of the
            # vault. The mirror case, starting inside an excluded directory and
            # moving out, silently dropped real turns.
            if _excluded(turn.cwd or "", slug, excludes):
                summary["excluded"] += 1
                continue
            if turn.uuid in ledger or turn.uuid in seen_here:
                continue
            hard = scan_hard(turn.text)
            if hard:
                # Drop the whole message. Masking leaves enough context to
                # re-derive the value, and spec 12.6 says a missed fact is
                # recoverable while a filed credential is not.
                summary["secrets_dropped"] += 1
                seen_here.add(turn.uuid)
                continue
            text, soft = mask_soft(turn.text)
            if soft:
                summary["soft_masked"] += 1
            before, after = context.get(turn.uuid, ("", ""))
            before, _ = mask_soft(before) if before and not scan_hard(before) else ("", False)
            after, _ = mask_soft(after) if after and not scan_hard(after) else ("", False)
            fresh.append({
                "uuid": turn.uuid,
                "session": session_id,
                "ts": turn.ts,
                "cwd": turn.cwd,
                "sha256": _sha(text),
                # Set by NON-MODEL code, from the source text, before any model
                # ever sees it. gate.py's sensitivity check reads the candidate's
                # title and body, which the extractor writes — so a paraphrase
                # ("mentioned a health update") could launder a private fact past
                # the cue regex. This flag is computed from the words the user
                # actually typed, and the gate can only ever add to it.
                "sensitive": bool(soft) or is_sensitive(turn.text),
                "before": before,
                "text": text,
                "after": after,
            })
            seen_here.add(turn.uuid)

        if fresh:
            written = _append_evidence(vault, session_id, fresh)
            for record in written:
                _append_line(ledger_path(vault), record["uuid"])
                ledger.add(record["uuid"])
            summary["turns_written"] += len(written)
            if written:
                summary["sessions_harvested"] += 1

        # Cleared only after the spool has landed on disk.
        _clear_pointer(pending, session_id, seen_mtimes)

    # A pointer with no transcript behind it is expected, not an error:
    # retention deletes anything older than about three weeks, so a queued
    # pointer routinely outlives its source.
    for pointer in sorted(pending.iterdir()):
        if pointer.name.startswith("."):
            continue
        if tx.transcript_for(pointer.name, projects) is None:
            summary["missing"].append(pointer.name)
            try:
                missing_mtimes.append(pointer.stat().st_mtime)
            except OSError:
                pass
            _clear_pointer(pending, pointer.name, seen_mtimes)

    # The window that was lost, in dates, so lv-harvest can record a real gap.
    # `missing` is a list of session ids buried in status.json and states
    # nothing about WHEN; gaps.tsv is the file that says "this vault knows it
    # is not complete between these two dates".
    if missing_mtimes:
        summary["missing_first"] = time.strftime(
            "%Y-%m-%d", time.localtime(min(missing_mtimes)))
        summary["missing_last"] = time.strftime(
            "%Y-%m-%d", time.localtime(max(missing_mtimes)))

    _stamp_hook_heartbeat(vault, seen_mtimes)
    summary["unknown_origin_kinds"] = sorted(unknown)
    return summary


def _clear_pointer(pending: Path, session_id: str, seen_mtimes: list[float]) -> None:
    path = pending / session_id
    try:
        seen_mtimes.append(path.stat().st_mtime)
    except OSError:
        pass
    try:
        path.unlink()
    except OSError:
        pass


def _stamp_hook_heartbeat(vault: Path, seen_mtimes: list[float]) -> None:
    """Make the hook's heartbeat survive the harvest that consumes it.

    The pointer mtime is the hook's only proof of life, and this function
    deletes every pointer it processes — so within an hour of any ordinary
    session the live queue is empty and "no pointer" would be indistinguishable
    from "the hook is dead". Health then raised `the Stop hook wrote no pointer`
    on almost every hourly run, mirrored it into MEMORY.md, and trained the one
    alarm that would catch a genuinely dead hook to be ignored.

    So the newest mtime seen this run is stamped onto a durable file before the
    pointers go, and health reads max(live pointers, this file).
    """
    if not seen_mtimes:
        return
    newest = max(seen_mtimes)
    path = vault / "_health" / "hook.seen"
    path.parent.mkdir(parents=True, exist_ok=True)
    current: float | None = None
    if path.exists():
        try:
            current = path.stat().st_mtime
        except OSError:
            current = None
    else:
        write_atomic(
            path,
            lambda fh: fh.write("the newest Stop-hook pointer this harvest observed\n"),
        )
    # Monotone. A stale pointer left over from three weeks ago must never drag
    # the heartbeat backwards and manufacture a "the hook is dead" alarm.
    if current is None or newest > current:
        os.utime(path, (newest, newest))


def _append_evidence(vault: Path, session_id: str, records: list[dict]) -> list[dict]:
    """Append new records to the spool. Returns the records actually appended.

    The plan for this function called for reading the whole file and rewriting
    it atomically on every call. That does not scale: the evidence spool is
    meant to grow for years, and copying the whole thing to add one line would
    make harvest cost grow with total history rather than with what just
    happened. So this only ever opens the file in append mode.

    It still reads the file first, though, and self-deduplicates against its
    own contents rather than trusting the ledger: evidence lands first, then
    uuids are appended to the ledger one line at a time (see `harvest`), and
    those two writes are not atomic together. A crash, a kill, or a full disk
    between them leaves spooled records whose uuids the ledger never learned,
    and the next run would otherwise re-append the identical records —
    `load_evidence` would then return both copies, biasing extraction toward
    deciding the turn is important, and the receipt's uuid list would carry
    duplicates.
    """
    path = evidence_path(vault, session_id)
    already: set[str] = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").split("\n"):
            if not line.strip():
                continue
            try:
                already.add(json.loads(line).get("uuid", ""))
            except ValueError:
                continue
    fresh = [r for r in records if r["uuid"] not in already]
    if not fresh:
        return []
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for record in fresh:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return fresh


def load_evidence(vault: str | Path, session_id: str) -> list[dict]:
    path = evidence_path(vault, session_id)
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").split("\n"):
        if line.strip():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def scrub(vault: str | Path, session_id: str, uuid: str) -> int:
    """Remove one record from the evidence spool. Returns how many were removed.

    The spool is append-only by convention, not by law, and this is the one
    documented exception: a credential that defeated the hard-secret scan must
    be removable. Unlike the routine append path, removing an arbitrary line
    genuinely needs a whole-file rewrite, so this is the one place in the
    module that still uses `atomic.write_atomic`. The removal itself is
    logged — an unlogged deletion from an append-only store is
    indistinguishable from corruption.
    """
    vault = Path(vault)
    path = evidence_path(vault, session_id)
    if not path.exists():
        return 0
    kept, removed = [], 0
    for line in path.read_text(encoding="utf-8").split("\n"):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            kept.append(line)
            continue
        if record.get("uuid") == uuid:
            removed += 1
            continue
        kept.append(line)
    if not removed:
        return 0
    write_atomic(path, lambda fh: fh.write("".join(l + "\n" for l in kept)))
    _append_line(source_dir(vault) / "scrubbed.tsv",
                 f"{time.strftime('%Y-%m-%d %H:%M')}\t{session_id}\t{uuid}")
    # The uuid stays in the ledger on purpose: it must never be re-harvested
    # from a transcript that has not been deleted yet.
    return removed
