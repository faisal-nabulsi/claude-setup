from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from .atomic import write_atomic
from .entry import parse_entries

SEARCH_DIRS = ("people", "orgs", "projects", "topics", "threads", "places", "timeline")


def tag_filename(tag: str) -> str:
    return tag.replace("/", "-") + ".txt"


def _status_tag(status: str) -> str:
    if status.startswith("superseded-by"):
        return "status/superseded"
    if status == "conflicted":
        return "status/conflicted"
    if status.startswith("blocked-by"):
        return "status/blocked"
    return "status/current"


def rebuild(vault: str | Path) -> dict[str, int]:
    """Rebuild _index/tags/ from the markdown files. Returns tag -> count."""
    vault = Path(vault)
    postings: dict[str, list[str]] = defaultdict(list)

    for directory in SEARCH_DIRS:
        for log in sorted((vault / directory).rglob("*.md")):
            if log.name == "index.md":
                continue
            rel = log.relative_to(vault).as_posix()
            for entry in parse_entries(log.read_text(encoding="utf-8")):
                posting = f"{rel}#{entry.date}"
                # Literal status/ tags are dropped: the field is the only truth.
                tags = {t for t in entry.tags if not t.startswith("status/")}
                for tag in tags | {_status_tag(entry.status)}:
                    postings[tag].append(posting)

    # Build and validate the whole payload BEFORE touching disk. Deleting first
    # and discovering a bad filename mid-write leaves legitimate tag files gone
    # and never rewritten — observed as OSError "File name too long".
    payload: dict[str, str] = {}
    counts: dict[str, int] = {}
    for tag, paths in postings.items():
        name = tag_filename(tag)
        if len(name.encode("utf-8")) > 255 or "\x00" in name or "/" in name:
            raise ValueError(f"tag {tag!r} does not make a usable filename: {name!r}")
        if name in payload:
            raise ValueError(f"tag {tag!r} collides with another tag on {name!r}")
        unique = sorted(set(paths))
        payload[name] = "\n".join(unique) + "\n"
        counts[tag] = len(unique)

    out = vault / "_index" / "tags"
    out.mkdir(parents=True, exist_ok=True)
    for stale in out.glob("*.txt"):
        stale.unlink()
    for name, content in payload.items():
        # Atomic like every other file this vault writes, though these are the
        # one kind that is genuinely disposable: spec §8 makes the markdown the
        # only source of truth and `lv reindex` regenerates all of _index/tags/.
        # It is still worth the rename, because `lv find` reads these files
        # without the vault lock and a truncated posting list reads exactly like
        # a complete one -- the query answers "no such fact" and nothing looks
        # wrong. The default argument binds `content` per iteration; a bare
        # closure over the loop variable would write the last tag's postings
        # into every file.
        write_atomic(out / name, lambda fh, text=content: fh.write(text))
    return counts
