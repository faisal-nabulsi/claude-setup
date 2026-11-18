"""One writer at a time, across processes.

Every write in this vault is a read-modify-write over a whole file: a log holds
every fact ever recorded about one entity, `_index/entities.tsv` holds the alias
table that resolves all of them, and both are read in full, edited in memory and
written back in full. Two processes that overlap on one of those each read the
same bytes and each write their own version, and the second rename wins. Both
exit 0. The surviving file is perfectly well-formed. Nothing anywhere looks like
an error, and one fact -- or one alias row, which is worse, because it makes one
person resolve as several -- is gone permanently.

**Atomicity is not exclusion.** `lifevault.atomic` guarantees a reader sees the
whole old file or the whole new one; it says nothing about two writers. It also
removed the only symptom this bug used to have: before it, an overlap could
leave a mangled file that `load()` would loudly reject. Now both writers succeed
cleanly, so the fix made the loss quieter rather than rarer. In a vault whose
entire premise is that nothing is ever lost, quiet is the worst outcome.

Concurrent writers are the designed workload, not a hypothetical: spec §9 spawns
background extraction after every response and drains a queue overnight
unattended, and spec §11 plans ~200 batched agents for the Phase 1 backfill.

## Why flock, and not an O_EXCL lockfile

The kernel releases an flock when the holding process's descriptors close --
however it died, including SIGKILL and a power cut. There is nothing left on
disk to go stale, so a 3am job that is killed mid-write cannot block every
future write to the vault until a human notices. An O_EXCL lockfile would need
stale-lock detection, which means guessing whether a pid recorded in a file is
still the same process; that guess is wrong after a pid is reused, and being
wrong means deleting a live writer's lock. One correct mechanism beats two
mechanisms and a heuristic.

The file's *contents* are never read, written or truncated, precisely so that
nothing about locking can fail on a full disk. Only its existence matters, and
its inode: nothing may ever delete or replace `_index/.lock` while the vault is
in use, or two processes will hold exclusive locks on two different inodes and
believe they are alone. `index.rebuild()` only unlinks `_index/tags/*.txt`, so
it is safe today.

## Re-entrancy

flock is a property of an *open file description*, not of a process. A second
`open()` of the lock file in the same process produces a second description,
and locking it blocks against the first -- a process deadlocking against itself.
`lv add` is expected to hold this lock across both an entity log write and an
`entities.tsv` save, so nesting has to be counted rather than re-locked. The
depth counter is keyed by the *resolved* vault path, so the same vault reached
through a symlink, a relative path or a trailing `.` is recognised as one vault
rather than deadlocking against itself.

The per-vault `threading.RLock` alongside it is what makes the counter safe:
without it, a second thread would see depth > 0 and sail straight through
without ever calling flock, which is worse than no lock at all because it looks
locked. It also gives threads the same re-entrant, mutually-exclusive semantics
processes get.

## Why these exceptions are not OSError

`bin/lv` turns every `OSError` into exit 2, and `lifevault.atomic` swallows
`OSError` in its housekeeping paths. "This write was never serialized" must not
be catchable by anything reaching for a file error, so `VaultLockError` derives
from `Exception` directly. The trade-off is that an uncaught `VaultLockTimeout`
exits `lv` with Python's default status of 1, which collides with lv's
documented "exit 1 means your input was rejected, nothing was written". A caller
that needs to tell those apart must catch `VaultLockError` explicitly.

Failing loudly is the point: a backfill agent that hangs forever on a lock is
indistinguishable from one that is working, and 200 of them hanging is a silent
outage. A bounded wait then a named exception is recoverable; an unbounded wait
is not.

POSIX only, deliberately. A fallback that ran unlocked wherever flock is
unavailable would reintroduce exactly the silent loss this module exists to
prevent, so a host that cannot lock fails at import rather than at 3am.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path

__all__ = [
    "DEFAULT_TIMEOUT",
    "VaultLockError",
    "VaultLockTimeout",
    "VaultLockUnavailable",
    "lock_path",
    "vault_lock",
]

# Long enough to sit out a whole-log rewrite by another agent, short enough that
# a genuinely wedged vault is reported the same shift it happened.
DEFAULT_TIMEOUT = 30.0

# flock has no timed wait, so waiting means retrying a non-blocking one. Start
# fine-grained -- most contention is over in milliseconds -- and back off to a
# ceiling that bounds how long the lock sits free while someone wants it.
_POLL_MIN = 0.001
_POLL_MAX = 0.025

# Contention, not failure: flock reports "someone else holds it" as EWOULDBLOCK,
# which is EAGAIN on Linux and macOS. EACCES is listed because a handful of
# systems report a refused lock that way. Anything else is a real fault -- a
# filesystem that cannot lock at all, for instance -- and must not be retried
# silently until the timeout, which would report contention that never existed.
_CONTENDED = frozenset({errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES})


class VaultLockError(Exception):
    """The vault could not be serialized, so nothing was written."""


class VaultLockTimeout(VaultLockError):
    """Someone else held the lock for longer than the caller was willing to wait."""


class VaultLockUnavailable(VaultLockError):
    """The lock itself could not be established -- read-only vault, no flock support.

    Distinct from a timeout because the remedy is different: a timeout means
    wait or retry, this means the vault cannot be written from here at all.
    """


class _VaultLockState:
    """Per-vault bookkeeping. One instance per resolved vault path per process."""

    __slots__ = ("guard", "depth", "fd")

    def __init__(self) -> None:
        # Re-entrant, so nesting inside one thread is free, and exclusive, so a
        # second thread waits for the flock holder instead of walking past the
        # depth counter unlocked.
        self.guard = threading.RLock()
        self.depth = 0
        self.fd: int | None = None


_states_guard = threading.Lock()
_states: dict[str, _VaultLockState] = {}


def _vault_key(vault: str | Path) -> str:
    """The identity of a vault: its real path, symlinks and `..` resolved.

    Two names for one vault must map to one counter, or a process that reaches
    it both ways opens a second descriptor and blocks on its own lock forever.
    """
    return os.path.realpath(vault)


def _state_for(key: str) -> _VaultLockState:
    with _states_guard:
        state = _states.get(key)
        if state is None:
            state = _states[key] = _VaultLockState()
        return state


def lock_path(vault: str | Path) -> Path:
    """Where the lock file lives: `<vault>/_index/.lock`, path resolved.

    Under `_index/` because that is vault infrastructure rather than content,
    and hidden so it never appears in Obsidian. Note that `entities.tsv` sits in
    the same directory and is the one file there that `index.rebuild()` cannot
    regenerate -- spec §8's "the indexes are disposable" does not cover it, and
    a row lost from it turns one person into several files.
    """
    return Path(_vault_key(vault)) / "_index" / ".lock"


def _open_lock_file(key: str) -> int:
    """Open (creating if needed) the lock file, or say clearly why not.

    Never returns without a usable descriptor. A read-only vault, a `_index`
    that is somehow a regular file, a lock file whose mode was tightened --
    every one of them raises. Proceeding unlocked because the lock could not be
    taken is the exact bug this module exists to prevent, and it would be
    invisible: the writes would succeed.

    The descriptor is non-inheritable, which `os.open` gives us by default
    (PEP 446). That matters more than it looks: if a subprocess inherited it,
    the lock would outlive a SIGKILLed holder for as long as any child it had
    spawned kept running -- which is precisely the stale lock that choosing
    flock over a lockfile was meant to make impossible.
    """
    index = Path(key) / "_index"
    try:
        index.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise VaultLockUnavailable(
            f"cannot create {index} to lock the vault at {key}: "
            f"{exc.strerror or exc}. Refusing to write to the vault unlocked."
        ) from exc

    path = index / ".lock"
    try:
        return os.open(path, os.O_RDWR | os.O_CREAT, 0o666)
    except OSError as exc:
        raise VaultLockUnavailable(
            f"cannot open the vault lock file {path}: {exc.strerror or exc}. "
            f"Refusing to write to the vault unlocked."
        ) from exc


def _flock_until(fd: int, deadline: float, key: str, timeout: float) -> None:
    """Take the exclusive lock, or raise by `deadline`. Never returns unlocked."""
    delay = _POLL_MIN
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in _CONTENDED:
                raise VaultLockUnavailable(
                    f"cannot lock {Path(key) / '_index' / '.lock'}: "
                    f"{exc.strerror or exc}. This filesystem may not support "
                    f"flock; the vault cannot be written safely from here."
                ) from exc

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise VaultLockTimeout(
                f"could not acquire the vault lock on {key} within {timeout:g}s: "
                f"another process is holding {Path(key) / '_index' / '.lock'}. "
                f"Nothing was written."
            )
        time.sleep(min(delay, remaining))
        delay = min(delay * 2, _POLL_MAX)


def _acquire(vault: str | Path, timeout: float) -> _VaultLockState:
    if timeout < 0:
        raise ValueError(f"timeout must not be negative: {timeout!r}")

    key = _vault_key(vault)
    state = _state_for(key)
    deadline = time.monotonic() + timeout

    # One deadline covers both waits. Charging the caller `timeout` for the
    # thread lock and then `timeout` again for the flock would let a wait the
    # caller bounded at 30s run to 60.
    if not state.guard.acquire(timeout=max(0.0, deadline - time.monotonic())):
        raise VaultLockTimeout(
            f"could not acquire the vault lock on {key} within {timeout:g}s: "
            f"another thread in this process is holding it. Nothing was written."
        )

    try:
        if state.depth == 0:
            fd = _open_lock_file(key)
            try:
                _flock_until(fd, deadline, key, timeout)
            except BaseException:
                os.close(fd)
                raise
            state.fd = fd
        state.depth += 1
    except BaseException:
        # A failed acquisition must leave the counter exactly where it was. A
        # counter stuck above zero would make the NEXT acquisition in this
        # process skip flock entirely and run unlocked -- one loud timeout
        # turned into silent permanent corruption.
        state.guard.release()
        raise

    return state


def _release(state: _VaultLockState) -> None:
    try:
        state.depth -= 1
        if state.depth == 0:
            fd, state.fd = state.fd, None
            if fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    # Closing releases the lock too, so the descriptor is closed
                    # even if the explicit unlock failed. Leaking it would hold
                    # the vault until the process exited.
                    os.close(fd)
    finally:
        state.guard.release()


@contextlib.contextmanager
def vault_lock(
    vault: str | Path, *, timeout: float = DEFAULT_TIMEOUT
) -> Iterator[None]:
    """Hold the vault's writer lock for the duration of the block.

        with vault_lock(vault):
            ...            # read, decide and write, with no other writer inside

    Advisory: it serializes callers that come through here and nothing else, so
    it only works if it wraps the *whole* read-modify-write. Wrapping just the
    save leaves the read outside, and the read is half the race.

    Nesting is free -- an inner `with` on the same vault costs a counter bump
    and releases nothing on exit. Raises `VaultLockTimeout` if another process
    holds it for longer than `timeout`, and `VaultLockUnavailable` if the lock
    cannot be established at all. Neither is an `OSError`; see the module
    docstring.
    """
    state = _acquire(vault, timeout)
    try:
        yield
    finally:
        _release(state)
