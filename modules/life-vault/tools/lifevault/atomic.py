"""Replace a file's whole contents, or leave it exactly as it was.

Every file this vault rewrites is rewritten in full: a log holds every fact ever
recorded about one entity, and `_index/entities.tsv` holds the alias table that
resolves all of them. Open-and-truncate leaves those files empty for the whole
duration of the write, so anything that interrupts it -- ENOSPC on a chronically
full disk, a killed 3am job, a power cut -- destroys data that no re-run can
reconstruct.

The recipe is the standard one: write a complete copy beside the target, push it
towards the disk, then rename it over the target. `os.replace` is atomic within
a single filesystem, so a reader sees either the entire old file or the entire
new one and never anything in between.

What is guaranteed, and what is not
-----------------------------------
Guaranteed unconditionally: a reader of the target sees either the whole old
file or the whole new one, and a write that fails for any reason leaves the
target byte-for-byte as it was, with no staging file beside it. That holds
because it rests on `os.replace` alone, not on any promise about hardware.

NOT guaranteed: that a write which has returned will still be there after the
power is cut. This module asks for the strongest barrier the platform offers --
on Darwin `F_FULLFSYNC`, elsewhere `fsync` -- but on Darwin `fsync(2)` only
hands the bytes to the drive and returns; the drive may hold them in its own
volatile cache, and Apple documents `F_FULLFSYNC` as the way to force that cache
out. A filesystem is free to refuse `F_FULLFSYNC`, and some do; the refusal is
swallowed and plain `fsync` is used, so on such a mount durability is exactly as
weak as `fsync` is. The directory flush that makes the rename itself durable is
best-effort in the same way. Nothing here is a substitute for a backup.
"""

from __future__ import annotations

import os
import re
import stat
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import IO

try:
    import fcntl
except ImportError:  # pragma: no cover - no fcntl at all means no F_FULLFSYNC
    fcntl = None  # type: ignore[assignment]

__all__ = ["reap_orphans", "sweep_orphan_temps", "write_atomic"]

DEFAULT_REAP_AGE = 3600.0

# The exact shape write_atomic gives a staging file: a leading dot, the target's
# own name, the writer's pid, a random half, and `.tmp`. Matching the whole
# shape rather than just `.tmp` is the difference between reaping our own debris
# and deleting somebody's editor scratch file or `.DS_Store`. The pid half must
# be digits and the random half hex, so a hand-named `.log.md.backup.tmp`
# survives.
_STAGING_RE = re.compile(r"\..+\.\d+\.[0-9a-f]+\.tmp\Z")


def _staging_pattern(target: Path) -> str:
    return f".{target.name}.*.tmp"


def _resolved(path: str | Path) -> Path:
    """The real file, with any symlink in the path followed.

    A rename replaces whatever sits at the name it is given, so renaming onto a
    symlink would replace the LINK with a regular file and freeze the file it
    pointed at -- while `Path.write_text`, which this recipe replaced, wrote
    through it. Someone who symlinks an entity log into iCloud, an external
    archive or another vault would lose the link on the first rewrite and send
    every later write somewhere else, silently.

    Resolving also puts the staging file in the real file's own directory,
    which is what keeps the rename inside one filesystem.
    """
    return Path(os.path.realpath(path))


def sweep_orphan_temps(path: str | Path) -> list[Path]:
    """Remove staging files left beside `path` by a writer that was killed.

    SIGKILL or a power cut between opening the staging file and renaming it is
    the one window no `except` clause can cover, and what it leaves behind is a
    COMPLETE copy of the file -- for a log, every fact ever recorded about that
    entity, including anything marked sens/private. The leading dot hides it
    from `ls` and from Obsidian, so nothing surfaces it and nothing removes it.

    The caller must hold whatever lock serializes writes to `path`, or this can
    delete the staging file of a writer that is still running. Failures are
    ignored: sweeping is opportunistic housekeeping and must never be the
    reason a write fails.
    """
    target = _resolved(path)
    swept: list[Path] = []
    try:
        candidates = list(target.parent.glob(_staging_pattern(target)))
    except OSError:
        return swept
    for tmp in candidates:
        try:
            tmp.unlink()
        except OSError:
            continue
        swept.append(tmp)
    return swept


