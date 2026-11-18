from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import IO

from .atomic import write_atomic

FORBIDDEN_IN_TSV = ("\t", "\n", "\r", "|")


def normalize(name: str) -> str:
    """Fold a name to a lookup key: no case, no diacritics, no punctuation.

    Deliberately forgiving, so "José", "jose" and "Jose!" all resolve to one
    entity. Non-Latin scripts are PRESERVED rather than stripped — an Arabic or
    CJK name must not fold to the empty string, or every such name would collide
    with every other one.
    """
    text = unicodedata.normalize("NFKD", name)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.casefold()
    text = "".join(c if c.isalnum() or c.isspace() else " " for c in text)
    return re.sub(r"\s+", " ", text).strip()


def slugify(name: str) -> str:
    slug = normalize(name).replace(" ", "-")
    if not slug:
        raise ValueError(f"name has no alphanumeric content: {name!r}")
    return slug


class Entities:
    """Maps every alias of a real-world thing to exactly one slug and path."""

    def __init__(self, rows: dict[str, tuple[str, list[str]]] | None = None):
        self.rows = rows or {}
        self._alias: dict[str, str] = {}
        for slug, (_path, aliases) in self.rows.items():
            for alias in aliases:
                key = normalize(alias)
                owner = self._alias.get(key)
                if owner and owner != slug:
                    raise ValueError(
                        f"alias {alias!r} maps to both {owner!r} and {slug!r}"
                    )
                self._alias[key] = slug

        seen: dict[str, str] = {}
        for slug, (entity_path, _aliases) in self.rows.items():
            if entity_path in seen:
                raise ValueError(
                    f"{slug!r} and {seen[entity_path]!r} share path {entity_path!r}"
                )
            seen[entity_path] = slug

    @classmethod
    def load(cls, path: str | Path) -> "Entities":
        """Read the table, refusing to silently discard any row.

        A bare `rows[slug] = ...` would let a hand-edited file with a repeated
        or empty slug overwrite an earlier row before __init__ ever sees it --
        the same silent-overwrite class as the mint bug, reachable through the
        file instead of the API.
        """
        rows: dict[str, tuple[str, list[str]]] = {}
        p = Path(path)
        if p.exists():
            for lineno, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if not line.strip() or line.startswith("#"):
                    continue
                slug, entity_path, aliases = (line.split("\t") + ["", ""])[:3]
                if not slug.strip():
                    raise ValueError(f"{p}:{lineno}: row has no slug: {line!r}")
                if slug in rows:
                    raise ValueError(f"{p}:{lineno}: duplicate slug {slug!r}")
                rows[slug] = (entity_path, [a for a in aliases.split("|") if a])
        return cls(rows)

    def save(self, path: str | Path) -> None:
        """Rewrite the table atomically. A failed save costs nothing but time.

        This runs on every `lv add`, and it rewrites the whole file, so a torn
        write does not damage one entity -- it damages the file that resolves
        every entity in the vault. load() raises on a duplicate or empty slug,
        so a half-written row would stop every future write until a human
        repaired the file by hand; and a table truncated to nothing is worse
        still, because it parses cleanly as zero rows. Every alias would
        silently stop resolving and the next ingest would mint a second
        directory for every person already recorded -- the "one person becomes
        four files" failure this table exists to prevent, with nothing anywhere
        that looks like an error.

        Per-FIELD validation happens as the fields are written rather than up
        front: with the write staged in a temp file, a rejection part way
        through costs the caller an exception and nothing else, so there is no
        reason to walk every alias twice.

        The invariants below are checked first because they are properties of
        the table as a whole rather than of any one field, and because a table
        that fails them is one this method must never write at all.
        """
        self._check_loadable()
        write_atomic(path, self._write_table)

    def _check_loadable(self) -> None:
        """Refuse to write a table `load` would refuse to read.

        `save` used to validate only FORBIDDEN_IN_TSV while `load` also rejects
        an empty slug and a repeated one, so a table could be written cleanly
        and then be unreadable. That gap is not a self-inflicted wound on one
        caller: `load` raises rather than skipping the bad row, and every
        `lv add` loads this table first, so one such row stops writes for EVERY
        entity in the vault until a human repairs the file by hand. Raising
        here fails the process that actually built the bad table, while it is
        still in front of the person who can fix it, and leaves the file that
        every other writer depends on exactly as it was.

        The two checks mirror `load`'s exactly, `.strip()` included: a slug of
        spaces reads back as a row with no slug, so it has to be rejected as
        one here. Keeping the wording identical means the same error names the
        same fault whichever end of the file it is caught at.
        """
        seen: set[str] = set()
        for slug in sorted(self.rows):
            if not slug.strip():
                raise ValueError(f"row has no slug: {slug!r}")
            if slug in seen:
                raise ValueError(f"duplicate slug {slug!r}")
            seen.add(slug)

    def _write_table(self, fh: IO[str]) -> None:
        fh.write("# slug\tpath\taliases (pipe-separated)\n")
        for slug in sorted(self.rows):
            entity_path, aliases = self.rows[slug]
            for value in (slug, entity_path, *aliases):
                for ch in FORBIDDEN_IN_TSV:
                    if ch in value:
                        raise ValueError(f"{value!r} contains {ch!r}, cannot save as TSV")
            fh.write(f"{slug}\t{entity_path}\t{'|'.join(aliases)}\n")

    def resolve(self, name: str) -> str | None:
        return self._alias.get(normalize(name))

    def add_alias(self, slug: str, alias: str) -> None:
        for ch in FORBIDDEN_IN_TSV:
            if ch in alias:
                raise ValueError(f"alias may not contain {ch!r}: {alias!r}")
        key = normalize(alias)
        if not key:
            raise ValueError(f"alias has no alphanumeric content: {alias!r}")
        owner = self._alias.get(key)
        if owner and owner != slug:
            raise ValueError(f"alias {alias!r} already resolves to {owner!r}")
        if owner == slug:
            return
        self._alias[key] = slug
        self.rows[slug][1].append(alias)

    def _unique_slug(self, name: str, kind: str) -> str:
        """Never return a slug that already belongs to something else."""
        base = slugify(name)
        if base not in self.rows:
            return base
        singular = kind[:-1] if kind.endswith("s") else kind
        candidate = f"{base}-{singular}"
        if candidate not in self.rows:
            return candidate
        n = 2
        while f"{candidate}-{n}" in self.rows:
            n += 1
        return f"{candidate}-{n}"

    def mint(self, name: str, kind: str) -> str:
        existing = self.resolve(name)
        if existing:
            return existing
        slug = self._unique_slug(name, kind)
        self.rows[slug] = (f"{kind}/{slug}", [])
        self.add_alias(slug, name)
        return slug
