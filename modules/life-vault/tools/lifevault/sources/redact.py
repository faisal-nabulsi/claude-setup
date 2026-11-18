"""The secret gate: find credentials in text before anything is filed.

Transcripts, messages and mail contain pasted API keys, `.env` dumps, tokens and
private keys. A fact filed carrying one of those is a permanent leak into a git
repository, and the vault's whole premise is that what goes in stays in.

**This is a gate, not a cleaner.** The intended use is `is_safe(text)` followed by
rejecting the whole record — not filing a redacted version. A redacted secret
still proves the secret existed, still records when and where it was said, and
usually leaves enough context to identify which credential it was. `redact()`
exists for the cases where a record must be kept anyway; it is the exception.

**A rejection must never itself leak.** `describe()` produces a log line carrying
kinds and offsets only. A log of the things that were too dangerous to file must
not become the leak.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

SECRET_KINDS: tuple[str, ...] = (
    "anthropic_api_key",
    "openai_api_key",
    "aws_access_key_id",
    "aws_secret_access_key",
    "github_token",
    "slack_token",
    "google_api_key",
    "jwt",
    "private_key",
    "database_url",
    "vendor_api_key",
    "secret_assignment",
    "high_entropy_blob",
)


@dataclass(frozen=True)
class Secret:
    """A detected credential, located but never quoted."""

    kind: str
    start: int
    end: int


# --- placeholders this module emits are inert on a second pass --------------
# Only the kinds we actually emit count. `[REDACTED:sk-ant-...]` is a fabricated
# wrapper around a live key, not a placeholder, and must still be caught.
_PLACEHOLDER = re.compile(r"\[REDACTED:(?:" + "|".join(SECRET_KINDS) + r")\]")

# Words that make a nearby high-entropy blob suspicious. Deliberately short:
# every addition here is a new source of false positives.
_HINT = re.compile(
    r"(?i)\b(?:api|key|keys|token|tokens|secret|secrets|password|passwd|pwd|"
    r"credential|credentials|auth|bearer)\b"
)
_HINT_WINDOW = 48

_SECRET_WORD = (
    r"(?:api[_-]?key|access[_-]?key|secret[_-]?key|client[_-]?secret|"
    r"auth[_-]?token|access[_-]?token|refresh[_-]?token|"
    r"key|token|secret|password|passwd|pwd|credentials|credential)"
)

# A value that is obviously a placeholder rather than a credential.
_PLACEHOLDER_VALUE = re.compile(
    r"(?i)^(?:<.*>|\{\{?.*\}?\}|changeme|change_me|password|secret|token|"
    r"your[_-].*|example|placeholder|redacted|none|null|true|false)$"
)

_DETECTORS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Anthropic first: its prefix also satisfies the OpenAI shape.
    ("anthropic_api_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai_api_key", re.compile(r"\bsk-(?!ant-)(?:proj-)?[A-Za-z0-9_-]{20,}")),
    ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    (
        "github_token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{22,})\b"),
    ),
    ("slack_token", re.compile(r"\bxox[baprse]-[A-Za-z0-9-]{10,}")),
    ("google_api_key", re.compile(r"\bAIza[A-Za-z0-9_-]{35}\b")),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    ),
    (
        "private_key",
        re.compile(
            r"-----BEGIN (?:[A-Z0-9 ]*)PRIVATE KEY-----"
            r"[\s\S]*?(?:-----END (?:[A-Z0-9 ]*)PRIVATE KEY-----|\Z)"
        ),
    ),
    (
        "vendor_api_key",
        re.compile(
            r"\b(?:wandb_(?:v1_)?[A-Za-z0-9]{20,}|hf_[A-Za-z0-9]{30,}|"
            r"glpat-[A-Za-z0-9_-]{20,}|npm_[A-Za-z0-9]{30,}|dop_v1_[a-f0-9]{60,}|"
            r"(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{20,}|shpat_[a-f0-9]{32}|"
            r"SG\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}|"
            r"dckr_pat_[A-Za-z0-9_-]{20,}|figd_[A-Za-z0-9_-]{30,})\b"
        ),
    ),
    (
        "database_url",
        re.compile(
            r"\b(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis|amqp)"
            r"://[^\s:@/]+:[^\s@/]+@\S+"
        ),
    ),
)

_ASSIGNMENT = re.compile(
    r"(?<![A-Za-z0-9])(?:[A-Za-z0-9]+[._-])*" + _SECRET_WORD + r"\s*[:=]\s*"
    r"(?P<v>\"[^\"\n]*\"|'[^'\n]*'|[^\s\"'\n]+)",
    re.IGNORECASE,
)

# 24, not 32: a real pasted key is often shorter than a git sha. False
# positives are held off by the mixed-case + digit + hint-word guards in
# _blob_is_suspicious, not by length.
_BLOB = re.compile(r"[A-Za-z0-9+/=_-]{24,}")


def _inert_spans(text: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in _PLACEHOLDER.finditer(text)]


def _overlaps(a: tuple[int, int], spans: list[tuple[int, int]]) -> bool:
    return any(a[0] < e and s < a[1] for s, e in spans)


def _value_is_credential_shaped(raw: str) -> bool:
    """Reject documentation placeholders and values too short to be a secret."""
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    value = value.strip()
    if len(value) < 6:
        return False
    if _PLACEHOLDER_VALUE.match(value):
        return False
    if len(set(value)) <= 2:  # xxxxxxxxxxxx, ------------
        return False
    return True


def _blob_is_suspicious(text: str, start: int, end: int, inert: list[tuple[int, int]]) -> bool:
    """A long run of base64-ish characters is only evidence when something nearby says so.

    Requiring BOTH mixed case and a digit is what keeps git shas and undashed
    UUIDs out: they are lowercase hex, so they never qualify however long they get.
    """
    blob = text[start:end]
    if not (any(c.islower() for c in blob) and any(c.isupper() for c in blob)):
        return False
    if not any(c.isdigit() for c in blob):
        return False

    # When the secret IS the entire message there is no context to hint from,
    # and a bare pasted key is one of the commonest ways one leaks. So a
    # high-entropy run standing essentially alone is suspicious on its own.
    # The mixed-case-and-digit requirement above still excludes git shas and
    # UUIDs, which are lowercase hex however long they get.
    remainder = (text[:start] + text[end:]).strip()
    if len("".join(remainder.split())) <= 8 and (end - start) >= 24:
        return True

    left, right = max(0, start - _HINT_WINDOW), min(len(text), end + _HINT_WINDOW)
    window = text[left:start] + " " + text[end:right]
    # A placeholder we emitted contains the word "key"; it must not be read as a
    # hint on the next pass, or redaction would stop being idempotent.
    for s, e in inert:
        if s < right and left < e:
            window = window.replace(text[s:e], " ")
    return bool(_HINT.search(window))


# `secret_assignment` and `high_entropy_blob` are the shape-blind fallbacks: they
# fire on anything that merely looks credential-ish. When one of them lands on the
# same text as a detector that knows what it is looking at, the specific answer is
# the useful one -- "aws_secret_access_key" tells the reader what to rotate.
_GENERIC_KINDS = frozenset({"secret_assignment", "high_entropy_blob"})


def _specificity(kind: str) -> int:
    return 1 if kind in _GENERIC_KINDS else 0


def _merge(found: list[Secret]) -> list[Secret]:
    """Collapse overlapping detections into one covering span.

    Redacting an inner match on its own would leave the rest of an enclosing
    secret in the clear -- the JWT inside `PASSWORD="prefix-<jwt>-suffix"` is the
    obvious case. The span is always the union; the kind is the most specific one.
    """
    if not found:
        return []
    ordered = sorted(
        found, key=lambda s: (s.start, -(s.end - s.start), _specificity(s.kind))
    )
    merged = [ordered[0]]
    for item in ordered[1:]:
        last = merged[-1]
        if item.start < last.end:
            best = min(
                (last, item), key=lambda s: (_specificity(s.kind), -(s.end - s.start))
            )
            merged[-1] = Secret(
                kind=best.kind, start=last.start, end=max(last.end, item.end)
            )
        else:
            merged.append(item)
    return merged


def find_secrets(text: str) -> list[Secret]:
    """Locate every credential in `text`, ordered by position, never overlapping."""
    if not text:
        return []

    inert = _inert_spans(text)
    found: list[Secret] = []

    for kind, pattern in _DETECTORS:
        for m in pattern.finditer(text):
            if not _overlaps((m.start(), m.end()), inert):
                found.append(Secret(kind=kind, start=m.start(), end=m.end()))

    for m in _ASSIGNMENT.finditer(text):
        span = (m.start("v"), m.end("v"))
        if _overlaps(span, inert):
            continue
        if _value_is_credential_shaped(m.group("v")):
            found.append(Secret(kind="secret_assignment", start=span[0], end=span[1]))

    # AWS secret keys have no distinctive shape, only a length, so they need a
    # nearby "aws" to be evidence of anything at all.
    for m in re.finditer(r"[A-Za-z0-9/+=]{40}", text):
        span = (m.start(), m.end())
        if _overlaps(span, inert):
            continue
        left, right = max(0, span[0] - _HINT_WINDOW), min(len(text), span[1] + _HINT_WINDOW)
        if re.search(r"(?i)\baws\b|aws_secret|aws_access", text[left:right]):
            found.append(Secret(kind="aws_secret_access_key", start=span[0], end=span[1]))

    for m in _BLOB.finditer(text):
        span = (m.start(), m.end())
        if _overlaps(span, inert):
            continue
        if _blob_is_suspicious(text, span[0], span[1], inert):
            found.append(Secret(kind="high_entropy_blob", start=span[0], end=span[1]))

    return _merge(found)


def is_safe(text: str) -> bool:
    """True when nothing in `text` looks like a credential."""
    return not find_secrets(text)


def redact(text: str) -> tuple[str, list[Secret]]:
    """Replace every credential with `[REDACTED:<kind>]`.

    Prefer rejecting the record. See this module's docstring for why.
    """
    found = find_secrets(text)
    out = text
    for secret in reversed(found):
        out = out[: secret.start] + f"[REDACTED:{secret.kind}]" + out[secret.end :]
    return out, found


def describe(secrets: list[Secret]) -> str:
    """A one-line log record of what was found: kinds and offsets, never text."""
    if not secrets:
        return "no secrets detected"
    parts = ", ".join(f"{s.kind}@{s.start}-{s.end}" for s in secrets)
    return f"{len(secrets)} secret(s) detected: {parts}"


# --- the soft tier --------------------------------------------------------
#
# Two tiers, because two different failures need two different answers.
#
# A CREDENTIAL is contagious: it grants access, so the only safe response is to
# drop the whole record. `scan_hard` finds those.
#
# An SSN or a diagnosis is not contagious but is private. Dropping the record
# would lose a real fact about the user's life -- "I was diagnosed with lupus in
# March" is exactly the kind of thing this vault exists to remember. So the
# identifier is masked in place and the record is FLAGGED, which routes it to
# sens/private under spec §10 rather than deleting it.

# A phone number has to be recognised by its SEPARATORS, not by its length.
# Ten consecutive digits is not a distinctive shape: measured over the 274,414
# records of the real Google Takeout export, a bare ten-digit run appears in 991
# of them -- 949 Chrome history URLs, 40 autofill values and two others -- and
# the great majority are unix timestamps and opaque ids in query strings. So the
# free-standing branches require either a leading `+` or punctuation between the
# groups.
#
# Which would leave real numbers in the clear, so there is a third branch, and
# it is label-gated for the same reason the IP pattern below is: when the digits
# say nothing, the field name around them does. Measured, what it actually earns
# is the `tel:` URI -- the standard click-to-call spelling in any HTML mail or
# page -- and it earns it 45 times in the 44,574-message mailbox that the other
# two branches miss entirely: `tel:5550100123`, `tel:15550100123`,
# `telephone:5550100123`. It picks up none of the 949 timestamp-shaped runs in
# Chrome history, because a URL path separator is not one it accepts.
#
# On the Takeout export it earns nothing, and the reason is worth knowing before
# trusting a label anywhere else: a Chrome autofill entry stores `MobilePhone` as
# the record's TITLE and the ten digits as its DETAIL, and `takeout._gate` masks
# title, detail and url as three separate strings. A label in one field cannot
# reach a value in another, so no pattern written here can protect those.
#
# The `.` separator is worth its risk and the measurement says so. `555.010.0123`
# is standard US business style: 1,182 occurrences across 310 messages in the
# 44,574-message mailbox, and it costs nothing -- allowing `.` added zero matches
# to the 274,414 Takeout records and zero to the 130,560 iMessage messages,
# because a dotted quad needs a four-digit final group to be phone-shaped and no
# IP address or version string has one.
#
# The lookbehind is load-bearing. `10.123.456.7890` contains `123.456.7890`;
# without `(?<![\w.])` the pattern reaches into a longer dotted run and masks
# three of its four parts, which corrupts the value without protecting anything.
#
# The label-gated branch allows an optional country code and ONE separator
# before the ten digits, and the number is why. `+1 5550100123` -- a country
# code, a space, then ten unpunctuated digits -- defeated all three branches:
# E.164 wants the digits hard against the plus, the punctuated NANP branch wants
# separators INSIDE the ten digits, and this branch allowed nothing between the
# plus and the digits. Measured as a DELTA against the pattern without it, over
# 220,484 Takeout records, 44,574 mail messages and 130,564 iMessage messages:
# it adds exactly ONE match in the whole corpus, and that match is a real phone
# number -- a Chrome autofill value under a form field named `phone-<uuid>`.
# Zero false positives.
#
# Widening the general E.164 branch to `\+\d{1,3}[ .-]\d{10}` was measured
# instead and rejected. It adds 5: the same record plus one Indian number in an
# email signature quoted at four nesting depths. Masking that would be right
# rather than wrong, but `mail.py` never calls `mask_soft` -- the callers are
# takeout, activity, gchat, photos and harvest -- so those 4 are unreachable and
# the broader branch buys nothing the narrow one does not, while carrying every
# future `+CC ` shaped id that is not a telephone.
#
# The false positive this pattern DOES have, priced and accepted: a Google Ads
# account id is written `243-039-7011`, which is a US phone number in every
# respect except what it means. Across the 175,175 `activity.py` records it
# accounts for 31 of the 93 matches, all of them inside `g_acctid=` or `acnt=`
# in a marketing URL. Refusing `NNN-NNN-NNNN` to dodge it would be the wrong
# trade by an order of magnitude -- that spelling is 62 of the same 93, 921
# occurrences in the 44,574-message mailbox, and the commonest way anyone writes
# a number down -- and the thing wrongly masked is an ad tracking id nothing
# reads, inside a URL that is already a tracking artefact.
_PHONE = (
    r"(?<![\w.])(?:"
    r"\+\d{10,15}"  # E.164, exactly how Google exports it
    r"|(?:\+?1[ .-]?)?(?:\(\d{3}\)|\d{3})[ .-]\d{3}[ .-]\d{4}"  # NANP, punctuated
    r")(?!\w)"
    # unpunctuated, and only where the field it came out of names it
    r"|(?i:[\w.\[\]-]*phone[\w.\[\]-]*|\btel)(?:\s*[:=]\s*|\s+)(?:\+\d{1,3}[ .-]?)?\d{10,11}(?!\w)"
)

# An IP address IS masked, but only where something says it is one, and the
# label is the whole detector -- the value pattern below is deliberately
# permissive because the label has already established what it is looking at.
# This is the same bargain `passport` and `routing` already make: neither
# validates its value either.
#
# The reason is that a dotted quad has no distinctive shape whatsoever. Measured
# over the real export: range-checking every octet and guarding the run
# boundaries still matches 160,662 of 274,414 records -- 58% of the corpus --
# because all 159,351 access-log rows carry `App Version: 131.0.6778.85`. Of the
# 1,179 that survive a `(?<![\w./])...(?![\w.])` guard, 433 are the single
# literal `1.0.1.1` sitting inside a tracking-URL id. A bare pattern would mangle
# a hundred and sixty thousand records to protect seven hundred and thirty nine.
#
# IPv6 alone COULD be masked bare -- a strict pattern hits 1,052 records and
# every one is a real address -- but v4 and v6 arrive in the same column of the
# same login table, and a log that masked half its rows by protocol would read
# as a bug and still leak the other half. One data type, one decision.
#
# What the label buys, beyond precision: it is also what excludes clock times. A
# loose IPv6 pattern reads `04:16:14` as an address, and one real
# `SubscriberInfo.html` holds 1,072 of them, one per login row.
_IP_VALUE = (
    r"(?:\d{1,3}(?:\.\d{1,3}){3}"
    r"|[0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7}(?:\.\d{1,3}){0,3})"
)
_IP = (
    r"(?i)\bip(?:v[46])?(?:[ _-]?address(?:es)?)?"
    r"(?:\s*[:=]\s*|\s+)" + _IP_VALUE + r"(?!\w)"
)

# A card number is 13 to 19 digits and nothing else, and that is not a shape. A
# unix millisecond timestamp is 13 digits; a Discord snowflake, a Google Play
# track id and a Google place id are 19; a Chrome history URL is made of them.
# The pattern that asked only for 13-to-18 digits between word boundaries --
# `\b(?:\d[ -]?){13,18}\b` -- hit 3,373 records: 1,083 of the 274,414 Takeout
# records, 2,288 of the 175,175 `activity.py` records and 2 of the 1,760 Chat
# messages. NOT ONE of those 3,373 was a card. There is in fact no card number
# anywhere in this corpus in any spelling; the detector was pure noise. Masking a
# timestamp and filing the record as a found credit card is worse than missing
# one: it is a false positive wearing the uniform of the detector working, and a
# reader who chases it has no way to tell it from a real hit.
#
# So the detector is two branches, priced separately against the same corpus:
# the 451,435 records that actually reach `mask_soft` -- 274,414 Takeout, 175,175
# activity, 1,760 Chat, 86 Photos. Together they match zero of them.
#
# GROUPED is the shape branch: four-by-four, or American Express's four-six-five.
# Measured over all 451,435, it matches ZERO records. Groups of four digits
# separated by a single space or hyphen, with the run bounded at both ends, is a
# genuinely distinctive shape in a way a bare digit run never is, so this branch
# is free and it is the spelling anyone writing a card down by hand uses. It
# validates nothing, for the same reason `ssn` validates nothing: no timestamp,
# id or version string in the corpus is written this way, so there is nothing for
# a checksum to rule out, and requiring one would only lose a card with a typo.
#
# The guards are load-bearing in both directions. Leading `(?<![\w.-])` keeps the
# pattern from carving four-digit groups out of a longer run -- a UUID whose
# middle groups happen to be all digits is the case that actually occurs, in a
# Reddit search URL that appears in both the Takeout and the activity copies of
# the same visit, and its four inner groups are exactly card-shaped. Trailing
# `(?![\w-])` deliberately does NOT exclude `.`, because a card at the end of a
# sentence is followed by a full stop. And no branch here may END in an optional
# separator: the old pattern's trailing `[ -]?` swallowed the space after the
# number, so `card <number> expires soon` was filed as `[REDACTED:card]expires
# soon`, with the words run together.
#
# BARE is 15 or 16 digits with no punctuation at all, which is how a card arrives
# out of a paste or a form field, and which has no shape whatsoever. There are
# 5,271 undecorated 13-to-19 digit runs across those records. Requiring an issuer
# prefix AT THE LENGTH THAT ISSUER USES cuts that to 10; requiring the Luhn check
# digit as well cuts it to 0. That is the whole reason a checksum appears in this
# file: it is the last 10-to-0 step on the one branch that has nothing else to go
# on. It is not applied anywhere else because nowhere else does it earn anything
# -- Luhn on its own is not a detector, it only rejects nine runs in ten, and on
# these 5,271 it would still leave 479 of them masked and every one a false one.
#
# Two things the bare branch deliberately does NOT do, both measured:
#
# - It is not label-gated, unlike `passport`, `routing` and `ip`. A label earns
#   nothing here: across all 451,435 records NO bare digit run has `card`,
#   `credit`, `debit`, `visa`, `amex` or `cvv` near it, and the only thing
#   admitting the short spelling `cc` would add is the `cc=` of one affiliate
#   URL parameter -- so a label branch would be all cost and no catch.
#   Worse, a label could not reach the value even where one exists -- `takeout.
#   _gate` masks title, detail and url as three separate strings, and Chrome
#   autofill stores `cardNumber` as the record's title with its value in the
#   detail. (In this export that value is `**** 1234`; Chrome exports the masked
#   display form, not the number. But the field separation is the same one the
#   `phone` comment above records, and no pattern written here can cross it.)
#
# - It accepts 15 and 16 digits only, because those are the two lengths that are
#   RARE here: 132 and 149 of the 5,271 runs. Adding 13-digit legacy Visa costs 2
#   false positives (two Aldi product SKUs) and adding 19-digit co-branded Visa
#   and Discover costs 15 -- Google Play track ids, Google place ids, a Steam
#   discussion id, an Android ID -- because 19 is the commonest opaque-id length
#   in this corpus at 1,919 runs, and one in ten of anything passes Luhn by
#   chance. Restoring the full issuer table across 13 to 19 costs 24. So a 13- or
#   19-digit card written BARE is missed on purpose; written in groups it is
#   still caught, because GROUPED allows the trailing group of three.
_CARD = (
    r"(?<![\w.-])(?:"
    r"\d{4}[ -]\d{4}[ -]\d{4}[ -]\d{4}(?:[ -]\d{3})?"  # Visa/Mastercard/Discover
    r"|\d{4}[ -]\d{6}[ -]\d{5}"  # American Express, written 4-6-5
    r"|\d{15,16}"  # bare, and only if `_card_is_plausible` agrees
    r")(?![\w-])"
)

#: Issuer prefix paired with the length that issuer actually issues at. Both
#: halves matter: `4` alone is one bare digit run in ten, and it is the pairing
#: with a length that takes 5,271 runs down to 10.
_ISSUERS: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"4"), 16),  # Visa
    (re.compile(r"5[1-5]"), 16),  # Mastercard
    (re.compile(r"2(?:2[2-9]|[3-6]\d|7[01]|720)"), 16),  # Mastercard, 2-series
    (re.compile(r"3[47]"), 15),  # American Express
    (re.compile(r"6011|65"), 16),  # Discover
)


def _luhn(digits: str) -> bool:
    """The check digit every issued card carries, and no timestamp carries.

    Double every second digit from the right, casting out nines, and the total
    must be a multiple of ten. It rejects nine in ten arbitrary digit runs, which
    is exactly as much as it claims to do -- see `_card_is_plausible` for why it
    is only ever asked in combination with a length and an issuer prefix.
    """
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _card_is_plausible(match: re.Match[str]) -> bool:
    """Whether a `_CARD` match is worth masking. See the comment above `_CARD`.

    Grouping is accepted as its own evidence and checked no further. Bare digits
    have no evidence at all, so they must carry both an issuer's prefix at that
    issuer's length and a valid check digit.
    """
    raw = match.group(0)
    digits = raw.replace(" ", "").replace("-", "")
    if digits != raw:
        return True
    return any(
        len(digits) == length and prefix.match(digits) for prefix, length in _ISSUERS
    ) and _luhn(digits)


#: Identifiers worth masking but not worth discarding a record over.
#:
#: Each entry is a kind, a pattern, and an optional second opinion: a predicate
#: that sees the match and can decline it. Only `card` has one, because only
#: `card` has a branch whose text carries its own proof -- see `_CARD`.
#:
#: Order matters, and only in one direction: a pattern that can swallow another
#: one's text must come first, so the placeholder names the identifier a reader
#: would have to act on. `card` before `phone` is the live case -- sixteen digits
#: in groups of four contains several phone-shaped runs, and `[REDACTED:phone]`
#: on a credit card number would tell the reader to cancel the wrong thing.
_SOFT_PATTERNS: tuple[
    tuple[str, re.Pattern[str], Callable[[re.Match[str]], bool] | None], ...
] = (
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), None),
    ("card", re.compile(_CARD), _card_is_plausible),
    (
        "passport",
        re.compile(r"(?i)\bpassport\s*(?:no\.?|number|#)?\s*[:=]?\s*([A-Z0-9]{7,9})\b"),
        None,
    ),
    ("iban", re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"), None),
    (
        "routing",
        re.compile(r"(?i)\b(?:routing|account)\s*(?:no\.?|number|#)\s*[:=]?\s*(\d{8,17})\b"),
        None,
    ),
    ("phone", re.compile(_PHONE), None),
    ("ip", re.compile(_IP), None),
)

# NOT here, and not in SENSITIVE_CUES either: a DATE OF BIRTH.
#
# It has no shape of its own. Measured over the real export, 610 of 274,414
# records carry a date in one of the three common spellings -- 171 ISO, 373
# slashed, 68 long-form -- and exactly TWO of them are the account owner's
# birthday. A pattern over bare dates would mask 305 ordinary dates for every
# real one, in a vault whose records are mostly dated.
#
# Labelling it does not rescue the idea, it inverts it. `mom's birthday is
# October 25` is the fact this vault exists to keep, not the leak, and no
# pattern can tell the owner's birthday from a friend's. SENSITIVE_CUES is worse
# again: for the history class in `takeout._gate` and `activity.parse` a cue
# match DROPS the record rather than flagging it, so `birthday` in the cue list
# would silently delete browsing history.
#
# The owner's own date of birth is known by FIELD NAME where it is read --
# `Profile.json`'s `birthday`, `SubscriberInfo.html`'s `Birthday` -- and a field
# a reader can name is a decision for that reader, not for a regex here.

#: Subject matter that makes a record sensitive under spec §10 even when it
#: contains no identifier at all. Deliberately broad: over-flagging costs a
#: confirmation prompt, under-flagging leaks something the user trusted us with.
SENSITIVE_CUES = re.compile(
    r"(?i)\b(?:"
    r"diagnos\w*|symptom\w*|prescri\w*|medication|dosage|therapy|therapist|"
    r"psychiatr\w*|counsel(?:l)?or|depress\w*|anxiety|panic attack|adhd|"
    r"bipolar|eating disorder|self.harm|suicid\w*|overdose|rehab|sober|"
    r"salary|wage|paycheck|bonus|debt|loan|mortgage|bankrupt\w*|foreclos\w*|"
    r"credit score|overdraft|financial aid|tuition bill|"
    r"lawyer|attorney|lawsuit|sued|subpoena|arrest\w*|charged with|probation|"
    r"deport\w*|visa status|immigration|"
    r"divorce|custody|restraining order|abus\w*|affair|miscarriage|pregnan\w*"
    r")\b"
)


def scan_hard(text: str) -> list[Secret]:
    """Credentials. A record carrying one of these is dropped whole, not masked.

    Named for the contract the harvester expects; `find_secrets` is the same
    function under its original name.
    """
    return find_secrets(text)


def _mask_one(
    kind: str,
    pattern: re.Pattern[str],
    accept: Callable[[re.Match[str]], bool] | None,
    text: str,
) -> tuple[str, int]:
    """Apply one soft pattern, honouring its second opinion if it has one.

    A declined match is put back verbatim and is NOT counted, so a record whose
    only candidate was declined comes back with `hit` false -- which is the whole
    point: an unflagged record is the honest answer when nothing was found.
    """
    placeholder = f"[REDACTED:{kind}]"
    if accept is None:
        return pattern.subn(placeholder, text)

    masked = 0

    def substitute(match: re.Match[str]) -> str:
        nonlocal masked
        if not accept(match):
            return match.group(0)
        masked += 1
        return placeholder

    return pattern.sub(substitute, text), masked


def mask_soft(text: str) -> tuple[str, bool]:
    """Mask private identifiers in place. Returns the text and whether anything hit.

    Unlike `redact`, this deliberately KEEPS the record: the surrounding sentence
    is usually the fact worth remembering, and only the identifier is dangerous.
    """
    out = text
    hit = False
    for kind, pattern, accept in _SOFT_PATTERNS:
        replaced, n = _mask_one(kind, pattern, accept, out)
        if n:
            hit = True
            out = replaced
    return out, hit


def is_sensitive(text: str) -> bool:
    """Whether spec §10 applies, judged mechanically rather than by a model."""
    return bool(SENSITIVE_CUES.search(text))
