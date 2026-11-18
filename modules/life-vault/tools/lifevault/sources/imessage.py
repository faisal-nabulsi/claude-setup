"""Read the Messages database and group it into conversation bursts.

Measured on the real corpus: 130,556 messages carry text, the median is 17
characters, 47% are under 16, and only 1.3% exceed 100. The most frequent
contents are an attachment placeholder, "ya", "ok", and GamePigeon invitations.

So the unit of reading here is a BURST, never a single message. "ok" on its own
is noise; "ok" inside an exchange where someone reports a death is context. Spec
§12.6 puts the bar on what gets WRITTEN, not on what gets read -- the whole
corpus is only ~810k tokens, so reading it all is cheap and the only real cost of
noise is a polluted vault.

**The text of a message lives in one of TWO columns.** `message.text` held it
until July 2024. After that Messages archives the body as an NSAttributedString
into `message.attributedBody` and leaves `text` NULL, so from 2024-07 onwards
only 312 rows carry a `text` column against 23,876 that carry only a blob.
`decode_attributed_body` is the parser for that blob and `Message.source` says
which column any given message came from -- which matters, because one of them
has been read for years and the other had never been read at all. Reading it
added 24,058 messages and 719,011 characters, 28% more of the user's own words.

**Three things live in this database, not one.** `read_messages` reads the rows
that carry text in either column, counted above. `read_attachments` reads the
19,266 rows of the `attachment` table -- metadata only, never a byte of the 9.0GB
of photographs and video they point at. `read_textless_messages` reads the 28,758
rows with no `text`, saying what each one is and what, if anything, came out of
its blob; it is the audit that makes a refusal countable.

Nothing in this module writes to the database. It is opened read-only, always.
"""

from __future__ import annotations

import os
import re
import sqlite3
import unicodedata
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .redact import scan_hard

#: Apple's Core Data reference date. Message timestamps are offsets from here.
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

#: Above this, a raw value cannot plausibly be seconds -- it would be far in the
#: future -- so it is nanoseconds. Older databases store seconds; newer ones
#: nanoseconds, and the same file can be migrated between the two.
_NANOSECOND_THRESHOLD = 10**12

_GAMEPIGEON = frozenset(
    {
        "8 ball", "9 ball", "anagrams", "archery", "basketball", "checkers",
        "chess", "crazy 8", "cup pong", "darts", "dots & boxes", "filler",
        "four in a row", "gomoku", "knockout", "mancala", "mini golf", "poker",
        "sea battle", "tanks", "word bites", "word hunt", "20 questions",
    }
)

_TAPBACK = re.compile(
    r"^(?:liked|loved|laughed at|emphasi[sz]ed|questioned|disliked)\s+[\"“]",
    re.IGNORECASE,
)

#: U+FFFC OBJECT REPLACEMENT CHARACTER: an attachment with no text of its own.
_ATTACHMENT_ONLY = re.compile(r"^[￼\s]*$")


#: Where a `Message.text` came from. The distinction is not cosmetic: `text` has
#: been read since this module was written, and `attributedBody` had never been
#: read at all until `decode_attributed_body` was. A fact drawn from one has
#: years of downstream checking behind it; a fact drawn from the other has none.
SOURCE_TEXT = "text"
SOURCE_ATTRIBUTED = "attributedBody"
MESSAGE_SOURCES: tuple[str, ...] = (SOURCE_TEXT, SOURCE_ATTRIBUTED)


@dataclass(frozen=True)
class Message:
    text: str
    handle: str
    timestamp: datetime
    is_from_me: bool
    #: Which column this text came from. See MESSAGE_SOURCES.
    source: str = SOURCE_TEXT


@dataclass(frozen=True)
class Burst:
    """One stretch of conversation with one correspondent."""

    handle: str
    messages: tuple[Message, ...] = field(default_factory=tuple)

    @property
    def started(self) -> datetime:
        return self.messages[0].timestamp

    @property
    def ended(self) -> datetime:
        return self.messages[-1].timestamp

    @property
    def char_count(self) -> int:
        return sum(len(m.text) for m in self.messages)


def _decode_timestamp(raw: int) -> datetime:
    """Apple stores seconds in older databases and nanoseconds in newer ones.

    Guessing wrong does not fail loudly -- it puts every message decades away,
    which silently destroys every burst boundary and every date on every fact
    filed from them. So detect per row rather than assuming per file.
    """
    seconds = raw / 1_000_000_000 if abs(raw) >= _NANOSECOND_THRESHOLD else raw
    return APPLE_EPOCH + timedelta(seconds=seconds)


# --- the archived body ----------------------------------------------------
#
# Modern Messages stores the body of a message as an NSAttributedString archived
# with NSArchiver -- the old NeXT `typedstream` format, not NSKeyedArchiver -- in
# `message.attributedBody`, and leaves `message.text` NULL. Measured over the
# whole real database, `text` stops being written in July 2024: from 2024-07
# onwards only 312 rows carry it against 23,876 that carry only the blob. So this
# parser is not an edge case, it is how the last two years of the user's messages
# are stored.
#
# There is no stdlib reader for this format, so there is one here. It is small
# because Messages only writes a handful of shapes, and it is strict because the
# alternative -- a heuristic that finds the longest printable run of bytes --
# LOOKS like it works: measured against the truth on the 24,098 rows in question
# it gets 866 of them right, 3.6%, and returns confident-looking wrong answers
# for the rest. Attribute names, emoji split mid-character, link previews and
# message-effect metadata all sit in the same blob as the text.

