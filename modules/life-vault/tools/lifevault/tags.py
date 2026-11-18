"""Tag vocabulary rules (spec §5 and §10).

Facets come in three groups:

* ``MANDATORY_FACETS`` — every entry must carry one tag from each.
* ``OPEN_FACETS`` — link facets whose values are slugs that cannot be
  enumerated in advance (some person, some org, some month), so any value
  after the slash is accepted.
* everything else is CLOSED: the whole tag must appear verbatim in
  ``_index/tags.tsv`` or it is reported as an unknown tag.

``sens/`` is CLOSED, with exactly two values: ``sens/private`` and
``sens/normal``. It is deliberately not an open facet, because a typo in a
sensitivity tag is not cosmetic — if ``sens/privat`` validated, health,
finance, legal or relationship-conflict material would be filed with no
protection marker at all and nothing downstream would ever notice.

``sens/`` is OPTIONAL, not mandatory, and absence is not permission. An entry
with no ``sens/`` tag is UNCLASSIFIED: it means nobody has judged it, which
is not the same as judging it safe. Code that decides whether material may
leave the machine must not re-derive that rule from this paragraph — call
``is_sensitive`` / ``may_leave_machine`` below, which treat a missing
``sens/`` tag exactly as they treat ``sens/private``.

The value is named ``sens/normal`` and not ``sens/public`` because "public"
reads as "cleared for publication" when what is actually being asserted is
only "nobody judged this sensitive". The name is also load-bearing across
documents: Plan 2's capture gate computes
``sens = "sens/private" if sensitive else "sens/normal"`` for every candidate
it files, so a vocabulary spelling it any other way rejects every
non-sensitive fact the nightly sweep produces.
"""

from __future__ import annotations

from pathlib import Path

MANDATORY_FACETS = ("area", "type", "conf")
OPEN_FACETS = ("person", "org", "project", "place", "time")

SENS_PRIVATE = "sens/private"
SENS_NORMAL = "sens/normal"


def is_sensitive(tags: list[str]) -> bool:
    """True unless these tags carry an explicit, uncontradicted ``sens/normal``.

    This is the one place the "absence is not permission" rule is decided.
    Written out at each call site it becomes ``if "sens/private" in tags``,
    which is the wrong test: it reads an unclassified health or finance entry
    -- one nobody has judged yet -- as safe to send.

    A misspelled marker is not a marker either. ``sens/norml`` is neither
    ``sens/normal`` nor ``sens/private``, so it lands in the same bucket as no
    marker at all: sensitive. validate_tags rejects the typo on the way in, but
    this vault is hand-edited in Obsidian and nothing validates on read yet.
    """
    if SENS_PRIVATE in tags:
        return True
    return SENS_NORMAL not in tags


def may_leave_machine(tags: list[str]) -> bool:
    """Spec §10, stated positively: may this material go into outbound content?

    The exact inverse of is_sensitive, named for the decision it answers so a
    caller cannot get the polarity backwards while reading its own code.
    """
    return not is_sensitive(tags)


def load_vocab(path: str | Path) -> set[str]:
    """Read the closed vocabulary, refusing any row that does not parse as one.

    The row check is here rather than in a test over one file because this file
    is the protection mechanism for §10, and the mistake it guards against is
    both easy and invisible. A row written with spaces instead of tabs makes the
    whole line a single vocabulary key: ``sens/private`` then silently vanishes
    from the vocabulary and every sensitive entry starts failing with
    "unknown tag". A row filed under the wrong facet column loads with no
    complaint at all. Neither looks wrong when you read the file.

    Only the first two columns are constrained. The description is free text and
    may contain tabs, because spec §5 makes adding a tag an ordinary hand edit
    and a loader that rejects ordinary edits gets worked around.
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        # Almost always a wrong vault root rather than a deleted file, so say
        # which file was wanted. Still a FileNotFoundError: callers that key on
        # the type keep working.
        raise FileNotFoundError(exc.errno, f"tag vocabulary not found: {p}") from None

    vocab: set[str] = set()
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        columns = line.split("\t")
        if len(columns) < 2:
            raise ValueError(
                f"{p}:{lineno}: expected 'tag<TAB>facet<TAB>description', got {raw!r}"
            )
        tag, facet_column = columns[0].strip(), columns[1].strip()
        if "/" not in tag or any(c.isspace() for c in tag):
            raise ValueError(f"{p}:{lineno}: {tag!r} is not a facet/value tag")
        facet = tag.split("/", 1)[0]
        if facet != facet_column:
            raise ValueError(
                f"{p}:{lineno}: tag {tag!r} is filed under facet {facet_column!r}"
            )
        vocab.add(tag)
    return vocab


def validate_tags(tags: list[str], vocab: set[str]) -> list[str]:
    """Return a list of problems with ``tags``; empty means valid.

    Tags in an open facet pass on shape alone. Every other tag — including
    every ``sens/`` tag — must be in ``vocab``. A missing ``sens/`` tag is
    not an error here: the facet is optional, and an entry without one is
    unclassified rather than normal (see the module docstring).

    A closed facet may appear at most once. ``sens/private sens/normal`` on one
    entry is not a marker at all — which of the two wins is decided by whichever
    consumer happens to look first — and two ``area/`` values make the mandatory
    facet stop discriminating. Open facets are exempt because repeating them is
    the point: an entry really can be about two people.
    """
    errors: list[str] = []
    by_facet: dict[str, list[str]] = {}
    for tag in tags:
        if "/" in tag:
            by_facet.setdefault(tag.split("/", 1)[0], []).append(tag)

    for facet in MANDATORY_FACETS:
        if facet not in by_facet:
            errors.append(f"missing mandatory facet: {facet}/")

    for facet, values in by_facet.items():
        if facet not in OPEN_FACETS and len(values) > 1:
            errors.append(f"more than one {facet}/ tag: {' '.join(values)}")

    for tag in tags:
        if "/" not in tag:
            errors.append(f"malformed tag: {tag}")
            continue
        facet = tag.split("/", 1)[0]
        if facet in OPEN_FACETS:
            continue
        if tag not in vocab:
            errors.append(f"unknown tag: {tag}")

    return errors
