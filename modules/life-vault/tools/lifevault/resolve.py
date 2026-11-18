"""Settle a true conflict — the decision the machinery is not entitled to make.

`conflict.classify` returns "conflict" only when it has genuinely run out of
grounds: same subject, same date, same confidence tier, different claims. There
is nothing left in the data to prefer one over the other. Recency is tied and
confidence is tied, which for a `conf/stated` pair means the user said both
things on the same day.

The vault's response is to mark both `conflicted` and neither `current`. That is
the honest state, and it is also a hole: `lv find status/current` about that
subject then returns nothing, so a vault that was told the truth twice answers
neither time. Every correction the user makes by hand lands here, because a
correction is two stated claims one after the other.

This module is the human's side of that. It rewrites status lines and nothing
else, which is the whole of what spec §3 permits an existing entry to change:
the losing prose stays on disk, verbatim, forever. The edit is surgical rather
than a re-render for the same reason -- a round trip through `render_entry`
would be free to renormalize a quote or reorder tags, and "we only changed the
status" would quietly stop being true.
"""

from __future__ import annotations

from pathlib import Path

from .atomic import write_atomic
from .entry import DELIM, HEADER_RE, META_KEYS, Entry, parse_entries
from .subjects import Subjects
from .vaultlock import vault_lock

CONFLICTED = "conflicted"


class NoSuchConflict(Exception):
    """Nothing about this subject is awaiting a decision."""


def _resolver(vault: Path):
    """Map a stored subject spelling onto its canonical one, as store does.

    Comparing raw strings would make a conflict filed under a drifted spelling
    invisible to the command meant to settle it -- and an unsettleable conflict
    is permanently un-`current`.
    """
    subjects = Subjects.load(vault / "_index" / "subjects.tsv")
    return lambda s: subjects.resolve(s) or s


def _entries(log: Path) -> list[Entry]:
    if not log.exists():
        raise NoSuchConflict(f"no log at {log}")
    return parse_entries(log.read_text(encoding="utf-8"))


def _contested_positions(entries: list[Entry], vault: Path, subject: str) -> list[int]:
    """Indices of every live claim about `subject`, if any of them is disputed.

    "Live" is conflicted OR current, not conflicted alone. A vault written
    before `store` refused to let a newcomer settle a dispute can hold both at
    once for one subject; resolving only the conflicted ones there would leave
    two entries marked `current` and the vault answering a settled question
    twice. Superseded and blocked entries are frozen history and stay out.

    Empty when nothing is disputed: a subject with one ordinary current entry
    has no decision to make, and offering one would invite a user to "resolve"
    a fact that was never in question.
    """
    resolve_subject = _resolver(vault)
    target = resolve_subject(subject)
    live = [
        i for i, e in enumerate(entries)
        if e.status in (CONFLICTED, "current") and resolve_subject(e.subject) == target
    ]
    if not any(entries[i].status == CONFLICTED for i in live):
        return []
    return live


def contested(log: str | Path, vault: str | Path, subject: str) -> list[Entry]:
    """Every claim about `subject` awaiting a decision, in file order.

    File order is the index `resolve` takes, so this is the listing a user reads
    before choosing. It is stable as long as nothing is appended in between --
    which is why `resolve` re-derives the list under the lock rather than
    trusting an index computed outside it.
    """
    entries = _entries(Path(log))
    return [entries[i] for i in _contested_positions(entries, Path(vault), subject)]


def _status_line_numbers(text: str, wanted: set[int]) -> dict[int, int]:
    """Line number of the `status:` line for each wanted entry, by entry index.

    Walks the file exactly as `parse_entries` and `_parse_chunk` do: blocks open
    at a bare delimiter line, metadata runs from the header to the first blank
    line. Scanning for any line starting with "status:" instead would also match
    body prose, and rewriting that would corrupt a fact while reporting success.
    """
    found: dict[int, int] = {}
    index = -1
    in_meta = False
    seen_header = False

    for number, line in enumerate(text.splitlines()):
        if line == DELIM:
            index += 1
            in_meta, seen_header = False, False
            continue
        if index < 0:
            continue
        if not seen_header:
            if HEADER_RE.match(line):
                seen_header, in_meta = True, True
            continue
        if not in_meta:
            continue
        if not line.strip():
            in_meta = False
            continue
        key = line.partition(":")[0].strip()
        if key == "status" and index in wanted:
            found[index] = number
        elif key not in META_KEYS:
            in_meta = False

    return found


def resolve(log: str | Path, vault: str | Path, subject: str, keep: int) -> Entry:
    """Make the `keep`-th conflicted entry current and supersede its rivals.

    `keep` indexes the list `conflicted()` returns. It is re-derived here under
    the lock: between the user reading the list and choosing from it, a capture
    run may have appended, and an index resolved against the stale list would
    silently make the wrong claim the live one.

    Refuses a negative index. Python would happily read `-1` as "the last one",
    so a typo would pick a side by accident -- which is the exact failure this
    module exists to prevent.

    Returns the entry that is now current.
    """
    log, vault = Path(log), Path(vault)

    with vault_lock(vault):
        text = log.read_text(encoding="utf-8") if log.exists() else None
        if text is None:
            raise NoSuchConflict(f"no log at {log}")

        entries = parse_entries(text)
        positions = _contested_positions(entries, vault, subject)

        if not positions:
            raise NoSuchConflict(
                f"nothing about {subject!r} is conflicted in {log}"
            )
        if keep < 0 or keep >= len(positions):
            raise IndexError(
                f"keep={keep} is out of range; {len(positions)} contested "
                f"{'claim' if len(positions) == 1 else 'claims'} about {subject!r}"
            )

        winner = positions[keep]
        # `superseded-by <date>` is the vocabulary the rest of the vault uses --
        # `index.py` keys `status/superseded` off that exact prefix, so a bare
        # "superseded" here would file the losing side under a status nothing
        # queries, and it would vanish from the reverse index rather than being
        # findable as the history it is.
        new_status = {i: f"superseded-by {entries[winner].date}" for i in positions}
        new_status[winner] = "current"

        lines = text.splitlines()
        for index, number in _status_line_numbers(text, set(positions)).items():
            lines[number] = f"status: {new_status[index]}"

        rewritten = "\n".join(lines)
        if text.endswith("\n"):
            rewritten += "\n"
        write_atomic(log, lambda fh: fh.write(rewritten))

        return entries[winner]