#: `\x04` is the streamer version and `\x0bstreamtyped` the length-prefixed
#: signature. Every one of the 130,596 real blobs begins with exactly these.
_TS_HEADER = b"\x04\x0bstreamtyped"

#: A new shared item follows; nothing follows; this object's contents end here.
_TAG_NEW, _TAG_NIL, _TAG_END = 0x84, 0x85, 0x86

#: Integers are one signed byte unless prefixed by a width tag.
_INTEGER_TAGS = {0x81: 2, 0x82: 4, 0x83: 8}

#: A back-reference is an integer counting up from here: 0x92, read as a signed
#: byte, is -110 and means index 0. Bigger tables keep counting through -109,
#: -108 ... 0, 1, and on into the width-tagged forms, which is why this is
#: arithmetic on a decoded integer and not a byte range.
_FIRST_REFERENCE = -110

#: The deepest object nesting in the real corpus is 10. This is a guard against a
#: corrupt blob recursing until Python raises instead of this module refusing.
_MAX_DEPTH = 64

#: Integer type encodings: char, short, int, long, long long, signed and not.
_INTEGER_TYPES = frozenset("cCsSiIlLqQ")

#: Measured over all 130,596 blobs, the root is one of these two and nothing
#: else, and the string inside it is one of the two below and nothing else.
#: Accepting a third shape would be accepting a shape nobody has ever checked.
_ATTRIBUTED_ROOTS = frozenset({"NSAttributedString", "NSMutableAttributedString"})
_STRING_CLASSES = frozenset({"NSString", "NSMutableString"})


class _Refused(Exception):
    """Something in this blob is not understood, so nothing in it is trusted.

    Never raised past `decode_attributed_body`, which turns it into None. There
    is deliberately no partial result: a message this module half-understands is
    worse than one it never read, because the half it got would be filed as fact.
    """


class _Node:
    """One entry in the object table: a class, an instance, or a C string.

    Mutable, and registered before it is filled in, because typedstream numbers a
    reference the moment an item STARTS -- an object's own contents may refer
    back to it. Filling it in afterwards is what makes that legal.

    `kind` exists because all three share one numbering. A reference that is one
    slot out still lands on SOMETHING and reading carries on, so the only way to
    notice is to check what kind of thing it landed on. That check is also what
    proves the registration order: with it, instance-before-class parses all
    130,596 real blobs and class-before-instance parses none of them. Without it
    both orders parse everything and merely disagree about which object every
    reference means.
    """

    __slots__ = ("kind", "name", "groups", "data")

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.name = ""
        self.groups: list[list[object]] = []
        self.data: bytes | None = None


