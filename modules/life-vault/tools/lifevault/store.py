from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import IO

from .atomic import sweep_orphan_temps, write_atomic
from .conflict import _rank, classify
from .entry import Entry, parse_entries, render_entry
from .subjects import Subjects, normalize
from .tags import load_vocab, validate_tags, SENS_NORMAL, SENS_PRIVATE
from .vaultlock import vault_lock


def _render_all(fh: IO[str], entries: list[Entry]) -> None:
    """Stream every entry into the open staging file.

    Rendering inside the write, rather than building one string first, means a
    single unrenderable entry raises part way through -- which is precisely the
    case write_atomic exists to make survivable, and the one the tests exercise.
    """
    for i, e in enumerate(entries):
        if i:
            fh.write("\n")
        fh.write(render_entry(e))


def _write_all(log: Path, entries: list[Entry]) -> None:
    """Rewrite `log`: a full copy beside it, fsynced, then renamed over it.

    This is a whole-history rewrite -- every fact ever recorded about the entity
    goes through here on every supersession -- so a torn write loses years of
    life that no re-run can reconstruct. The old truncate-then-write left the
    file empty for the whole duration of the write; anything that interrupted it
    (ENOSPC on a disk documented as chronically full, a killed 3am job, a power
    cut) destroyed the file.

    The recipe itself lives in `lifevault.atomic`, which `entities.save` and
    `subjects.save` also use. It was hand-copied here once and the two copies
    had already diverged in the cleanup path -- the copy swallowed only
    FileNotFoundError, so a cleanup failure replaced the real ENOSPC the caller
    needed to see. One implementation is the fix for that class of bug.

    write_atomic deliberately does not create the parent directory, so creating
    the entity directory stays here, where it is a known part of filing a new
    entity rather than a path that might be a typo.

    Callers must hold the vault lock (see `lifevault.vaultlock`): the sweep
    below removes staging files for this log, which is only safe when no other
    writer can be part way through one.
    """
    entries.sort(key=lambda e: e.date, reverse=True)
    log.parent.mkdir(parents=True, exist_ok=True)
    sweep_orphan_temps(log)
    write_atomic(log, lambda fh: _render_all(fh, entries))


def _canonical_subject(vault: Path, subject: str) -> str:
    """Resolve `subject` through the vault's one subject namespace.

    Without this, `conflict.classify` compares raw strings and `allergy` and
    `allergies` are two unrelated facts: neither supersedes the other, both stay
    `status: current`, and §6's guarantee that a guess can never override a
    stated fact stops applying because the two claims never meet. Reproduced on
    this machine with "No known allergies" at conf/stated and "Allergic to
    penicillin" at conf/derived.

    A subject with no alphanumeric content is left exactly as it was. This layer
    has never validated subjects, and starting to reject entries it used to
    accept is a separate decision from resolving the ones it does accept.

    Callers must hold the vault lock: the table below is read, edited and
    rewritten in full.
    """
    if not normalize(subject):
        return subject

    index = vault / "_index"
    index.mkdir(parents=True, exist_ok=True)
    table = index / "subjects.tsv"
    # This table is a read-modify-write over one file shared by every entity in
    # the vault, so two appends to DIFFERENT entities race on it exactly as two
    # appends to one log do. The caller's vault lock is what serializes it;
    # there is no second lock here, because one mutex the whole vault agrees on
    # cannot be taken in the wrong order and cannot deadlock against itself.
    subjects = Subjects.load(table)
    before = {canon: list(variants) for canon, variants in subjects.rows.items()}
    canonical = subjects.canonical(subject)
    if subjects.rows != before:
        subjects.save(table)
    return canonical


def _inherit_sensitivity(new: Entry, same_subject: list[Entry]) -> None:
    """Sensitivity ratchets up and never down.

    Closing the `sens/` vocabulary supplied the exact token that expresses a
    downgrade (`sens/normal`) and nothing that forbids using it: filing a health
    fact at `sens/private` and later the same subject at `sens/normal` left the
    current entry marked normal, and `lv find status/current sens/normal`
    returned the health fact. `classify` consults subject, body and confidence
    -- never `sens/` -- so nothing else was going to catch it.

    Once a claim about a subject has been judged private, every later claim
    about that subject is at least as sensitive, including after the private
    entry itself has been superseded. Declassifying is a human decision made in
    the file, not something an extractor gets to do by omission.

    The contradictory pair is not written: `sens/private sens/normal` on one
    entry is no marker at all, since which one wins depends on which consumer
    looks first. validate_tags rejects that pair on the way in for the same
    reason.
    """
    if SENS_PRIVATE in new.tags:
        return
    if not any(SENS_PRIVATE in e.tags for e in same_subject):
        return
    new.tags = [t for t in new.tags if t != SENS_NORMAL] + [SENS_PRIVATE]


def _log_conflict(vault: Path, log: Path, a: Entry, b: Entry) -> None:
    review = vault / "_review" / "conflicts.md"
    review.parent.mkdir(parents=True, exist_ok=True)
    with review.open("a", encoding="utf-8") as fh:
        fh.write(
            f"\n## {b.date} — {b.subject}\n"
            f"file: {log}\n"
            f"- A ({a.source}): {a.body.strip()}\n"
            f"- B ({b.source}): {b.body.strip()}\n"
        )


