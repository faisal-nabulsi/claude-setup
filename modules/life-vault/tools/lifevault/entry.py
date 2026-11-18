from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date as _date

DELIM = "<!-- entry -->"
DELIM_ANY_RE = re.compile(r"^(\\*)" + re.escape(DELIM) + r"$")
DELIM_ESCAPED_RE = re.compile(r"^(\\+)" + re.escape(DELIM) + r"$")
HEADER_RE = re.compile(r"^## ([0-9]{4}-[0-9]{2}-[0-9]{2}) — (.+)$")
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
META_KEYS = ("subject", "status", "tags", "source", "supersedes", "quote")
REQUIRED_META = ("subject", "status", "tags", "source")
LINE_BREAK_RE = re.compile(r"[\r\n\v\f\x1c\x1d\x1e\x85  ]")

# A quote is an anchor, not a copy of the source: spec 12.1 only needs a span
# distinctive enough to find by string search. The cap exists because the whole
# quote lives on one metadata line, and a 40,000-character line makes the log
# unreadable in Obsidian -- which spec §1 requires it to stay. Generous on
# purpose: 2,000 characters is far longer than any message in the corpus
# spec 12.6 measured.
MAX_QUOTE = 2000

# Characters that must not reach the file raw, each with a two-character escape
# that reads naturally to a human editing the vault by hand.
_QUOTE_ESCAPES = {"\\": r"\\", "\n": r"\n", "\r": r"\r", "\t": r"\t",
                  "\v": r"\v", "\f": r"\f"}
_QUOTE_UNESCAPES = {escape[1]: raw for raw, escape in _QUOTE_ESCAPES.items()}
_HEX = "0123456789abcdefABCDEF"


class MalformedEntryError(ValueError):
    """A stored block could not be parsed. Raised rather than dropping it."""


@dataclass
class Entry:
    date: str
    title: str
    subject: str
    status: str
    tags: list[str] = field(default_factory=list)
    source: str = ""
    body: str = ""
    supersedes: str | None = None
    # Spec 12.1: a span copied verbatim from the source, so a fabricated
    # fact is caught by string search instead of judgement. Optional here:
    # entries written before 12.1 existed carry no quote, and this layer
    # must still read them. Requiring it is the extractor's job, not the
    # parser's.
    quote: str | None = None

    @property
    def conf(self) -> str:
        for t in self.tags:
            if t.startswith("conf/"):
                return t.split("/", 1)[1]
        return "derived"


def _normalize_body(body: str) -> str:
    """Collapse every line boundary str.splitlines() recognises into a plain \n.

    render and parse MUST agree on what a line is. str.splitlines() splits on
    \r, \v, \f, U+0085, U+2028 and U+2029, which body.split("\n") does not see.
    Without this, a delimiter hidden behind one of those is invisible to the
    escaper but visible to the parser -- which lets body text forge a fake entry.
    """
    return "\n".join(body.splitlines())


def _single_line(value: str) -> str:
    """Metadata must never contain a line boundary, or it could forge structure."""
    return LINE_BREAK_RE.sub(" ", value)


def _escape_char(ch: str) -> str:
    if ch in _QUOTE_ESCAPES:
        return _QUOTE_ESCAPES[ch]
    return f"\\x{ord(ch):02x}" if ord(ch) < 0x100 else f"\\u{ord(ch):04x}"


def _escape_quote(value: str) -> str:
    """Encode a quote so it occupies one line and loses nothing on the way back.

    A quote is a span copied verbatim out of a source, and spec 12.1 verifies it
    by searching that source for the exact string: a quote that is not found
    literally means the fact was fabricated and the entry is rejected. So any
    character the round trip alters turns an HONEST fact into a rejected one --
    and the corpus is line-wrapped iMessages, emails and transcripts, where a
    distinctive span routinely crosses a line break.

    Normalizing line breaks to spaces (the way `source` is normalized) is not an
    option here for the same reason, and neither is leaving them raw: the quote
    is rendered inside the metadata block, so a surviving break would let the
    text after it parse as another metadata line of the same entry and flip a
    live fact to superseded.

    Two classes of character are escaped:

    * anything str.splitlines() treats as a line boundary, plus the backslash
      that makes the encoding reversible at all;
    * whichever character sits at each end, if it is whitespace -- because the
      parser strips the metadata value, and a span sliced out of a line-oriented
      source keeps the newline or the indentation it was sliced with.
    """
    text = "".join(
        _escape_char(ch) if ch in _QUOTE_ESCAPES or LINE_BREAK_RE.match(ch) else ch
        for ch in value
    )
    if text and text[0].isspace():
        text = _escape_char(text[0]) + text[1:]
    if text and text[-1].isspace():
        text = text[:-1] + _escape_char(text[-1])
    return text