class _TypedStream:
    """As much of the NeXT typedstream format as Messages writes.

    **Two reference tables, not one, and both numbered from 0x92.** One holds
    shared strings -- type encodings and class names -- and the other holds
    objects and classes. Real blobs contain the byte pair `\\x98\\x98`, where the
    first byte is a class reference and the second a string reference; they
    resolve to different things. A reader with a single table finds a class where
    the type encoding should be and produces garbage that still parses.

    **An object registers before its class does.** Verified against real blobs:
    the reference numbers only line up if the instance takes its slot first, then
    its class, then that class's superclass. One slot out and every later
    reference in the blob silently resolves to the neighbouring object.

    **The parse must consume the blob exactly.** That is the strongest structural
    check available without a second implementation to compare against: leftover
    bytes mean the grammar and the file disagreed somewhere earlier, so the text
    already read is not trustworthy either. `test_trailing_bytes_are_refused`
    holds it in place.
    """

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._at = 0
        self._strings: list[bytes] = []
        self._objects: list[_Node] = []
        self._depth = 0

    # -- bytes

    def _byte(self) -> int:
        if self._at >= len(self._data):
            raise _Refused("ran off the end")
        value = self._data[self._at]
        self._at += 1
        return value

    def _peek(self) -> int:
        if self._at >= len(self._data):
            raise _Refused("ran off the end")
        return self._data[self._at]

    def _raw(self, count: int) -> bytes:
        if count < 0 or self._at + count > len(self._data):
            raise _Refused("length runs past the end")
        value = self._data[self._at : self._at + count]
        self._at += count
        return value

    def _integer(self) -> int:
        """One signed byte, or a width tag and that many bytes, little endian.

        A NEW/NIL/END tag where an integer belongs means the reader is no longer
        where the grammar thinks it is. Reading 0x84 as the literal -124 and
        carrying on is exactly how a wrong answer stays plausible.
        """
        tag = self._byte()
        width = _INTEGER_TAGS.get(tag)
        if width is not None:
            return int.from_bytes(self._raw(width), "little", signed=True)
        if tag in (_TAG_NEW, _TAG_NIL, _TAG_END):
            raise _Refused("tag where an integer belongs")
        return tag - 256 if tag > 0x7F else tag

    def _ascii(self, raw: bytes | None) -> str:
        if raw is None:
            raise _Refused("no name")
        try:
            return raw.decode("ascii")
        except UnicodeDecodeError:
            raise _Refused("class name is not ascii") from None

    # -- the two tables

    def _reference(self, table: list) -> object:
        index = self._integer() - _FIRST_REFERENCE
        if not 0 <= index < len(table):
            raise _Refused("reference to something never written")
        return table[index]

    def _node_reference(self, kind: str) -> _Node:
        node = self._reference(self._objects)
        if not isinstance(node, _Node) or node.kind != kind:
            raise _Refused(f"reference where a {kind} belongs")
        return node

    def _shared_string(self) -> bytes | None:
        """A type encoding or a class name: written once, referenced after."""
        tag = self._peek()
        if tag == _TAG_NIL:
            self._at += 1
            return None
        if tag != _TAG_NEW:
            value = self._reference(self._strings)
            assert isinstance(value, bytes)
            return value
        self._at += 1
        raw = self._raw(self._integer())
        self._strings.append(raw)
        return raw

    def _class(self) -> _Node | None:
        tag = self._peek()
        if tag == _TAG_NIL:
            self._at += 1
            return None
        if tag != _TAG_NEW:
            return self._node_reference("class")
        self._at += 1
        self._descend()
        node = _Node("class")
        self._objects.append(node)
        name = self._shared_string()
        self._integer()  # the class version, which nothing here needs
        self._class()  # and its superclass, to the root of the chain
        node.name = self._ascii(name)
        self._depth -= 1
        return node

    def _object(self) -> _Node | None:
        tag = self._peek()
        if tag == _TAG_NIL:
            self._at += 1
            return None
        if tag != _TAG_NEW:
            return self._node_reference("object")
        self._at += 1
        self._descend()
        node = _Node("object")
        self._objects.append(node)
        klass = self._class()
        if klass is None:
            raise _Refused("an object with no class")
        node.name = klass.name
        node.groups = self._contents()
        self._byte()  # the END tag _contents stopped in front of
        self._depth -= 1
        return node

    def _descend(self) -> None:
        self._depth += 1
        if self._depth > _MAX_DEPTH:
            raise _Refused("nested deeper than any real message")

    # -- typed values

    def _contents(self) -> list[list[object]]:
        """An object's contents: typed groups until the END tag."""
        groups: list[list[object]] = []
        while self._peek() != _TAG_END:
            groups.append(self._group())
        return groups

    def _group(self) -> list[object]:
        """A type-encoding string, then one value for each type in it."""
        types = self._ascii(self._shared_string())
        values: list[object] = []
        at = 0
        while at < len(types):
            at, value = self._value(types, at)
            values.append(value)
        return values

    def _value(self, types: str, at: int) -> tuple[int, object]:
        code = types[at]
        if code in _INTEGER_TYPES:
            return at + 1, self._integer()
        if code == "+":
            # An inline string: a length and that many bytes, NOT registered.
            # Registering it would shift every later reference in the blob.
            return at + 1, self._raw(self._integer())
        if code in "*:":
            return at + 1, self._c_string()
        if code == "@":
            return at + 1, self._object()
        if code == "#":
            return at + 1, self._class()
        if code == "[":
            return self._array(types, at)
        # A type this parser has never seen is a format it does not understand,
        # and skipping it would mean guessing how many bytes it occupies.
        raise _Refused(f"unknown type encoding {code!r}")

    def _c_string(self) -> bytes | None:
        """A `*` value is a POINTER, so it takes an object-table slot of its own
        and the string it points at takes a separate shared-string slot. Hence
        the doubled `\\x84\\x84` in front of one in a real blob."""
        tag = self._peek()
        if tag == _TAG_NIL:
            self._at += 1
            return None
        if tag != _TAG_NEW:
            return self._node_reference("string").data
        self._at += 1
        node = _Node("string")
        self._objects.append(node)
        node.data = self._shared_string()
        return node.data

    def _array(self, types: str, at: int) -> tuple[int, object]:
        """`[Nc]` and friends. 8,199 real blobs hang a binary plist off a message
        -- a calendar event or a link preview -- and this is how it is stored."""
        at += 1
        digits = at
        while at < len(types) and types[at].isdigit():
            at += 1
        if at == digits or at >= len(types):
            raise _Refused("malformed array type")
        count = int(types[digits:at])
        element = at
        if types[element] in "cC":
            # A byte array is raw bytes, not one encoded value per element. This
            # is the only form that occurs in the real corpus.
            value: object = self._raw(count)
        else:
            items = []
            for _ in range(count):
                after, item = self._value(types, element)
                if after != element + 1:
                    raise _Refused("an array of something this parser cannot size")
                items.append(item)
            value = items
        at = element + 1
        if at >= len(types) or types[at] != "]":
            raise _Refused("unclosed array type")
        return at + 1, value

    # -- the one thing this is for

    def message_text(self) -> str:
        if self._raw(len(_TS_HEADER)) != _TS_HEADER:
            raise _Refused("not a typedstream")
        self._integer()  # the system version: 1000 in every real blob
        values = self._group()
        if self._at != len(self._data):
            raise _Refused("bytes left over")
        if len(values) != 1:
            raise _Refused("the archive holds more than one root")
        root = values[0]
        if not isinstance(root, _Node) or root.name not in _ATTRIBUTED_ROOTS:
            raise _Refused("root is not an attributed string")
        # The backing string is the attributed string's FIRST typed group, by
        # the format, not by looking around for something that resembles text.
        if not root.groups or not root.groups[0]:
            raise _Refused("an attributed string with no string")
        backing = root.groups[0][0]
        if not isinstance(backing, _Node) or backing.name not in _STRING_CLASSES:
            raise _Refused("the backing store is not a string")
        if not backing.groups or not backing.groups[0]:
            raise _Refused("a string with no contents")
        raw = backing.groups[0][0]
        if not isinstance(raw, bytes):
            raise _Refused("the string's contents are not bytes")
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            raise _Refused("the string is not utf-8") from None