def append_entry(log: str | Path, new: Entry, vault: str | Path) -> str:
    """Append `new` to `log`, never altering the prose of existing entries.

    Returns the classification that was applied.

    Appending is a read-modify-write over the whole log and over
    `_index/subjects.tsv`, so the lock covers the ENTIRE span -- the read, the
    subject resolution, the classification and the rename. The read is half the
    race: two processes that overlap on it each parse the same file, each render
    their own version of it, and the second rename wins. Both exit 0 and the
    surviving file is perfectly well-formed markdown, so nothing anywhere looks
    like an error. Wrapping only the write would leave that untouched.

    Making the write atomic does not help and slightly hurts: it removed the
    torn file that used to be the only evidence anything was lost.

    The lock is on the vault, not on this entity's directory, because a single
    `lv add` writes both this log and the vault-wide `entities.tsv` and needs
    one mutex across both. It is re-entrant, so a caller that already holds it
    -- `bin/lv` does -- pays only a counter bump here. It is also advisory: it
    serializes callers that come through `lifevault.vaultlock` and nothing else.

    Raises `VaultLockTimeout` if another writer holds the vault for longer than
    the default wait. Neither that nor `VaultLockUnavailable` is an `OSError`;
    see `lifevault.vaultlock` for why, and note that a caller catching `OSError`
    around this call will not swallow them.
    """
    log, vault = Path(log), Path(vault)
    with vault_lock(vault):
        new.subject = _canonical_subject(vault, new.subject)
        return _append_locked(log, new, vault)


def _same_subject_as(new: Entry, existing: list[Entry], vault: Path) -> list[Entry]:
    """Every stored entry about the same subject as `new`, however it was spelled.

    Canonicalizing only the ARRIVING subject fixes nothing: this comparison runs
    against the raw `subject:` strings parsed off disk, so every spelling filed
    before the namespace existed stays invisible and the parallel truth survives
    untouched. Both sides have to be resolved.

    The stored spelling is only resolved, never rewritten. Spec §3 allows an
    entry's status line to change and nothing else, so entries already filed
    under a drifted spelling keep it on disk forever -- they just stop being
    unreachable from the canonical one.
    """
    if not normalize(new.subject):
        return [e for e in existing if e.subject == new.subject]

    subjects = Subjects.load(vault / "_index" / "subjects.tsv")
    return [
        e for e in existing
        if (subjects.resolve(e.subject) or e.subject) == new.subject
    ]


def _validate_vocabulary(new: Entry, vault: Path) -> None:
    """Refuse a tag outside the closed vocabulary, at the door every write uses.

    `validate_tags` has run in `bin/lv add` since the vocabulary was closed. It
    never ran here -- so every caller that files through the library, which is
    how every bulk import in this project actually works, skipped it. 30 entries
    reached the vault tagged `area/social,` and `conf/stated,`, trailing commas
    and all, because one composer emitted comma-separated tags. They parsed,
    they rendered, and they were findable by nothing: `lv find area/social`
    does not match `area/social,`, so the failure showed up as a quiet zero
    rather than as an error.

    A vocabulary enforced at one of two doors is not closed.

    A vault with no `_index/tags.tsv` is exempt: a fresh vault has not got one
    yet, and refusing every write until it does would make the first `lv add`
    in a new vault impossible.
    """
    table = vault / "_index" / "tags.tsv"
    if not table.exists():
        return
    errors = validate_tags(new.tags, load_vocab(table))
    if errors:
        raise ValueError("; ".join(errors))


def _append_locked(log: Path, new: Entry, vault: Path) -> str:
    _validate_vocabulary(new, vault)
    existing = parse_entries(log.read_text(encoding="utf-8")) if log.exists() else []

    same_subject = _same_subject_as(new, existing, vault)
    _inherit_sensitivity(new, same_subject)

    # These entries are the same subject as `new` by resolution, which is what
    # `classify` would otherwise decide for itself by comparing raw strings --
    # and get wrong for `allergy` against a stored `allergies`. The copy exists
    # only to answer that question; the status stamps below go on the originals.
    def verdict_against(old: Entry) -> str:
        return classify(dataclasses.replace(old, subject=new.subject), new)

    # A duplicate of ANY existing entry is a no-op, whatever that entry's status.
    for old in same_subject:
        if verdict_against(old) == "duplicate":
            return "duplicate"

    # A subject in dispute has NO current entry, and "no current entry" must not
    # be read as "nothing to compare against". It was: the newcomer was filed
    # `independent` and became the live answer without ever being weighed
    # against the claims it contradicted -- so a dispute could be ended by
    # whoever happened to write next, including the nightly sweep. At
    # `conf/derived` that inverts the confidence tiers outright, making a guess
    # the answer to a question the user has stated two conflicting answers to.
    #
    # A dispute is ended by a person, through `lifevault.resolve`, and by
    # nothing else. Until then every further claim joins it.
    disputed = [e for e in same_subject if e.status == "conflicted"]
    if disputed:
        new.status = "conflicted"
        new.supersedes = None
        _log_conflict(vault, log, disputed[0], new)
        _write_all(log, existing + [new])
        return "conflict"

    # Only the entry that is currently authoritative may be superseded,
    # conflicted, or block the newcomer. Already-superseded entries are frozen
    # history: restamping them would rewrite the past every time a fact changes.
    current = next((e for e in same_subject if e.status == "current"), None)
    if current is None:
        _write_all(log, existing + [new])
        return "independent"

    verdict = verdict_against(current)

    if verdict == "supersession":
        if new.date > current.date:
            current.status = f"superseded-by {new.date}"
            new.supersedes = current.date
        else:
            new.status = f"superseded-by {current.date}"

    elif verdict == "conflict":
        current.status = "conflicted"
        new.status = "conflicted"
        new.supersedes = None
        _log_conflict(vault, log, current, new)

    elif verdict == "blocked":
        # Block whichever side is lower-confidence, not simply the newcomer:
        # on equal dates the arriving entry may be the more trustworthy one.
        if _rank(new) < _rank(current):
            new.status = f"blocked-by-{current.conf}"
        else:
            current.status = f"blocked-by-{new.conf}"

    _write_all(log, existing + [new])
    return verdict