def _unescape_quote(value: str, where: str) -> str:
    """Decode _escape_quote, reporting anything it could not have produced.

    An unknown escape is raised rather than passed through. Passing it through
    would re-escape its backslash on the next write, so an entry nobody touched
    would change on disk every time a sibling fact was appended -- which is the
    asymmetry this encoding exists to remove. A hand editor who wants a literal
    backslash writes two, and the message says so.
    """
    if "\\" not in value:
        return value

    out: list[str] = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        marker = value[i + 1] if i + 1 < len(value) else ""
        if marker in _QUOTE_UNESCAPES:
            out.append(_QUOTE_UNESCAPES[marker])
            i += 2
            continue
        if marker in ("x", "u"):
            width = 2 if marker == "x" else 4
            digits = value[i + 2 : i + 2 + width]
            if len(digits) == width and all(d in _HEX for d in digits):
                out.append(chr(int(digits, 16)))
                i += 2 + width
                continue
        raise MalformedEntryError(
            f"unknown escape '\\{marker}' in the quote of entry {where}: {value!r} "
            f"(a literal backslash is written as two)"
        )
    return "".join(out)


def _escape_body(body: str) -> str:
    """Prefix a backslash to any line that could be mistaken for a delimiter."""
    return "\n".join(
        "\\" + line if DELIM_ANY_RE.match(line) else line
        for line in body.split("\n")
    )


def _unescape_body(body: str) -> str:
    """Remove exactly one backslash from lines escaped by _escape_body."""
    return "\n".join(
        line[1:] if DELIM_ESCAPED_RE.match(line) else line
        for line in body.split("\n")
    )


def _validate(e: Entry) -> None:
    """Refuse to write an entry that could not be read back correctly.

    Structural fields raise. date, title, subject, status and supersedes all
    drive parsing, conflict classification or supersession ordering, and every
    one of them is set by our own code -- so a line break or an impossible date
    in them is a bug worth surfacing, not smoothing over.

    `source` is the deliberate exception: it can carry a real-world filename, so
    _single_line normalizes it rather than aborting a whole ingest batch.
    """
    for name, value in (("date", e.date), ("supersedes", e.supersedes)):
        if value is None:
            continue
        if not DATE_RE.match(value):
            raise ValueError(f"{name} must be YYYY-MM-DD, got {value!r}")
        try:
            _date.fromisoformat(value)
        except ValueError:
            raise ValueError(f"{name} is not a real calendar date: {value!r}") from None

    if not _single_line(e.title).strip():
        raise ValueError("entry title must not be empty")
    if not e.status.strip():
        raise ValueError("entry status must not be empty")

    for name in ("title", "subject", "status"):
        value = getattr(e, name)
        if LINE_BREAK_RE.search(value):
            raise ValueError(f"{name} must not contain a line break: {value!r}")

    if e.quote is not None:
        # A quote is either absent or an anchor. Whitespace is neither, and
        # accepting it produces a fact that reads as anchored while carrying
        # nothing to verify against -- spec 12.1 caps unanchored facts at
        # conf/derived, and that cap cannot apply to an anchor nobody can see
        # is missing. `supersedes=""` raises for exactly this reason.
        if not e.quote.strip():
            raise ValueError(f"quote must be an anchor or absent, not blank: {e.quote!r}")
        if len(e.quote) > MAX_QUOTE:
            raise ValueError(
                f"quote is too long to be an anchor: {len(e.quote)} characters, "
                f"cap is {MAX_QUOTE}; store a shorter distinctive span"
            )

    for tag in e.tags:
        if not tag or any(c.isspace() for c in tag):
            raise ValueError(f"tag must be non-empty and whitespace-free: {tag!r}")
        # Exactly one slash, hyphen-free facet: this is what makes the index's
        # facet-value filename injective. Without it `person/mom-dad` and
        # `person/mom/dad` share a file and one silently loses its postings.
        if tag.count("/") != 1:
            raise ValueError(f"tag must be exactly facet/value: {tag!r}")
        facet, value = tag.split("/")
        if not facet or "-" in facet:
            raise ValueError(f"tag facet must be non-empty and hyphen-free: {tag!r}")
        if not value:
            raise ValueError(f"tag must have a value after the slash: {tag!r}")
        if facet == "status":
            raise ValueError(f"status is a field, not a tag: {tag!r}")
        if len(tag) > 200:
            raise ValueError(f"tag is too long to be a filename: {tag[:40]!r}...")