def decode_attributed_body(blob: bytes | None) -> str | None:
    """Recover the text of a message archived into `message.attributedBody`.

    Returns None -- never a guess, never a prefix, never a best effort -- when
    anything at all about the blob is not understood. This decodes the user's own
    words and files them permanently as fact, so a decoder that returns
    ROUGHLY the message is worse than one that returns nothing: nothing is
    visible and countable, and roughly-right is neither.

    Measured on the real database, that strictness costs nothing: all 130,596
    blobs decode and 0 are refused. The oracle is the 106,522 rows that carry
    BOTH a `text` column and a blob -- on every single one the decoded string
    equals the column exactly, 100.0%. That is what says the other 24,098, which
    have no column to check against, are being read correctly too; their blobs
    are structurally the same objects, written by the same encoder.
    """
    if not blob:
        return None
    try:
        return _TypedStream(blob).message_text()
    except _Refused:
        return None


# --- reading --------------------------------------------------------------

#: The condition that has always defined "a message with text".
_HAS_TEXT = "m.text is not null and length(trim(m.text)) > 0"

#: `has_text` is computed in SQL rather than re-tested in Python so that the two
#: cannot drift. They already had: SQLite's `trim` strips spaces and Python's
#: `strip` strips all whitespace, which disagree about 7 real rows whose entire
#: `text` column is a newline. Those 7 were read before this change and must be
#: read after it, whatever anyone thinks they should be.
_MESSAGE_COLUMNS = f"m.text, ({_HAS_TEXT}) as has_text, {{body}}, m.date, m.is_from_me"

_CHAT_QUERY = f"""
    select {_MESSAGE_COLUMNS},
           coalesce(ch.chat_identifier, h.id, '') as who
    from message m
    left join (select message_id, min(chat_id) as chat_id
               from chat_message_join group by message_id) j on j.message_id = m.ROWID
    left join chat ch on ch.ROWID = j.chat_id
    left join handle h on m.handle_id = h.ROWID
    where {{where}}
    order by m.date
"""

_HANDLE_QUERY = f"""
    select {_MESSAGE_COLUMNS}, coalesce(h.id, '') as who
    from message m left join handle h on m.handle_id = h.ROWID
    where {{where}}
    order by m.date
"""


def read_messages(
    db_path: str | Path,
    since: datetime | None = None,
    include_attributed: bool = True,
) -> Iterator[Message]:
    """Stream messages that carry text, oldest first, from EITHER column.

    Correspondent identity comes from the CHAT, not from `message.handle_id`.
    That column is 0 for every message the user sent -- measured here, all 37,326
    of them -- because a sent message does not record its recipient. Joining on it
    puts every outgoing message in one nameless bucket, makes every burst
    one-sided, and quietly puts `conf/stated` out of reach: the facts the user
    said in their own words are exactly the ones that go missing.

    Every join is a LEFT join and identity falls back chat -> handle -> empty, so
    a message belonging to no chat is still read rather than silently dropped.
    In the real database 353 messages are in that state; dropping them would be a
    small, invisible hole in a record whose whole premise is that nothing is lost.

    **`include_attributed` defaults to True, and this is the important choice in
    this module.** A message whose text lives only in `attributedBody` is not a
    different KIND of message -- it is an ordinary message stored the way modern
    Messages stores one. Reading it is the same act as reading `text`, and the
    only thing that ever made it a separate question was that nobody had written
    a decoder. Measured on the real database:

    | measure             | text only  | both columns | change |
    |---------------------|-----------:|-------------:|-------:|
    | messages            |    106,498 |      130,556 |  +23%  |
    | characters          |  2,522,825 |    3,241,836 |  +28%  |
    | bursts              |     11,277 |       14,522 |  +29%  |
    | substantive bursts  |      2,386 |        3,054 |  +28%  |

    The cost was expected to be that every burst boundary moves. Measured, it
    barely does: 11,228 of the 11,277 existing bursts keep their exact (handle,
    start, end) span and 49 change, because 3,246 of the 14,522 bursts the fuller
    corpus produces overlap no burst that exists today at all. That is not luck,
    it is the calendar. `text` stops being written in July 2024, so the new rows
    are a later era than the old ones and hardly interleave with them:

    | period    | messages from text | messages from the blob |
    |-----------|-------------------:|-----------------------:|
    | 2020-2024 |            106,202 |                  2,919 |
    | 2025-2026 |                296 |                 21,139 |

    Which is the real argument for the default. With this off, this reader sees
    296 of the 21,435 messages the user exchanged in 2025 and 2026 -- 1.4% of the
    last nineteen months. "Everything that is unread must be read" cannot survive
    a default that silently drops the most recent two years.

    Pass `include_attributed=False` to get back exactly the corpus every earlier
    count in this repository was measured against -- and exactly is the word:
    replayed against the query this function used before the decoder existed, the
    two streams are identical message for message, all 106,498 of them.

    **`text` wins when a row has both.** 106,522 real rows do, and on every one
    the decoder agrees with the column exactly -- so this costs nothing today and
    means that if the two ever disagree it surfaces as a decoder bug rather than
    as a message quietly changing what it said.

    A row whose blob cannot be decoded is not yielded; it stays unread and stays
    visible through `read_textless_messages`, which reports the refusal. Measured,
    there are 0 of those. 40 more decode to nothing but whitespace and are
    dropped for the same reason a blank `text` column is.

    The whole query falls back to the handle join only when the chat tables are
    absent, and to `text` alone when there is no `attributedBody` column, which
    happens on very old or partial databases. Each fallback costs its own
    coverage and nothing else.

    Opened read-only through a URI so this can never modify the user's database.
    """
    conn = _connect(db_path)
    try:
        body = "m.attributedBody" if "attributedBody" in _columns(conn, "message") else "NULL"
        where = _HAS_TEXT if not include_attributed else f"({_HAS_TEXT}) or {body} is not null"
        try:
            rows = conn.execute(_CHAT_QUERY.format(body=body, where=where))
        except sqlite3.Error:
            rows = conn.execute(_HANDLE_QUERY.format(body=body, where=where))
        for text, has_text, blob, raw_date, is_from_me, handle in rows:
            if has_text:
                source = SOURCE_TEXT
            else:
                text = decode_attributed_body(blob)
                if text is None or not text.strip():
                    continue
                source = SOURCE_ATTRIBUTED
            stamp = _decode_timestamp(raw_date or 0)
            if since is not None and stamp <= since:
                continue
            yield Message(
                text=text,
                handle=handle or "",
                timestamp=stamp,
                is_from_me=bool(is_from_me),
                source=source,
            )
    finally:
        conn.close()