def reap_orphans(
    vault: str | Path,
    older_than_seconds: float = DEFAULT_REAP_AGE,
) -> list[Path]:
    """Delete staging files abandoned across the whole vault. Returns what went.

    `sweep_orphan_temps` only ever runs beside a log that is about to be
    rewritten, so it reaps an orphan only if that same entity is written again.
    An entity nobody touches a second time keeps its orphan forever -- and that
    orphan is a COMPLETE copy of the log, every fact ever recorded about that
    person, at the log's own permissions, including everything tagged
    sens/private. The leading dot hides it from Obsidian and from a casual `ls`,
    so nothing surfaces it, and on a disk documented as chronically full it is
    also dead weight that grows with every killed job. This is the pass that
    finds them without needing a write to ride along.

    The list of directories to walk comes from `index.SEARCH_DIRS` rather than a
    copy kept here: that tuple already defines what an entity directory IS (it
    is what `reindex` scans and what `lv add --kind` accepts), and a second copy
    would drift until a whole kind of entity was quietly never reaped. The walk
    is recursive because entity logs are not all one level down.

    Age is the file's own mtime, and only files at least `older_than_seconds`
    old are touched, because a fresh staging file probably belongs to a writer
    that is still running. Deleting one of those does not corrupt anything --
    the writer holds its descriptor, and its `os.replace` fails loudly, leaving
    the target as it was -- but it turns a healthy write into a failed one for
    no reason. An hour is far longer than any write here takes and far shorter
    than the debris sits around.

    That threshold is a heuristic, not a lock. A caller that wants the guarantee
    should hold the vault lock (`lifevault.vaultlock`), which is what makes
    "no other writer is mid-write" true rather than likely.

    Nothing else is removed: not directories, not symlinks, not hidden files
    whose names merely end in `.tmp`. Per-file failures are skipped rather than
    raised -- housekeeping must not become the reason a job dies -- and a file
    that could not be deleted is simply absent from the returned list, which
    reports what actually went, not what was intended.
    """
    # Imported here, not at module scope. This module is the bottom of the
    # stack: `entities`, `subjects`, `store` and `index` all import it, `index`
    # at module scope for its tag files. A module-level import back the other
    # way would therefore be a circular import. A local import costs one dict
    # lookup per reap and cannot become that bug.
    from .index import SEARCH_DIRS

    root = Path(vault)
    cutoff = time.time() - older_than_seconds
    reaped: list[Path] = []

    for name in SEARCH_DIRS:
        try:
            # Sorted and materialised inside the try: rglob is lazy, so a
            # missing or unreadable directory -- a vault root that is a typo,
            # a kind that has never been used -- surfaces here rather than
            # part way through the loop below.
            candidates = sorted((root / name).rglob(".*.tmp"))
        except OSError:
            continue

        for tmp in candidates:
            if not _STAGING_RE.fullmatch(tmp.name):
                continue
            try:
                st = os.lstat(tmp)
            except OSError:
                continue
            # lstat, and regular files only. A symlink is not a leaked copy of
            # a log, and following one would let a link planted in an entity
            # directory point the age check -- and then the unlink -- at a file
            # this was never meant to touch.
            if not stat.S_ISREG(st.st_mode):
                continue
            if st.st_mtime > cutoff:
                continue
            try:
                tmp.unlink()
            except OSError:
                continue
            reaped.append(tmp)

    return reaped


def _flush(fd: int) -> None:
    """Push everything written to `fd` as far towards the disk as this OS allows.

    On Darwin `os.fsync` is NOT a durability barrier. `fsync(2)` hands the data
    to the drive and returns; the drive may keep it in its own volatile write
    cache, so a power cut can lose a write that fsync already reported as
    complete. Apple's documented way to force that cache out is the
    `F_FULLFSYNC` fcntl, so that is what gets tried first here.

    A filesystem may refuse `F_FULLFSYNC` -- network and virtualised mounts do --
    and a refusal must not fail the write: a write that is less durable than we
    would like still beats a write that did not happen. So an `OSError` falls
    back to `os.fsync`, which is also all there is to ask for off Darwin. On such
    a mount the durability is exactly `fsync`'s, no better, and this module
    cannot tell the caller which of the two it got.

    Errors from the fallback are NOT swallowed. Failing to flush a file whose
    bytes are about to be renamed over the only copy of a log is a real failure
    and the caller must hear about it.
    """
    fullfsync = getattr(fcntl, "F_FULLFSYNC", None) if sys.platform == "darwin" else None
    if fullfsync is not None:
        try:
            fcntl.fcntl(fd, fullfsync)
        except OSError:
            pass
        else:
            return
    os.fsync(fd)


