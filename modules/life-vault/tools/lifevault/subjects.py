"""One canonical namespace for entry subjects (spec §4 and §6).

`conflict.classify` decides whether a new fact supersedes an old one, and its
first test is `a.subject != b.subject`. Subjects are free text written by
whoever filed the entry, so `allergy` and `allergies` are two unrelated facts:
neither supersedes the other, neither conflicts with the other, and both stay
`status: current`. That is not a cosmetic problem. Filing "No known allergies"
at conf/stated and later "Allergic to penicillin" at conf/derived produced
exactly that on this machine -- two live answers to one question, and §6's
guarantee that a guess can never override a stated fact quietly stopped
applying, because the two claims never met.

This module is to subjects what `entities.py` is to names: every spelling
resolves through one table, so the claims about one subject are guaranteed to
land in front of each other.

**The rule is one sentence: a subject's key is its singular fold, except that a
spelling already in the table keeps the key it already has.**

The KEY is a guess; the NAME is not. The singular fold is what two spellings are
looked up by, and it lives in memory -- it is recomputed on every load and costs
nothing if it changes. The canonical NAME is the first spelling actually seen,
slugified, exactly as `entities.mint` treats a person's name. It has to be a
real spelling because it is written into `subject:` on every entry filed under
it, and spec §3 forbids editing anything on a stored entry except its status
line: a coined stem there (`diabete`, `calory`, `movy`) is permanent, and no
human will find it by looking.

The exception is what keeps the merge honest. If `job` and `jobs` are both
already registered -- because someone wrote them into `_index/subjects.tsv` by
hand, meaning them differently -- folding `jobs` into `job` would re-point a
subject that already has entries filed under it, and the next fact filed under
`jobs` would mark a real, unrelated fact as superseded.

That asymmetry decides every judgement call here, and it is why the plural
rules below are so timid. Failing to merge two spellings leaves a fact harder
to find, which a later re-run can repair. Merging two subjects that were never
the same thing fabricates a supersession that never happened and stamps
`superseded-by` onto a fact that is still true -- and since the vault is
append-only, that stamp is permanent. When in doubt, do not merge.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Container
from pathlib import Path

from .atomic import write_atomic

FORBIDDEN_IN_TSV = ("\t", "\n", "\r", "|")

# A word ending in one of these is not a plural: "status", "address" and
# "analysis" would become "statu", "addres" and "analysi". Stripping the "s"
# off a word that never had a plural "s" is how a fold starts inventing
# neighbours for subjects that have none.
NOT_PLURAL_ENDINGS = ("ss", "us", "is")

# Below this, the "s" is likelier to be part of a short word ("gas", "bus")
# than a plural marker, and the stem is too small to be a subject anyway.
MIN_PLURAL_LENGTH = 4


def _fold(subject: str) -> str:
    """Fold a subject to its bare lookup form: no case, no accents, no punctuation.

    `school-enrollment`, `School Enrollment` and `school_enrollment` are one
    subject. Non-Latin scripts are PRESERVED rather than stripped, or every
    subject written in one would fold to the empty string and collide with
    every other.

    A combining mark is dropped only when it sits on an ASCII letter, which is
    what makes `café` and `cafe` one subject. Everywhere else the mark is kept,
    because outside the Latin alphabet a combining mark is usually not an
    accent but part of the letter: in kana the dakuten is the whole difference
    between だいがく (university) and たいがく (dropping out), and between ハン,
    バン and パン. Stripping marks blindly merges those, and this module's one
    unrecoverable failure is merging subjects that were never the same thing --
    a fact filed under each would stamp the other `superseded-by`, permanently.

    This is where the fold parts company with `entities.normalize`, which still
    strips every mark and merges those names. Same bug, different file; fixing
    it there belongs with that module.
    """
    kept: list[str] = []
    base_is_ascii = False
    for ch in unicodedata.normalize("NFKD", subject):
        if unicodedata.combining(ch):
            if not base_is_ascii:
                kept.append(ch)
            continue
        base_is_ascii = ch.isascii()
        kept.append(ch)
    # Recompose, so a precomposed だ and a decomposed one fold identically --
    # and so a kept mark is part of an alphanumeric character rather than a
    # lone combining code point, which the punctuation pass below would blank.
    text = unicodedata.normalize("NFC", "".join(kept)).casefold()
    text = "".join(c if c.isalnum() or c.isspace() else " " for c in text)
    return re.sub(r"\s+", " ", text).strip()


def _singular_word(word: str) -> str:
    """Singularize one word, using only the two rules that are hard to get wrong.

    English pluralization has a long tail (`classes`, `boxes`, `children`,
    `people`) and every extra rule is another chance to fold two different
    words together. The tail is left alone on purpose: `class` and `classes`
    staying separate costs a search, while `glass` and `glasses` being merged
    costs a fact.
    """
    if word.endswith("ies") and len(word) > MIN_PLURAL_LENGTH:
        return word[:-3] + "y"
    if (
        word.endswith("s")
        and len(word) >= MIN_PLURAL_LENGTH
        and not word.endswith(NOT_PLURAL_ENDINGS)
    ):
        return word[:-1]
    return word


def _singularize(key: str) -> str:
    """Singularize the head noun of a folded key.

    Only the last word moves. In a subject phrase the last word is the thing
    being described and the earlier words qualify it, so `school enrollments`
    is `school enrollment` pluralized, while `jobs applied to` is a single
    subject that is not the plural of anything -- and must never be folded onto
    `job`, which is a different fact about a different part of a life.
    """
    if not key:
        return key
    words = key.split(" ")
    words[-1] = _singular_word(words[-1])
    return " ".join(words)


def normalize(subject: str, known: Container[str] | None = None) -> str:
    """Return the lookup key for `subject`.

    `known` is the set of keys already registered. Pass it and a spelling that
    is already in the table keeps its own key; omit it and the key is always
    the singular fold. Everything in the module docstring reduces to these
    three lines.
    """
    base = _fold(subject)
    if known is not None and base in known:
        return base
    return _singularize(base)


class Subjects:
    """Maps every spelling of one subject to exactly one canonical form.

    `rows` is `canonical -> variants`, mirroring `Entities.rows`. A variant is
    a spelling that has actually been seen and does not fold to the canonical
    on its own -- `allergies` under `allergy`. Recording it writes the merge
    decision into the file instead of leaving it to be recomputed, so a later
    change to the plural rules cannot silently re-route a subject that has
    entries filed under it.
    """

    def __init__(self, rows: dict[str, list[str]] | None = None):
        self.rows = rows if rows is not None else {}
        self._index: dict[str, str] = {}
        self._reindex()

    def _reindex(self) -> None:
        """Rebuild the key index from `rows`. Two passes, in this order.

        Pass 1 binds what the file literally says: every spelling written down
        resolves to the row it was written under. Pass 2 adds the singular
        folds, which are guesses this module made rather than facts the file
        states -- so they may only fill keys nobody has claimed outright.

        A singular key wanted by two different subjects goes to neither. There
        is no non-arbitrary way to choose, and choosing arbitrarily is the
        over-merge this module exists to avoid.
        """
        index: dict[str, str] = {}
        for canon, variants in self.rows.items():
            for spelling in (canon, *variants):
                key = _fold(spelling)
                if not key:
                    raise ValueError(f"subject has no alphanumeric content: {spelling!r}")
                owner = index.get(key)
                if owner is not None and owner != canon:
                    raise ValueError(
                        f"subject {spelling!r} maps to both {owner!r} and {canon!r}"
                    )
                index[key] = canon

        claims: dict[str, set[str]] = {}
        for canon, variants in self.rows.items():
            for spelling in (canon, *variants):
                key = _fold(spelling)
                singular = _singularize(key)
                if singular != key and singular not in index:
                    claims.setdefault(singular, set()).add(canon)
        for singular, contenders in claims.items():
            if len(contenders) == 1:
                index[singular] = next(iter(contenders))

        self._index = index

    @classmethod
    def load(cls, path: str | Path) -> "Subjects":
        """Read the table, refusing to silently discard any row.

        A bare `rows[canonical] = ...` would let a hand-edited file with a
        repeated or empty canonical overwrite an earlier row before __init__
        ever saw it, and the variants on the lost row would stop resolving --
        which is the drift this file exists to prevent, arriving through the
        file instead of the API.
        """
        rows: dict[str, list[str]] = {}
        p = Path(path)
        if p.exists():
            # utf-8-sig: an editor that writes a byte-order mark would
            # otherwise turn the header comment into a subject row called
            # "﻿# canonical", which resolves nothing and is invisible on
            # screen.
            for lineno, line in enumerate(p.read_text(encoding="utf-8-sig").splitlines(), 1):
                if not line.strip() or line.startswith("#"):
                    continue
                columns = line.split("\t")
                if len(columns) > 2:
                    raise ValueError(
                        f"{p}:{lineno}: expected 'canonical<TAB>variants', "
                        f"got {len(columns)} columns: {line!r}"
                    )
                canon, variants = (columns + [""])[:2]
                if not canon.strip():
                    raise ValueError(f"{p}:{lineno}: row has no canonical: {line!r}")
                if canon in rows:
                    raise ValueError(f"{p}:{lineno}: duplicate canonical {canon!r}")
                rows[canon] = [v for v in variants.split("|") if v]
        return cls(rows)

    def save(self, path: str | Path) -> None:
        """Rewrite the whole table, atomically.

        Rows are validated before anything is written, so a rejected field
        costs the caller an exception and leaves the file exactly as it was.

        The write goes through `lifevault.atomic.write_atomic` -- the same
        helper `entities.save` uses -- because this file resolves every subject
        in the vault and is rewritten in full on every append. A torn write
        does not damage one subject; it damages the table that keeps stated
        facts and derived guesses meeting each other at all, and a truncated
        table is silent: it parses cleanly as zero rows, so the next ingest
        re-mints every subject and nothing looks wrong.
        """
        lines = ["# canonical\tvariants (pipe-separated)"]
        for canon in sorted(self.rows):
            variants = self.rows[canon]
            for value in (canon, *variants):
                for ch in FORBIDDEN_IN_TSV:
                    if ch in value:
                        raise ValueError(f"{value!r} contains {ch!r}, cannot save as TSV")
            lines.append(f"{canon}\t{'|'.join(variants)}")
        write_atomic(path, lambda fh: fh.write("\n".join(lines) + "\n"))

    def resolve(self, subject: str) -> str | None:
        """Return the canonical form of `subject`, or None if it is unknown."""
        return self._index.get(normalize(subject, self._index))

    def add_variant(self, canonical: str, variant: str) -> None:
        """Record `variant` as another spelling of `canonical`.

        Raises rather than storing a spelling the table cannot hold: a tab or a
        pipe would split the row on the next read and hand one subject's
        variants to another.
        """
        if canonical not in self.rows:
            raise ValueError(f"unknown subject {canonical!r}")
        for ch in FORBIDDEN_IN_TSV:
            if ch in variant:
                raise ValueError(f"variant may not contain {ch!r}: {variant!r}")
        key = _fold(variant)
        if not key:
            raise ValueError(f"variant has no alphanumeric content: {variant!r}")
        owner = self._index.get(key)
        if owner is not None and owner != canonical:
            raise ValueError(f"variant {variant!r} already resolves to {owner!r}")
        if key == _fold(canonical) or key in {_fold(v) for v in self.rows[canonical]}:
            return  # already covered; recording it again would only grow the file
        self.rows[canonical].append(variant)
        self._reindex()

    def canonical(self, subject: str) -> str:
        """Return the canonical form of `subject`, registering it if it is new.

        This is the call every writer should make before filing an entry, and
        it is what puts two spellings of one subject in front of
        `conflict.classify` instead of leaving them as parallel truths.
        """
        key = normalize(subject, self._index)
        if not key:
            raise ValueError(f"subject has no alphanumeric content: {subject!r}")

        owner = self._index.get(key)
        if owner is not None:
            self.add_variant(owner, _storable(subject))
            return owner

        # Mint from the spelling that was actually used, NOT from `key`.
        # `key` is the singular fold, and the fold is a guess: it turns
        # `diabetes` into `diabete`, `taxes` into `taxe`, `movies` into `movy`
        # and `news` into `new`. A guess is fine as a lookup key, which lives
        # in memory and is recomputed on every load -- but the canonical form
        # is written into `subject:` on every entry filed under it, and spec §3
        # forbids editing anything on a stored entry except its status line. A
        # coined non-word there is permanent and unfindable. `entities.mint`
        # slugifies the real name for the same reason.
        #
        # The merge is not lost by minting the longer spelling: _reindex binds
        # the singular fold to this row as well, so `allergy` still resolves to
        # a stored `allergies`.
        canon = _fold(subject).replace(" ", "-")
        if canon in self.rows:
            # rows and the index disagree, so resolution is no longer
            # trustworthy. Better to stop than to hand back a subject that
            # might route a fact onto someone else's history.
            raise ValueError(f"duplicate canonical {canon!r} is missing from the index")
        self.rows[canon] = []
        self._reindex()
        # No add_variant here: `canon` IS this spelling's fold, so recording it
        # would be recording that a subject is a variant of itself. The call
        # that matters is the one above, on the branch where a DIFFERENT
        # spelling was folded onto an existing row.
        return canon


def _storable(spelling: str) -> str:
    """The spelling as written, or its folded form if the table cannot hold it.

    A subject carrying a tab or a pipe is still a subject and must still
    resolve; it is only the record of how it was spelled that has to give way.
    Folding it keeps the row readable and loses nothing that affects lookup,
    since the fold is what resolves. This mirrors `source` on an entry, which
    is normalized rather than rejected for the same reason: a real-world string
    should not be able to abort a write.
    """
    if any(ch in spelling for ch in FORBIDDEN_IN_TSV):
        return _fold(spelling)
    return spelling