def is_junk(text: str) -> bool:
    """True for content that can never carry a fact, whatever surrounds it.

    Deliberately narrow. Short messages are NOT junk -- "ok" can be the answer to
    something that matters, and the burst around it decides. Only content that is
    meaningless in every possible context belongs here.
    """
    if _ATTACHMENT_ONLY.match(text):
        return True
    stripped = text.strip()
    if not stripped:
        return True
    if stripped.lower() in _GAMEPIGEON:
        return True
    if _TAPBACK.match(stripped):
        return True
    if not any(unicodedata.category(c).startswith(("L", "N")) for c in stripped):
        return True
    return False


def segment(messages: Iterable[Message], gap_seconds: int = 1800) -> Iterator[Burst]:
    """Group messages into per-correspondent bursts.

    A burst is a stretch of conversation with ONE person, so this groups by
    correspondent before it looks at time. It does not assume the caller sorted:
    `read_messages` streams in date order, which interleaves conversations, and
    treating a correspondent change as a burst boundary shredded 19,906 real
    bursts into 64,489 fragments -- taking the substantive count from 2,343 to 77.

    Grouping means buffering, so this is not streaming. At ~106k messages that is
    a few tens of MB and fine; a corpus large enough to matter would need
    per-correspondent chunking on the way in instead.
    """
    by_handle: dict[str, list[Message]] = {}
    for message in messages:
        by_handle.setdefault(message.handle, []).append(message)

    for handle, group in by_handle.items():
        group.sort(key=lambda m: m.timestamp)
        current: list[Message] = []
        for message in group:
            if current and (
                (message.timestamp - current[-1].timestamp).total_seconds() > gap_seconds
            ):
                yield Burst(handle=handle, messages=tuple(current))
                current = []
            current.append(message)
        if current:
            yield Burst(handle=handle, messages=tuple(current))


def is_substantive(burst: Burst, min_chars: int = 200, min_messages: int = 6) -> bool:
    """Whether a burst is worth an extraction pass.

    BOTH thresholds must hold. Length alone admits a single pasted block, which is
    not a conversation; message count alone admits forty consecutive "ya"s.
    """
    return burst.char_count >= min_chars and len(burst.messages) >= min_messages


# --- contacts -------------------------------------------------------------


def _normalise_handle(raw: str) -> str | None:
    """Fold a phone number or address into the form message handles use."""
    value = raw.strip()
    if not value:
        return None
    if "@" in value:
        return value.lower()
    digits = re.sub(r"\D", "", value)
    if len(digits) < 10:
        return None
    return "+1" + digits[-10:]


