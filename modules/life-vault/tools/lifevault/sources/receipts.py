"""Coverage accounting, per spec §12.2.

Every item in every source gets exactly one row, including the ones that produced
nothing. That is what makes "did you process X?" a question with an answer, and
what makes a silently skipped source impossible rather than merely unlikely.

The reconcile step is the point of the whole module: at the end of a batch the row
count must equal the number of items the source actually held. A mismatch fails
loudly instead of producing a vault that looks complete.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from ..atomic import write_atomic

#: Every terminal state an item can reach. `no-signal` is a success: most bursts
#: and most emails contain nothing worth keeping forever, and recording that is
#: how we tell "read it, found nothing" apart from "never looked".
STATUSES: tuple[str, ...] = (
    "extracted",
    "no-signal",
    "failed",
    "skipped",
    "rejected-secret",
)

_FORBIDDEN = ("\t", "\n", "\r", "|")
_HEADER = "# item_id\tsha256\tstatus\tn_entries\tentry_paths"


class ReceiptError(ValueError):
    """A receipt could not be recorded or read back. Never swallowed."""


@dataclass(frozen=True)
class Receipt:
    item_id: str
    sha256: str
    status: str
    n_entries: int = 0
    entry_paths: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ReceiptError(
                f"unknown status {self.status!r}; expected one of {', '.join(STATUSES)}"
            )
        if not self.item_id:
            raise ReceiptError("a receipt must identify its item")
        for name, value in (
            ("item_id", self.item_id),
            ("sha256", self.sha256),
            ("status", self.status),
        ):
            _reject_delimiters(name, value)
        for path in self.entry_paths:
            _reject_delimiters("entry_path", path)


def _reject_delimiters(name: str, value: str) -> None:
    for char in _FORBIDDEN:
        if char in value:
            raise ReceiptError(f"{name} may not contain {char!r}: {value!r}")


@dataclass
class ReconcileResult:
    expected: int
    recorded: int

    @property
    def ok(self) -> bool:
        return self.expected == self.recorded


def content_hash(text: str) -> str:
    """Stable hash of an item's content, for the idempotent re-run rule (§12.4)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ReceiptLog:
    """An append-only record of what was looked at and what came of it."""

    def __init__(self, path: str | Path, receipts: Iterable[Receipt] = ()):
        self.path = Path(path)
        self._receipts: list[Receipt] = list(receipts)
        # A set, not a scan: the personal mailbox alone holds 44,574 items and
        # this is consulted once per item.
        self._seen: set[str] = {r.item_id for r in self._receipts}

    def __len__(self) -> int:
        return len(self._receipts)

    @property
    def receipts(self) -> tuple[Receipt, ...]:
        return tuple(self._receipts)

    def seen(self, item_id: str) -> bool:
        return item_id in self._seen

    def append(self, receipt: Receipt) -> None:
        if receipt.item_id in self._seen:
            raise ReceiptError(
                f"item {receipt.item_id!r} already has a receipt; "
                "an item is looked at once, and re-runs skip what is already recorded"
            )
        self._receipts.append(receipt)
        self._seen.add(receipt.item_id)

    def reconcile(self, expected_count: int) -> ReconcileResult:
        result = ReconcileResult(expected=expected_count, recorded=len(self._receipts))
        if not result.ok:
            raise ReceiptError(
                f"coverage mismatch for {self.path}: the source held "
                f"{expected_count} items but {len(self._receipts)} receipts were "
                "written. Some items were never accounted for."
            )
        return result

    def save(self) -> None:
        def write(handle) -> None:
            handle.write(_HEADER + "\n")
            for r in self._receipts:
                handle.write(
                    f"{r.item_id}\t{r.sha256}\t{r.status}\t{r.n_entries}\t"
                    f"{'|'.join(r.entry_paths)}\n"
                )

        write_atomic(self.path, write)

    @classmethod
    def load(cls, path: str | Path) -> "ReceiptLog":
        target = Path(path)
        if not target.exists():
            return cls(target)
        receipts: list[Receipt] = []
        for lineno, line in enumerate(
            target.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip() or line.startswith("#"):
                continue
            fields = line.split("\t")
            if len(fields) != 5:
                raise ReceiptError(
                    f"{target}:{lineno}: expected 5 tab-separated fields, got "
                    f"{len(fields)}. A corrupt row is not skipped, because skipping "
                    "it would silently understate coverage."
                )
            item_id, sha, status, raw_count, raw_paths = fields
            try:
                count = int(raw_count)
            except ValueError:
                raise ReceiptError(
                    f"{target}:{lineno}: n_entries is not a number: {raw_count!r}"
                ) from None
            receipts.append(
                Receipt(
                    item_id=item_id,
                    sha256=sha,
                    status=status,
                    n_entries=count,
                    entry_paths=tuple(p for p in raw_paths.split("|") if p),
                )
            )
        return cls(target, receipts)
