"""Stream an mbox and separate biography from marketing.

Per spec §12.5 this classifies PER MESSAGE, not per sender. The same address
sends a "50% off" blast and an order confirmation; the first is noise and the
second is a record of what was bought and when. An Uber receipt is a dated record
of where someone physically was. A payment-app memo names another person and says why
money moved between them -- relationship data wearing a receipt's clothes.

Dropping a sender wholesale because most of its mail is promotional throws all of
that away. Measured on the real mailbox: 44,574 messages, roughly a third of them
clothing-retailer marketing, sitting alongside the receipts from the same senders.

The 3.3GB file is never loaded into memory. `stream` yields one record at a time.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from pathlib import Path

CLASSIFICATIONS: tuple[str, ...] = (
    "transactional",
    "personal",
    "marketing",
    "notification",
)

#: An mbox message begins at a line matching the postmark, not at any "From ".
#: Splitting on the latter cuts messages in half whenever a body quotes someone.
_POSTMARK = re.compile(rb"^From \S+.*\d{4}\s*$")

_TAG = re.compile(r"<[^>]+>")
_SCRIPT_OR_STYLE = re.compile(r"(?is)<(script|style)\b.*?</\1>")

#: Words that mark a completed event: an amount, an order, a journey, a booking.
_TRANSACTIONAL = re.compile(
    r"(?i)\b(?:receipt|invoice|order\s*(?:#|no\.?|confirmation|placed|shipped)|"
    r"has shipped|your trip|trip with|ride with|delivered|payment|you paid|"
    r"you received|charged|refund|booking|reservation|itinerary|boarding pass|"
    r"ticket confirmation|tuition|statement|transaction)\b"
)

#: Words that mark an inducement to buy rather than a record of having bought.
_MARKETING = re.compile(
    r"(?i)(?:\b\d{1,3}%\s*off\b|\bsale\b|\bdeals?\b|\bshop now\b|\blimited time\b|"
    r"\bends (?:tonight|today|soon)\b|\bnew arrivals\b|\bdon'?t miss\b|\bexclusive\b|"
    r"\blast chance\b|\bfree shipping\b|\bunsubscribe\b)"
)

_AMOUNT = re.compile(r"[$£€]\s?\d[\d,]*(?:\.\d{2})?")

_NOREPLY = re.compile(r"(?i)\b(?:no-?reply|do-?not-?reply|notifications?|alerts?|mailer)\b")


@dataclass(frozen=True)
class MailRecord:
    message_id: str
    date: datetime | None
    sender: str
    sender_name: str
    recipients: tuple[str, ...] = field(default_factory=tuple)
    subject: str = ""
    body: str = ""
    classification: str = "notification"
    bulk: bool = False


def _html_to_text(html: str) -> str:
    text = _SCRIPT_OR_STYLE.sub(" ", html)
    text = _TAG.sub(" ", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&#39;", "'")
        .replace("&quot;", '"')
    )
    return re.sub(r"[ \t]*\n[ \t]*", "\n", re.sub(r"[ \t]+", " ", text)).strip()


def _decode_part(part) -> str:
    """Decode one part, surviving a charset the sender invented."""
    try:
        payload = part.get_payload(decode=True)
    except Exception:
        return ""
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return payload.decode("utf-8", errors="replace")


def _extract_body(message) -> str:
    """Prefer text/plain; fall back to stripped HTML. Attachments are ignored."""
    plain: list[str] = []
    html: list[str] = []
    if message.is_multipart():
        for part in message.walk():
            if part.is_multipart():
                continue
            disposition = str(part.get("Content-Disposition") or "")
            if "attachment" in disposition.lower():
                continue
            content_type = part.get_content_type()
            if content_type == "text/plain":
                plain.append(_decode_part(part))
            elif content_type == "text/html":
                html.append(_decode_part(part))
    else:
        text = _decode_part(message)
        if message.get_content_type() == "text/html":
            html.append(text)
        else:
            plain.append(text)

    if any(p.strip() for p in plain):
        return "\n".join(plain).strip()
    return _html_to_text("\n".join(html)).strip()


def _unescape_from(raw: bytes) -> bytes:
    """mbox escapes a body line beginning `From ` as `>From `. Undo it."""
    return re.sub(rb"(?m)^>(?=>*From )", b"", raw)


def _parse(raw: bytes) -> MailRecord | None:
    if not raw.strip():
        return None
    message = BytesParser(policy=policy.default).parsebytes(_unescape_from(raw))

    sender_name, sender = "", ""
    from_header = message.get("From")
    if from_header:
        try:
            addresses = getattr(from_header, "addresses", ())
            if addresses:
                sender_name = addresses[0].display_name or ""
                sender = addresses[0].addr_spec or ""
        except Exception:
            sender = str(from_header)
    if not sender:
        sender = str(from_header or "")

    date = None
    if message.get("Date"):
        try:
            date = parsedate_to_datetime(str(message.get("Date")))
        except (TypeError, ValueError):
            date = None

    recipients: list[str] = []
    for header in ("To", "Cc"):
        value = message.get(header)
        if value is None:
            continue
        try:
            recipients.extend(a.addr_spec for a in getattr(value, "addresses", ()))
        except Exception:
            recipients.append(str(value))

    bulk = bool(
        message.get("List-Unsubscribe")
        or str(message.get("Precedence") or "").lower() in {"bulk", "list", "junk"}
    )

    record = MailRecord(
        message_id=str(message.get("Message-ID") or ""),
        date=date,
        sender=sender,
        sender_name=sender_name,
        recipients=tuple(recipients),
        subject=str(message.get("Subject") or ""),
        body=_extract_body(message),
        bulk=bulk,
    )
    return MailRecord(**{**record.__dict__, "classification": classify(record)})


def stream(mbox_path: str | Path) -> Iterator[MailRecord]:
    """Yield one record at a time. Never holds more than a single message."""
    path = Path(mbox_path)
    buffer: list[bytes] = []
    with path.open("rb") as handle:
        for line in handle:
            if _POSTMARK.match(line.rstrip(b"\r\n")) and buffer:
                record = _parse(b"".join(buffer))
                if record is not None:
                    yield record
                buffer = []
            elif _POSTMARK.match(line.rstrip(b"\r\n")):
                continue
            else:
                buffer.append(line)
        if buffer:
            record = _parse(b"".join(buffer))
            if record is not None:
                yield record


def classify(record: MailRecord) -> str:
    """One of transactional, personal, marketing, notification.

    Decided per message. A message is transactional when it records a specific
    COMPLETED event -- an amount, an order, a journey, a booking -- which is why
    a receipt from a bulk sender outranks that sender's marketing reputation.
    """
    haystack = f"{record.subject}\n{record.body[:2000]}"

    transactional = bool(_TRANSACTIONAL.search(haystack))
    if transactional and _AMOUNT.search(haystack):
        return "transactional"
    if transactional and not _MARKETING.search(record.subject):
        return "transactional"

    if record.bulk or _MARKETING.search(haystack):
        return "marketing" if _MARKETING.search(haystack) else "notification"

    if _NOREPLY.search(record.sender):
        return "notification"

    return "personal"


def is_worth_reading(record: MailRecord) -> bool:
    """Transactional and personal mail carries biography. The rest does not."""
    return record.classification in {"transactional", "personal"}