def render_entry(e: Entry) -> str:
    _validate(e)
    lines = [
        DELIM,
        f"## {_single_line(e.date)} — {_single_line(e.title)}",
        f"subject: {_single_line(e.subject)}",
        f"status: {_single_line(e.status)}",
        f"tags: {_single_line(' '.join(e.tags))}",
        f"source: {_single_line(e.source)}",
    ]
    if e.quote is not None:
        # Escaped, not normalized, and not rejected: a quote is copied out of
        # real-world text, so a line break in it is expected input rather than a
        # bug in our own code -- but unlike `source` it has to come back
        # unchanged, because §12.1 verifies it by searching the source for the
        # exact string. `is not None` rather than truthiness: testing the raw
        # value let a whitespace-only quote render a line that parsed back to
        # None, silently deleting the anchor. _validate now rejects that value
        # outright, and this test no longer depends on which happens first.
        lines.append(f"quote: {_escape_quote(e.quote)}")
    if e.supersedes:
        lines.append(f"supersedes: {_single_line(e.supersedes)}")
    lines.append("")
    lines.append(_escape_body(_normalize_body(e.body).strip()))
    return "\n".join(lines) + "\n"


def _build(header, meta, body_lines) -> Entry:
    date, title = header
    quote = meta.get("quote") or None
    return Entry(
        date=date,
        title=title,
        subject=meta.get("subject", ""),
        status=meta.get("status", "current"),
        tags=meta.get("tags", "").split(),
        source=meta.get("source", ""),
        supersedes=meta.get("supersedes") or None,
        quote=None if quote is None else _unescape_quote(quote, date),
        body=_unescape_body("\n".join(body_lines).strip()),
    )


def _parse_chunk(lines: list[str]) -> Entry | None:
    """Parse one delimited block. Raises rather than dropping anything.

    Silence is the enemy here. If an unrecognized key were treated as body text,
    a typo like `stauts:` would discard the real status and _build would default
    it to "current" -- turning a superseded fact back into a live one, with no
    error anywhere. This is a hand-editable Obsidian vault, so that typo is a
    matter of when, not if.
    """
    header = None
    meta: dict[str, str] = {}
    body: list[str] = []
    in_meta = False

    for line in lines:
        if header is None:
            m = HEADER_RE.match(line)
            if m:
                header, in_meta = m.groups(), True
            elif line.startswith("## "):
                raise MalformedEntryError(
                    f"malformed entry header, expected '## YYYY-MM-DD — title': {line!r}"
                )
            elif line.strip():
                raise MalformedEntryError(f"content before the entry header: {line!r}")
            continue
        if in_meta:
            if not line.strip():
                in_meta = False
                continue
            key, sep, value = line.partition(":")
            key = key.strip()
            if not sep or key not in META_KEYS:
                raise MalformedEntryError(
                    f"unrecognized metadata line in entry {header[0]}: {line!r}"
                )
            if key in meta:
                # Last-wins is the silent failure this parser exists to refuse.
                # An entry carrying `quote: the real anchor` followed by
                # `quote: (synthesized)` would parse as synthesized, and §12.1's
                # confidence cap keys off exactly that sentinel -- so a stray
                # duplicated line would change what a fact claims about its own
                # provenance with nothing to show for it. The same edit on
                # `status:` silently resurrects or buries a fact.
                raise MalformedEntryError(
                    f"repeated metadata line in entry {header[0]}: {line!r} "
                    f"(already saw {key!r}: {meta[key]!r})"
                )
            meta[key] = value.strip()
            continue
        body.append(line)

    if header is None:
        return None

    missing = [k for k in REQUIRED_META if k not in meta]
    if missing:
        raise MalformedEntryError(
            f"entry {header[0]} is missing metadata: {', '.join(missing)}"
        )

    # _validate rejects these on write; reject them on read too, or parse_entries
    # could hand back an Entry that render_entry would refuse to write.
    if not header[1].strip():
        raise MalformedEntryError(f"entry {header[0]} has an empty title")
    if not meta["status"].strip():
        raise MalformedEntryError(f"entry {header[0]} has an empty status")

    return _build(header, meta, body)


def parse_entries(text: str) -> list[Entry]:
    chunks: list[list[str]] = []
    current: list[str] | None = None

    for line in text.splitlines():
        if line == DELIM:
            current = []
            chunks.append(current)
        elif current is not None:
            current.append(line)

    entries: list[Entry] = []
    for chunk in chunks:
        entry = _parse_chunk(chunk)
        if entry is None:
            preview = "\n".join(chunk[:3])
            raise MalformedEntryError(
                f"delimited block has no valid '## YYYY-MM-DD — title' header: {preview!r}"
            )
        entries.append(entry)
    return entries