def _read_vcards(path: Path) -> Iterator[tuple[str, str]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    for card in text.split("BEGIN:VCARD"):
        name = re.search(r"^FN:(.+)$", card, re.MULTILINE)
        if not name:
            continue
        display = name.group(1).strip()
        if not display:
            continue
        for raw in re.findall(r"^(?:TEL|EMAIL)[^:]*:(.+)$", card, re.MULTILINE):
            handle = _normalise_handle(raw)
            if handle:
                yield handle, display


def _read_address_book(path: Path) -> Iterator[tuple[str, str]]:
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    try:
        queries = (
            "select r.ZFIRSTNAME, r.ZLASTNAME, p.ZFULLNUMBER from ZABCDRECORD r "
            "join ZABCDPHONENUMBER p on p.ZOWNER = r.Z_PK",
            "select r.ZFIRSTNAME, r.ZLASTNAME, e.ZADDRESS from ZABCDRECORD r "
            "join ZABCDEMAILADDRESS e on e.ZOWNER = r.Z_PK",
        )
        for query in queries:
            try:
                rows = conn.execute(query).fetchall()
            except sqlite3.Error:
                continue  # a source without that table is normal, not an error
            for first, last, raw in rows:
                display = " ".join(p for p in (first, last) if p).strip()
                handle = _normalise_handle(raw or "")
                if display and handle:
                    yield handle, display
    finally:
        conn.close()


def resolve_contacts(
    vcf_paths: Iterable[str | Path], addressbook_paths: Iterable[str | Path]
) -> dict[str, str]:
    """Map normalised handles to display names, merging every available source.

    Both sources are needed and neither is sufficient. On this machine the Google
    export holds the larger list while the local address book holds family --
    measured, the merge is what takes busiest-thread resolution from 8/15 to 14/15.

    A missing or unreadable source is skipped rather than fatal: contacts are an
    enrichment, and losing a name must never stop a fact being filed.
    """
    resolved: dict[str, str] = {}
    for raw_path in vcf_paths:
        path = Path(raw_path)
        if not path.exists():
            continue
        try:
            for handle, name in _read_vcards(path):
                resolved.setdefault(handle, name)
        except OSError:
            continue
    for raw_path in addressbook_paths:
        path = Path(raw_path)
        if not path.exists():
            continue
        try:
            for handle, name in _read_address_book(path):
                resolved.setdefault(handle, name)
        except sqlite3.Error:
            continue
    return resolved


# --- attachments ----------------------------------------------------------


def _connect(db_path: str | Path) -> sqlite3.Connection:
    """Read-only through a URI, exactly as `read_messages` does.

    Messages.app is usually running while this reads. `mode=ro` is what makes
    that safe: the file is the user's live message history and there is no
    version of "oops" here that is recoverable.
    """
    return sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True)


def _tables(conn: sqlite3.Connection) -> set[str]:
    try:
        return {
            row[0] for row in conn.execute("select name from sqlite_master where type='table'")
        }
    except sqlite3.Error:
        return set()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {row[1] for row in conn.execute(f"pragma table_info({table})")}
    except sqlite3.Error:
        return set()


@dataclass(frozen=True)
class Attachment:
    """One file that crossed a conversation. Its metadata, never its contents.

    `size_on_disk` is None exactly when `present` is False, so a file iCloud has
    offloaded is distinguishable from one that is here -- which matters, because
    "they sent a 4MB video that day" is still biography when the video is gone.
    """

    handle: str
    filename: str
    transfer_name: str
    mime_type: str
    uti: str
    total_bytes: int
    timestamp: datetime | None
    is_from_me: bool
    is_sticker: bool
    present: bool
    size_on_disk: int | None

    @property
    def missing(self) -> bool:
        return not self.present

    @property
    def is_plugin_payload(self) -> bool:
        """An app's private blob rather than something a person chose to send.

        10,985 of the 19,266 real rows -- 57% -- are these: no mime type, a
        dynamic UTI, and a `.pluginpayloadattachment` filename. They are the
        working files of link previews, GamePigeon and similar. Counting them as
        "attachments the user sent" would treat app plumbing as a life event.
        """
        return not self.mime_type and self.uti.startswith("dyn.")


def _stat_size(filename: str) -> tuple[bool, int | None]:
    """Whether the file is here, and how big -- by `os.stat`, never by opening it.

    `~` has to be expanded: every filename in the real table is stored as
    `~/Library/Messages/Attachments/...`, so testing the literal string reports
    all 16,305 present files as missing, and reports it silently.
    """
    if not filename:
        return False, None
    try:
        return True, os.stat(os.path.expanduser(filename)).st_size
    except OSError:
        # Missing, offloaded, on an unmounted volume, or not readable. All of
        # them mean the same thing to a caller and none of them is an error.
        return False, None


def _as_seconds(column: str) -> str:
    """Normalise a raw Apple date to seconds inside SQL, for ORDER BY only.

    The yielded value is always decoded by `_decode_timestamp`. This exists
    because the two date columns involved are in two different units in the SAME
    file -- `message.date` in nanoseconds, all 19,266 `attachment.created_date`
    values in seconds -- so sorting them against each other raw interleaves 2001
    with 2026. The threshold is the module's constant, not a second copy of it.
    """
    return (
        f"(case when abs({column}) >= {_NANOSECOND_THRESHOLD} "
        f"then {column} / 1000000000.0 else {column} end)"
    )


_ATTACHMENT_COLUMNS = """
    a.filename, a.transfer_name, a.mime_type, a.uti, a.total_bytes,
    a.created_date, a.is_outgoing, a.is_sticker, m.ROWID, m.date, m.is_from_me
"""

_ATTACHMENT_ORDER = (
    f"order by coalesce({_as_seconds('m.date')}, {_as_seconds('a.created_date')}), a.ROWID"
)

_ATTACHMENT_QUERY = f"""
    select {_ATTACHMENT_COLUMNS},
           coalesce(ch.chat_identifier, h.id, '') as who
    from attachment a
    left join message_attachment_join j on j.attachment_id = a.ROWID
    left join message m on m.ROWID = j.message_id
    left join (select message_id, min(chat_id) as chat_id
               from chat_message_join group by message_id) cj on cj.message_id = m.ROWID
    left join chat ch on ch.ROWID = cj.chat_id
    left join handle h on m.handle_id = h.ROWID
    {_ATTACHMENT_ORDER}
"""