def _flush_dir(directory: Path) -> None:
    """Flush the directory entry, so the rename itself has a chance of surviving.

    Flushing a file only pushes its *contents* towards the disk. The link that
    makes those contents reachable under the target's name lives in the parent
    directory and needs its own flush, or a crash can leave the old name
    pointing at nothing.

    Not every platform lets you open a directory for this. Where it fails the
    rename is still atomic -- only its durability is weaker -- so a failure here
    must not turn a write that already succeeded into an exception. That is why
    this one swallows errors and `_flush` does not.
    """
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        _flush(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_atomic(
    path: str | Path,
    write: Callable[[IO[str]], None],
    *,
    encoding: str = "utf-8",
) -> None:
    """Rewrite `path` with whatever `write` streams into the handle it is given.

    Either the target ends up holding every byte `write` produced, or it is left
    byte-for-byte as it was. There is no third outcome, and no debris either way.

    `write` is handed an open text file and may raise: validating a record as it
    is written is the natural way to use this, and a rejection part way through
    must cost the caller an exception rather than the file.

    The parent directory is NOT created. A missing one means the caller was
    handed a path it did not expect, and quietly building it would turn a typo'd
    vault root into a second, empty vault. Callers that own their directory
    should mkdir it themselves before calling.

    A symlink at `path` is followed, so the file it points at is what gets
    rewritten and the link survives. Hard links do NOT survive: the rename
    installs a new inode, so another name for the old one keeps the old
    contents. That is the right outcome for the tooling that makes hard links
    (`rsync --link-dest` and borg-style backups link a snapshot to the live
    file so the snapshot freezes when the file changes), and preserving them
    would mean writing in place, which is the torn-file risk this exists to
    remove. macOS extended attributes -- Finder tags and comments -- are lost
    for the same reason, and stdlib on macOS has no way to copy them
    (`os.listxattr` is Linux-only). The mode, including setuid/setgid/sticky,
    is carried across.
    """
    target = _resolved(path)

    # Beside the target, so the rename stays inside one filesystem -- across a
    # mount point os.replace is not atomic and may not work at all. The leading
    # dot keeps a crashed run from leaving anything visible in Obsidian, and pid
    # plus random suffix keep two concurrent writers off each other's file.
    tmp = target.parent / f".{target.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp"

    try:
        # O_EXCL refuses to reuse an existing file or to follow a symlink planted
        # at this name. 0o666 is masked by the umask exactly as a plain
        # Path.write_text would be, so a brand-new file keeps the permissions it
        # would otherwise have got -- unlike mkstemp, which would hand every file
        # in the vault a 0600 of its own choosing.
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        except PermissionError as exc:
            # Staging beside the target means a rewrite needs write permission
            # on the DIRECTORY, where writing in place needed only the file's.
            # The bare error names a temp file the caller never heard of, so
            # say what is actually being asked for.
            raise PermissionError(
                exc.errno,
                f"cannot stage a rewrite of {target.name}: an atomic write needs "
                f"write permission on the directory, not only on the file",
                str(target.parent),
            ) from exc

        try:
            fh = os.fdopen(fd, "w", encoding=encoding, newline="\n")
        except BaseException:
            os.close(fd)
            raise

        with fh:
            write(fh)
            fh.flush()
            _flush(fh.fileno())
            # AFTER the bytes, not before: writing to a file clears setuid and
            # setgid on macOS, so a mode copied first came out silently
            # stripped of exactly the bits that are hardest to notice missing.
            # An existing target keeps its own mode, which the rename would
            # otherwise drop entirely.
            try:
                os.chmod(tmp, stat.S_IMODE(os.stat(target).st_mode))
            except FileNotFoundError:
                pass

        os.replace(tmp, target)
    except BaseException:
        # Anything at all -- a rejected record, a full disk, Ctrl-C -- leaves the
        # target exactly as it was, with nothing beside it. The cleanup itself
        # must never raise, or it would replace the real error with its own.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    _flush_dir(target.parent)
