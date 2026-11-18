from __future__ import annotations

import re

from .entry import Entry

CONF_RANK = {"derived": 0, "confirmed": 1, "stated": 2}


def _same_claim(a: Entry, b: Entry) -> bool:
    norm = lambda s: re.sub(r"\s+", " ", s.strip().lower())
    return norm(a.body) == norm(b.body)


def _rank(e: Entry) -> int:
    """Confidence tier as a number. Unknown values are an error, not a default.

    The tag vocabulary is validated on write, so an unrecognized conf/ value can
    only reach here from a hand-edited file. Defaulting it to the lowest tier
    would silently change which fact wins; naming it lets the user fix the typo.
    """
    try:
        return CONF_RANK[e.conf]
    except KeyError:
        raise ValueError(
            f"unknown confidence {e.conf!r} in entry {e.date} ({e.subject}); "
            f"expected one of {', '.join(sorted(CONF_RANK))}"
        ) from None


def classify(a: Entry, b: Entry) -> str:
    """Return one of: independent, duplicate, conflict, blocked, supersession."""
    if a.subject != b.subject:
        return "independent"

    if _same_claim(a, b):
        return "duplicate"

    if a.date == b.date:
        # Confidence is consulted even here. Otherwise a same-day derived guess
        # could drag a stated fact into conflict limbo, and the vault would
        # answer "you told me both" when only one claim came from the user.
        return "blocked" if _rank(a) != _rank(b) else "conflict"

    older, newer = (a, b) if a.date < b.date else (b, a)
    if _rank(newer) < _rank(older):
        return "blocked"
    return "supersession"