_ATTACHMENT_HANDLE_QUERY = f"""
    select {_ATTACHMENT_COLUMNS}, coalesce(h.id, '') as who
    from attachment a
    left join message_attachment_join j on j.attachment_id = a.ROWID
    left join message m on m.ROWID = j.message_id
    left join handle h on m.handle_id = h.ROWID
    {_ATTACHMENT_ORDER}
"""


def read_attachments(
    db_path: str | Path, since: datetime | None = None
) -> Iterator[Attachment]:
    """Stream attachment METADATA, oldest first. Never a byte of any file.

    A photograph sent to a named person on a dated day is biography; the image
    is not, and this module has no code that could open one. `_stat_size` calls
    `os.stat` and nothing else, which is what lets this be pointed at 9.0GB of
    photographs and video without a pixel of it reaching the vault.

    **Direction comes from the message, never from `attachment.is_outgoing`.**
    That column is the same class of trap as `message.handle_id`: it exists, it
    is populated, and it is wrong. Cross-tabulated against `message.is_from_me`
    on the real database it disagrees on 4,994 of 16,309 linked rows -- 31%,
    both ways. Believing it would attribute a third of the user's photographs to
    the wrong person, which is worse than not reading them at all.

    **The correspondent comes from the CHAT, never from `handle_id`** -- for the
    reason `read_messages` documents, and it bites here too: 8,087 of the 8,468
    sent messages that carry an attachment have `handle_id = 0`.

    **The date is the message's, not `attachment.created_date`.** That column is
    when the FILE record appeared on this machine, which is a different event: it
    is later than its message for 5,147 real rows, more than a day later for
    1,003 of them, and up to 715 days later where iCloud re-downloaded something.
    `created_date` is only used for an attachment that has no message, where it
    is the only date there is.

    **Attachments with no message are still read.** 2,960 of the 19,266 have no
    row in `message_attachment_join`. They have no correspondent, so `handle` is
    empty -- and for those, and only those, `is_from_me` falls back to
    `is_outgoing`, the unreliable column, because nothing better exists. An empty
    `handle` is the marker that says the direction is a guess.

    One record per (attachment, message) pair: 3 real attachments hang off two
    messages each, so the 19,266 rows are 19,269 records before redaction and
    19,243 after.

    Names are user data and go through `redact.scan_hard`. A hard hit drops the
    record whole, which is `redact.py`'s contract -- a filename someone typed a
    key into is exactly how a live credential reaches a git repository. Measured,
    that costs 26 of the 19,266 (0.13%), every one of them a false positive: they
    are device-generated names -- 20 iOS screen recordings, the rest camera and
    screenshot files -- long enough and mixed enough that `high_entropy_blob`
    reads the stem as a pasted key. Losing 26 filenames is the cheaper mistake,
    so this does not special-case them; a narrower rule belongs in `redact.py`.

    Falls back to the handle join only when the chat tables are absent, and
    yields nothing at all when the attachment tables are: an old or partial
    database costs coverage, not an exception.
    """
    conn = _connect(db_path)
    try:
        if not {"attachment", "message_attachment_join"} <= _tables(conn):
            return
        try:
            rows = conn.execute(_ATTACHMENT_QUERY)
        except sqlite3.Error:
            rows = conn.execute(_ATTACHMENT_HANDLE_QUERY)
        for row in rows:
            (
                filename, transfer_name, mime_type, uti, total_bytes,
                created_date, is_outgoing, is_sticker,
                message_rowid, message_date, is_from_me, who,
            ) = row

            raw_date = message_date if message_date else created_date
            stamp = _decode_timestamp(raw_date) if raw_date else None
            if since is not None and (stamp is None or stamp <= since):
                continue

            filename = filename or ""
            transfer_name = transfer_name or ""
            if scan_hard(f"{filename}\n{transfer_name}"):
                continue

            present, size_on_disk = _stat_size(filename)
            yield Attachment(
                handle=who or "",
                filename=filename,
                transfer_name=transfer_name,
                mime_type=mime_type or "",
                uti=uti or "",
                total_bytes=int(total_bytes or 0),
                timestamp=stamp,
                # Keyed off the message's existence, not its date: a message with
                # no usable date is still the authority on who sent it.
                is_from_me=bool(is_from_me if message_rowid is not None else is_outgoing),
                is_sticker=bool(is_sticker),
                present=present,
                size_on_disk=size_on_disk,
            )
    finally:
        conn.close()


# --- the text-less rows ---------------------------------------------------

#: Every label `read_textless_messages` can produce. Ordered by the precedence
#: it applies, which is why "attachment" is first: see that function.
TEXTLESS_KINDS: tuple[str, ...] = (
    "attachment",
    "tapback",
    "app",
    "event",
    "attributed_only",
    "empty",
)


@dataclass(frozen=True)
class TextlessMessage:
    """A message row with nothing in its `text` column, and what it turned out to be.

    `text` carries what `decode_attributed_body` recovered from the row's blob,
    or None. It used to carry nothing at all, on the grounds that `has_attributed_
    body` should say that text exists without saying what it says -- but that was
    written when no decoder existed. Now that one does, the pair is the audit that
    matters: `has_attributed_body and text is None` is a REFUSAL, a row this module
    knows holds words and declines to read. That number has to be countable by
    someone other than the function that produced it, and this is where it lives.
    """

    handle: str
    timestamp: datetime
    is_from_me: bool
    kind: str
    attachments: int
    has_attributed_body: bool
    #: The recovered text, or None for no blob or a blob that was refused.
    text: str | None = None


def _textless_kind(
    attachments: int, associated: int, balloon: str | None, item_type: int, body: bool
) -> str:
    """Label one row. Precedence, not a hierarchy of importance.

    "attachment" wins because whether a file crossed the conversation is the
    question actually being asked, and 246 real rows are both an app balloon and
    an attachment while 14 are both a tapback and an attachment. Labelling those
    "app" would hide 260 real files behind a plumbing label.
    """
    if attachments:
        return "attachment"
    if associated:
        return "tapback"
    if balloon:
        return "app"
    if item_type:
        return "event"
    if body:
        return "attributed_only"
    return "empty"


def read_textless_messages(
    db_path: str | Path, since: datetime | None = None
) -> Iterator[TextlessMessage]:
    """Stream the rows with no `text` column, oldest first, saying what each is.

    28,758 of the 135,256 real rows -- 21% -- have a null or blank `text`. This is
    the audit of that column, not the reading path: `read_messages` now reads most
    of these rows through `decode_attributed_body`, and this function is how you
    check what it read and what it did not. Labelled by the precedence in
    `_textless_kind`, and with the text their blobs give up:

    | kind              | rows   | decoded | chars   | what it is                 |
    |-------------------|-------:|--------:|--------:|----------------------------|
    | `attributed_only` | 22,330 |  22,330 | 652,692 | an ordinary message        |
    | `event`           |  4,613 |       0 |       0 | joined, left, renamed      |
    | `attachment`      |    937 |     930 |  19,711 | a file and nothing else    |
    | `tapback`         |    800 |     800 |  45,354 | liked / loved / emphasised |
    | `app`             |     39 |      38 |   1,279 | GamePigeon, link previews  |
    | `empty`           |     39 |       0 |       0 | genuinely nothing          |

    **Only 937 of the 28,758 have an attachment.** So the old SQL filter had NOT
    been quietly discarding the attachment corpus: of the 16,309 rows in
    `message_attachment_join`, 14,879 hang off messages that carry text and have
    been read all along, and 1,430 hang off these 937.

    **`has_attributed_body and text is None` is a refusal** -- a row known to hold
    words that `decode_attributed_body` declined to read rather than guess at.
    That is the number this module owes the user, and it is 0 across all 24,098
    rows that carry a blob. Counting it here rather than inside `read_messages`
    keeps it verifiable by something other than the code that produced it.

    Note the `tapback` and `attachment` rows: their blobs decode to real strings,
    but they are 'Liked "..."' and the U+FFFC attachment placeholder, which
    `is_junk` has always thrown away. Decoding them changes nothing downstream; it
    just means the audit no longer has to describe them as unreadable.

    Every optional column is detected rather than assumed, because an older
    database has no `item_type`, no `balloon_bundle_id`, no `attributedBody` and
    no attachment tables. Each missing one costs its own field and nothing else.
    """
    conn = _connect(db_path)
    try:
        available = _columns(conn, "message")
        if not available:
            return

        def column(name: str, absent: str) -> str:
            return f"m.{name}" if name in available else absent

        attachments = (
            "(select count(*) from message_attachment_join j where j.message_id = m.ROWID)"
            if "message_attachment_join" in _tables(conn)
            else "0"
        )
        selected = f"""
            m.date, m.is_from_me,
            {attachments},
            {column("associated_message_type", "0")},
            {column("balloon_bundle_id", "NULL")},
            {column("item_type", "0")},
            {column("attributedBody", "NULL")}
        """
        where = "where m.text is null or length(trim(m.text)) = 0"
        query = f"""
            select {selected}, coalesce(ch.chat_identifier, h.id, '') as who
            from message m
            left join (select message_id, min(chat_id) as chat_id
                       from chat_message_join group by message_id) j on j.message_id = m.ROWID
            left join chat ch on ch.ROWID = j.chat_id
            left join handle h on m.handle_id = h.ROWID
            {where} order by m.date
        """
        fallback = f"""
            select {selected}, coalesce(h.id, '') as who
            from message m left join handle h on m.handle_id = h.ROWID
            {where} order by m.date
        """
        try:
            rows = conn.execute(query)
        except sqlite3.Error:
            rows = conn.execute(fallback)

        for raw_date, is_from_me, count, associated, balloon, item_type, body, who in rows:
            stamp = _decode_timestamp(raw_date or 0)
            if since is not None and stamp <= since:
                continue
            yield TextlessMessage(
                handle=who or "",
                timestamp=stamp,
                is_from_me=bool(is_from_me),
                kind=_textless_kind(
                    int(count or 0), int(associated or 0), balloon, int(item_type or 0),
                    body is not None,
                ),
                attachments=int(count or 0),
                has_attributed_body=body is not None,
                text=decode_attributed_body(body),
            )
    finally:
        conn.close()
