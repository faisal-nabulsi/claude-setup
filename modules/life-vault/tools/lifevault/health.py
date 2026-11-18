# tools/lifevault/health.py
"""Is capture actually working today?

The worst outcome this system can produce is not a crash. It is the user
believing capture works while it silently stopped months ago -- a Stop hook
de-registered by an unrelated `settings.json` edit, a launchd agent that
stopped firing after an OS upgrade, a nightly sweep erroring into a log nobody
reads. None of those announce themselves, and every one of them leaves a vault
that looks fine.

So this module may be wrong in exactly one direction. Reporting a problem that
turns out to be nothing costs a minute. Reporting OK on a dead system costs
however many months pass before anyone happens to look. Every judgement call
below resolves that way, which is why there are FOUR statuses and not two:

    ok       measured, and healthy
    stale    measured, and nothing has happened for longer than expected
    broken   measured, and it is definitely not working
    unknown  COULD NOT BE MEASURED

`unknown` exists so that "I don't know" can never be spelled the same way as
"it's fine". `HealthReport.ok` is True only when every single check is `ok`,
and no code path in this file turns an unknown into an ok.

Thresholds are set for a LAPTOP, not a server. launchd does not wake a sleeping
Mac: a missed StartCalendarInterval fires at the next wake, so an overnight
sleep, a weekend or a travel day routinely produces a gap of many hours with
nothing wrong at all. Alarming on those trains the reader to ignore the alarm,
which is the same failure as having no alarm, arrived at more slowly. Hence 36h
for the hourly harvester and 72h for the nightly sweep.

stdlib only, by rule: this has to run under /usr/bin/python3 from a launchd
job with a minimal environment, not just under the project venv.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .capture import transcript as tx

# --- statuses ------------------------------------------------------------

OK = "ok"
STALE = "stale"
BROKEN = "broken"
UNKNOWN = "unknown"

# --- the checks, in the order a human wants to read them -----------------

CHECKS: tuple[str, ...] = (
    "capture-enabled",
    "hook-registered",
    "hook-firing",
    "harvest",
    "sweep",
    "launchd",
    "backlog",
    "disk",
    "roots",
)

#: How long each time-based check may go without news before it is `stale`.
#: The hourly harvester and the nightly sweep have different expectations, and
#: collapsing them into one number would either alarm every morning or hide a
#: dead harvester for three days.
STALE_HOURS: dict[str, float] = {
    # Hourly job. 36h clears an overnight sleep and a long weekend morning.
    "harvest": 36.0,
    # Nightly job at 03:15. 72h clears a weekend away with the lid shut.
    "sweep": 72.0,
    # Pointers are consumed by the next hourly harvest; older than that and
    # nothing is draining the queue.
    "backlog": 36.0,
    # The hook's heartbeat only advances when the user ends a session, so this is
    # deliberately a week: long enough to survive a holiday, short enough that
    # "capture has not proved itself alive since March" cannot happen.
    "hook-firing": 168.0,
}

#: A transcript newer than the hook's last heartbeat by more than this is
#: positive evidence the hook did not fire: a session ended and no pointer
#: appeared. Below it, the harvest simply has not run yet.
HOOK_GRACE_HOURS = 36.0

#: The vault lives on a 228GB disk that runs chronically full. A vault that
#: cannot write is a vault that captures nothing, and the failure arrives as a
#: silent OSError inside an hourly job.
DISK_WARN_GB = 10.0
DISK_CRITICAL_GB = 2.0

#: More pointers than this means nothing has drained the queue in a long time,
#: regardless of how old the oldest one is.
BACKLOG_MAX = 500

LAUNCHD_PREFIX = "{{LABEL_PREFIX}}.lifevault"
HARVEST_LABEL = f"{LAUNCHD_PREFIX}.harvest"
SWEEP_LABEL = f"{LAUNCHD_PREFIX}.sweep"

#: Overridable the same way and for the same reason as transcript.LV_PROJECTS:
#: a test must be able to point this at a fixture instead of asserting against
#: whatever the developer's machine happens to have bootstrapped today.
LAUNCHCTL = "/bin/launchctl"


# --- results -------------------------------------------------------------

@dataclass(frozen=True)
class CheckResult:
    """One question, answered, with the evidence that answered it."""

    name: str
    status: str
    detail: str
    remedy: str = ""
    age_hours: float | None = None

    @property
    def alarming(self) -> bool:
        return self.status != OK

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "remedy": self.remedy,
            "age_hours": (None if self.age_hours is None
                          else round(self.age_hours, 2)),
        }


@dataclass(frozen=True)
class HealthReport:
    vault: str
    checked_at: str
    checks: tuple[CheckResult, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        """True only when EVERY check is `ok`. An unknown is not an ok."""
        return all(c.status == OK for c in self.checks)

    @property
    def failing(self) -> tuple[CheckResult, ...]:
        """Definitely wrong: something is stale or broken."""
        return tuple(c for c in self.checks if c.status in (STALE, BROKEN))

    @property
    def undetermined(self) -> tuple[CheckResult, ...]:
        return tuple(c for c in self.checks if c.status == UNKNOWN)

    @property
    def alarming(self) -> tuple[CheckResult, ...]:
        return tuple(c for c in self.checks if c.alarming)

    @property
    def exit_code(self) -> int:
        """0 healthy, 1 something is wrong, 2 could not tell.

        A monitoring script that treats any non-zero as "look at this" is
        correct with no further work. One that reads the codes can tell a dead
        component (1) from a check that could not run (2) -- and 2 is never
        collapsed into 0, because the entire purpose of this file is that
        "I could not measure it" must not be spelled the same way as "fine".
        """
        if self.failing:
            return 1
        if self.undetermined:
            return 2
        return 0

    def status_of(self, name: str) -> str:
        """The verdict for one check. Raises KeyError if there is no such check.

        Deliberately not `.get(name, OK)`: a monitoring script that watches a
        check which was later renamed must break loudly rather than start
        reporting a green light for a question nobody is asking any more.
        """
        for check in self.checks:
            if check.name == name:
                return check.status
        raise KeyError(name)

    def get(self, name: str) -> CheckResult:
        for check in self.checks:
            if check.name == name:
                return check
        raise KeyError(name)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "checked_at": self.checked_at,
            "vault": self.vault,
            "checks": [c.to_dict() for c in self.checks],
        }

    def summary(self) -> str:
        """Short, human, and stamped. Never a bare OK.

        Every line carries the time it was taken, so that if health itself
        stops running the last surviving line is self-evidently old to a reader
        instead of quietly reassuring.
        """
        verdict = ("capture is OK" if self.ok else
                   "CAPTURE IS NOT KNOWN TO BE WORKING")
        out = [f"{verdict} as of {self.checked_at}", ""]
        width = max(len(name) for name in CHECKS)
        for check in self.checks:
            age = ("" if check.age_hours is None
                   else f"  [{_ago(check.age_hours)}]")
            out.append(f"  {check.name.ljust(width)}  "
                       f"{check.status.upper().ljust(7)} {check.detail}{age}")
        if self.alarming:
            out += ["", "What to do:"]
            for check in self.alarming:
                if check.remedy:
                    out.append(f"  {check.name}: {check.remedy}")
        return "\n".join(out) + "\n"

    def banner(self) -> str:
        """The text of `_health/BANNER.md`. Empty EXACTLY when all is well.

        A non-empty banner is itself the alarm, so nothing decorative is ever
        written here -- no "all clear", no heading, not a newline. Any reader
        can use `if banner.strip()` and be right.
        """
        if self.ok:
            return ""
        out = [f"**Life vault capture is not known to be working** "
               f"(checked {self.checked_at}).", ""]
        for check in self.alarming:
            line = f"- [{check.status}] {check.name}: {check.detail}"
            if check.remedy:
                line += f"\n  Fix: {check.remedy}"
            out.append(line)
        out += ["", f"Full report: `{self.vault}/tools/bin/lv health`", ""]
        return "\n".join(out)


# --- small measurements --------------------------------------------------

def _stamp(when: float | None = None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(when))


def _ago(hours: float) -> str:
    if hours < 1.0:
        return f"{hours * 60:.0f}m ago"
    if hours < 48.0:
        return f"{hours:.0f}h ago"
    return f"{hours / 24:.0f}d ago"


def _age_hours(path: Path, now: float) -> float | None:
    """Hours since `path` was last written, or None if it cannot be read."""
    try:
        return (now - path.stat().st_mtime) / 3600.0
    except OSError:
        return None


def _newest_age_hours(paths: Iterable[Path], now: float) -> float | None:
    ages = [a for a in (_age_hours(p, now) for p in paths) if a is not None]
    return min(ages) if ages else None


def _oldest_age_hours(paths: Iterable[Path], now: float) -> float | None:
    ages = [a for a in (_age_hours(p, now) for p in paths) if a is not None]
    return max(ages) if ages else None


def launchd_loaded_labels(launchctl: str | None = None) -> set[str] | None:
    """The set of loaded launchd labels, or None if it could not be read.

    None is the important return value. `launchctl list` failing, timing out,
    or not existing tells us NOTHING about whether the agents are loaded, and
    the caller must turn that into `unknown` rather than into either verdict.
    """
    binary = launchctl or os.environ.get("LV_LAUNCHCTL") or LAUNCHCTL
    try:
        result = subprocess.run([binary, "list"], capture_output=True,
                                text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    labels = set()
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if parts:
            labels.add(parts[-1].strip())
    return labels


# --- remedies ------------------------------------------------------------
#
# Every command printed below is verified before it is printed. A remedy that
# names a script which is not installed is worse than no remedy at all: it can
# be followed to the letter, forever, while nothing gets fixed -- and after the
# second `command not found` the whole report stops being believed.

def _run_advice(vault: Path, name: str, args: str = "", then: str = "") -> str:
    path = vault / "tools" / "bin" / name
    if path.exists() and os.access(path, os.X_OK):
        return f"run `{path}{(' ' + args) if args else ''}` by hand{then}"
    return (f"{path} is missing, so there is nothing to run yet: restore it "
            f"with `/usr/bin/git -C {vault} checkout -- tools/bin/{name}` first")


def _reregister_advice(vault: Path) -> str:
    return _run_advice(
        vault, "lv-install", "--hook-only",
        then=", then send one message in any project and check that "
             f"{vault}/_queue/pending gains a file")


def _launchctl_check() -> str:
    return "check with `/bin/launchctl list | grep lifevault`"


# --- the checks ----------------------------------------------------------

def _check_harvest(vault: Path, now: float) -> CheckResult:
    """When did the evidence spool last grow?

    The spool is the harvester's only durable output, so its newest mtime is a
    lower bound on when the hourly job last did real work. A quiet week looks
    the same as a dead harvester here, and that is on purpose: `stale` says
    "nothing has happened", which is exactly what is known.
    """
    spool = vault / "sources" / "claude-code" / "evidence"
    if not spool.is_dir():
        return CheckResult(
            "harvest", UNKNOWN,
            f"no evidence spool at {spool}, so the harvester has never written "
            "anything and there is nothing to date it by",
            _run_advice(vault, "lv-harvest", "--dry-run",
                        then=" to see what it would spool"))
    age = _newest_age_hours(spool.glob("*.jsonl"), now)
    if age is None:
        return CheckResult(
            "harvest", UNKNOWN,
            "the evidence spool is empty, so there is nothing to date the last "
            "harvest by",
            _run_advice(vault, "lv-harvest", "--dry-run",
                        then=" to see what it would spool"))
    if age > STALE_HOURS["harvest"]:
        return CheckResult(
            "harvest", STALE,
            f"the evidence spool has not grown in {_ago(age)} "
            f"(expected within {STALE_HOURS['harvest']:.0f}h)",
            _run_advice(vault, "lv-harvest", then=f", then {_launchctl_check()}"),
            age_hours=age)
    return CheckResult("harvest", OK, "the evidence spool grew recently",
                       age_hours=age)


def _check_hook_registered(vault: Path, home: Path) -> CheckResult:
    """Is `lv-capture` still registered as a Stop hook?

    Read-only, always. This function never writes ~/.claude/settings.json: that
    file also holds the model selection, the plugin list and auth-adjacent
    config, and a health check has no business editing it.

    Two ways the registration outlives the script, both of which leave a hook
    error after every response in every project while the JSON still looks
    right: lv-capture deleted or stripped of its +x bit (a `git checkout` of an
    older commit in ~/life does it, since the script lives in a git working
    tree), and a stale absolute path left behind by a vault that moved.
    """
    settings = home / ".claude" / "settings.json"
    fix = _reregister_advice(vault)
    try:
        raw = settings.read_text(encoding="utf-8")
    except FileNotFoundError:
        return CheckResult("hook-registered", BROKEN,
                           f"{settings} does not exist, so no Stop hook is "
                           "registered and no session is being captured", fix)
    except OSError as exc:
        return CheckResult("hook-registered", UNKNOWN,
                           f"{settings} could not be read ({exc.strerror}), so "
                           "whether the hook is registered is unknown",
                           f"check its permissions with `/bin/ls -l {settings}`")
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return CheckResult(
            "hook-registered", BROKEN,
            f"{settings} is not valid JSON, so Claude Code loads no hooks from "
            "it at all",
            "fix the JSON by hand first (the installer refuses to guess at "
            f"what a malformed file meant), then {fix}")
    if not isinstance(data, dict):
        return CheckResult("hook-registered", BROKEN,
                           f"{settings} is not a JSON object", fix)
    hooks = data.get("hooks")
    stop = hooks.get("Stop") if isinstance(hooks, dict) else None
    commands = []
    for group in stop if isinstance(stop, list) else []:
        if not isinstance(group, dict):
            continue
        for entry in group.get("hooks") or []:
            if isinstance(entry, dict):
                commands.append(str(entry.get("command", "")))
    ours = [c for c in commands if "lv-capture" in c]
    if not ours:
        return CheckResult("hook-registered", BROKEN,
                           "no lv-capture Stop hook is registered in "
                           f"{settings}, so sessions end without leaving a "
                           "pointer and nothing is captured", fix)
    for command in ours:
        # The registered form is `'<path>'; exit 0` -- see lv-install.
        script = Path(command.split(";")[0].strip().strip("'\""))
        if not script.exists():
            return CheckResult("hook-registered", BROKEN,
                               f"the registered Stop hook points at {script}, "
                               "which does not exist", fix)
        if not os.access(script, os.X_OK):
            return CheckResult("hook-registered", BROKEN,
                               f"the registered Stop hook at {script} is not "
                               "executable", fix)
    return CheckResult("hook-registered", OK,
                       f"lv-capture is registered as a Stop hook in {settings}")


def _check_hook_firing(vault: Path, projects: Path | None, now: float) -> CheckResult:
    """When did a session pointer last appear?

    The live queue cannot be the proof of life. `harvest()` deletes the pointer
    for every session it processes, so within an hour of any ordinary session
    `_queue/pending` is empty -- and reading that emptiness as "the hook is
    dead" would raise the alarm roughly 23 hours out of every 24, which trains
    it out of the reader inside a week. So harvest stamps `_health/hook.seen`
    with the newest pointer mtime it saw before deleting anything, and the
    heartbeat is the newer of that stamp and any pointer still in the queue.

    Silence on its own is not a fault: the user may simply not have used Claude
    Code. What convicts the hook is silence WITH new transcripts, because a
    session that ended must have left a pointer. Silence without transcripts
    old enough to compare is `stale` -- no fault proved, but no proof of life
    either, and after a week of that the honest answer is not `ok`.
    """
    beats: list[Path] = [vault / "_health" / "hook.seen"]
    pending = vault / "_queue" / "pending"
    if pending.is_dir():
        try:
            beats += [p for p in pending.iterdir() if not p.name.startswith(".")]
        except OSError:
            pass
    beat = _newest_age_hours(beats, now)
    session = _newest_transcript_age_hours(projects, now)
    threshold = STALE_HOURS["hook-firing"]

    if beat is None:
        if session is None:
            return CheckResult(
                "hook-firing", UNKNOWN,
                "no session pointer has ever been recorded, and no transcripts "
                "were found to compare against",
                "send one message in any project, then "
                + _run_advice(vault, "lv", "health", then=" again"))
        if session < threshold:
            return CheckResult(
                "hook-firing", BROKEN,
                f"a session ended {_ago(session)} and no pointer has ever been "
                "written, so the Stop hook is not firing",
                _reregister_advice(vault), age_hours=session)
        return CheckResult(
            "hook-firing", UNKNOWN,
            "no session pointer has ever been recorded, and the newest "
            f"transcript is itself {_ago(session)}",
            "send one message in any project, then "
            + _run_advice(vault, "lv", "health", then=" again"),
            age_hours=session)

    if session is not None and beat > session + HOOK_GRACE_HOURS:
        return CheckResult(
            "hook-firing", BROKEN,
            f"the newest session ended {_ago(session)} but the last pointer is "
            f"from {_ago(beat)}: sessions are ending and the Stop hook is "
            "writing nothing",
            _reregister_advice(vault), age_hours=beat)
    if beat > threshold:
        return CheckResult(
            "hook-firing", STALE,
            f"no session pointer in {_ago(beat)}, so nothing has proved the "
            "Stop hook alive for over "
            f"{threshold / 24:.0f} days",
            _reregister_advice(vault), age_hours=beat)
    return CheckResult("hook-firing", OK,
                       "the Stop hook left a pointer recently", age_hours=beat)


def _check_sweep(vault: Path, now: float) -> CheckResult:
    """When did the receipt log last gain rows?

    The nightly sweep writes one receipt row per session per run, including for
    the sessions that produced nothing. No rows means nothing has been
    extracted or filed -- which is a measurement, not a shrug, so it is
    `broken` and not `unknown`.
    """
    log = vault / "sources" / "claude-code" / "receipts.tsv"
    fix = _run_advice(vault, "lv-sweep", then=f", then {_launchctl_check()}")
    try:
        raw = log.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return CheckResult("sweep", BROKEN,
                           f"there is no receipt log at {log}, so the sweep has "
                           "never filed anything: nothing is being extracted "
                           "from the evidence spool", fix)
    except OSError as exc:
        return CheckResult("sweep", UNKNOWN,
                           f"{log} could not be read ({exc.strerror})",
                           f"read it by hand with `/bin/cat {log}`")
    rows = [l for l in raw.splitlines() if l.strip() and not l.startswith("#")]
    if not rows:
        return CheckResult("sweep", BROKEN,
                           f"{log} exists but has never gained a row, so the "
                           "sweep has never filed anything", fix)
    age = _age_hours(log, now)
    if age is None:
        return CheckResult("sweep", UNKNOWN,
                           f"{len(rows)} receipt row(s), but {log} could not be "
                           "dated", fix)
    if age > STALE_HOURS["sweep"]:
        return CheckResult(
            "sweep", STALE,
            f"{len(rows)} receipt row(s); the last was added {_ago(age)} "
            f"(expected within {STALE_HOURS['sweep']:.0f}h)", fix, age_hours=age)
    return CheckResult("sweep", OK,
                       f"{len(rows)} receipt row(s); the last was added "
                       f"{_ago(age)}", age_hours=age)


def _check_launchd(vault: Path, home: Path, loaded: set[str] | None) -> CheckResult:
    """Are both agents actually bootstrapped?

    `launchctl list` not answering tells us nothing about whether the jobs are
    loaded, so it is `unknown`. Reading a failed probe as healthy is precisely
    the shape of failure this module exists to prevent.
    """
    if loaded is None:
        return CheckResult(
            "launchd", UNKNOWN,
            "`launchctl list` could not be read, so whether the hourly and "
            "nightly agents are loaded is unknown",
            "check by hand with `/bin/launchctl list`")
    missing = [label for label in (HARVEST_LABEL, SWEEP_LABEL)
               if label not in loaded]
    if missing:
        agents = home / "Library" / "LaunchAgents"
        plists = " ".join(f"{agents}/{label}.plist" for label in missing)
        return CheckResult(
            "launchd", BROKEN,
            f"not bootstrapped, so nothing runs on a schedule: "
            f"{', '.join(missing)}",
            f"load with `/bin/launchctl bootstrap gui/$(id -u) {plists}`; if "
            f"the plist is not in {agents}, "
            + _run_advice(vault, "lv-install", "--plists-only",
                          then=" to write it"))
    return CheckResult("launchd", OK, "both launchd agents are loaded")


def _check_disk(vault: Path, home: Path, disk_usage: Callable) -> CheckResult:
    """The vault lives on a 228GB disk documented as chronically full.

    A full disk does not announce itself as a capture failure. It announces
    itself as an OSError inside an hourly job whose log nobody reads, and the
    spool simply stops growing.
    """
    try:
        usage = disk_usage(vault)
    except OSError as exc:
        return CheckResult("disk", UNKNOWN,
                           f"free space on {vault} could not be read "
                           f"({exc.strerror})",
                           f"check by hand with `/bin/df -h {vault}`")
    free_gb = usage.free / 1024 ** 3
    total_gb = usage.total / 1024 ** 3
    fix = (f"find the space with `/bin/df -h {vault}` and "
           f"`/usr/bin/du -sh {home}/Library/Caches`")
    if free_gb < DISK_CRITICAL_GB:
        return CheckResult("disk", BROKEN,
                           f"only {free_gb:.1f}GB free of {total_gb:.0f}GB; "
                           "writes into the vault will start failing", fix)
    if free_gb < DISK_WARN_GB:
        return CheckResult("disk", STALE,
                           f"{free_gb:.1f}GB free of {total_gb:.0f}GB, below "
                           f"the {DISK_WARN_GB:.0f}GB mark", fix)
    return CheckResult("disk", OK, f"{free_gb:.0f}GB free of {total_gb:.0f}GB")


def _check_backlog(vault: Path, now: float) -> CheckResult:
    """How many pointers are unprocessed, and how old is the oldest?

    A missing `_queue/pending` is a silent kill: lv-capture exits 0 without
    writing anything when the directory is absent, and `_queue/` is gitignored,
    so one `git clean -xdf` in ~/life switches capture off with no error
    anywhere. That is `broken`, not a shrug -- and it is a different state from
    the deliberate `_queue/CAPTURE-OFF` kill switch, which is its own check.
    """
    pending = vault / "_queue" / "pending"
    if not pending.is_dir():
        return CheckResult(
            "backlog", BROKEN,
            f"{pending} does not exist, and the Stop hook exits without "
            "writing anything when it is absent, so nothing is being queued",
            f"recreate it with `/bin/mkdir -p {pending}`")
    try:
        pointers = [p for p in pending.iterdir() if not p.name.startswith(".")]
    except OSError as exc:
        return CheckResult("backlog", UNKNOWN,
                           f"{pending} could not be listed ({exc.strerror})",
                           f"check it by hand with `/bin/ls -la {pending}`")
    if not pointers:
        return CheckResult("backlog", OK, "0 unprocessed session pointers")
    oldest = _oldest_age_hours(pointers, now)
    fix = _run_advice(vault, "lv-harvest",
                      then=f" to drain the queue, then {_launchctl_check()}")
    if oldest is not None and oldest > STALE_HOURS["backlog"]:
        return CheckResult(
            "backlog", STALE,
            f"{len(pointers)} unprocessed session pointer(s); the oldest has "
            f"waited {_ago(oldest)}, and an hourly harvest should clear it "
            f"within {STALE_HOURS['backlog']:.0f}h", fix, age_hours=oldest)
    if len(pointers) > BACKLOG_MAX:
        return CheckResult(
            "backlog", STALE,
            f"{len(pointers)} unprocessed session pointers, more than the "
            f"{BACKLOG_MAX} a working harvest ever leaves", fix,
            age_hours=oldest)
    detail = f"{len(pointers)} unprocessed session pointer(s)"
    if oldest is not None:
        detail += f", oldest {_ago(oldest)}"
    return CheckResult("backlog", OK, detail, age_hours=oldest)


def _check_capture_enabled(vault: Path) -> CheckResult:
    """The documented kill switch, reported as itself.

    Switched off on purpose is not a mystery failure, and it is not working
    either. The question this command answers is "is capture working today?",
    and when the marker exists the answer is no -- with the reason named, so
    nobody spends an afternoon debugging a decision.
    """
    marker = vault / "_queue" / "CAPTURE-OFF"
    if marker.exists():
        return CheckResult(
            "capture-enabled", BROKEN,
            f"capture is switched off on purpose: {marker} exists, so the "
            "harvester returns immediately and nothing is spooled",
            f"switch it back on with `/bin/rm {marker}`")
    return CheckResult("capture-enabled", OK, "capture is switched on")


def _newest_transcript_age_hours(projects: Path | None, now: float) -> float | None:
    """Age of the newest Claude Code transcript, or None if there are none.

    Measured through capture.transcript so that health counts a session the
    same way the harvester does. A status command that measures less than the
    job it reports on can only ever tell a subset of the truth while looking
    authoritative.
    """
    try:
        paths = tx.depth1_transcripts(projects)
    except OSError:
        return None
    return _newest_age_hours(paths, now)


# --- the whole answer ----------------------------------------------------

def _check_roots(home: Path) -> CheckResult:
    """Is every declared source root still where the vault thinks it is?

    This check exists because of a specific, silent failure. `roots.py` declared
    the Takeout parent as one folder name; the directory was renamed; `roots.takeout_roots()` began returning 0; and every Google
    product became invisible to any sweep driven by that module. Nothing raised.
    A sweep ran, read nothing, and reported success -- because a root that is
    absent produces exactly the same zero facts as a root that is empty.

    `roots.verify()` catches it, and did, but only when a person thought to call
    it. Nobody did for a week. This check is the same question asked by
    something that runs unattended every day, which is the only place it is
    worth anything.

    UNKNOWN rather than OK when the roots module cannot be imported or read:
    "I could not tell" must never be spelled the same way as "fine".
    """
    try:
        from . import roots as roots_module
    except ImportError:
        # This shared copy ships without roots.py (the archive-backfill map is personal
        # to each machine). No roots declared means nothing to check.
        return CheckResult("roots", OK, "no archive roots declared (roots.py not installed)")
    except Exception as exc:                     # pragma: no cover - import guard
        return CheckResult("roots", UNKNOWN,
                           f"could not load the root declarations: {exc}")
    try:
        statuses = roots_module.survey(roots_module.all_roots(home=home))
        exports = roots_module.takeout_roots(home=home)
    except Exception as exc:
        return CheckResult("roots", UNKNOWN, f"could not survey the roots: {exc}")

    missing = [s for s in statuses if not s.exists]
    if not missing:
        # all_roots includes the globbed exports, so an absent parent is caught
        # above. An EXISTING but empty parent is not, and produces the same
        # silence: takeout_roots() returns 0 and every product goes unread.
        if not exports:
            return CheckResult(
                "roots", BROKEN,
                "the Takeout parent exists but the glob matched no exports, so "
                "every Google product is invisible to a roots-driven sweep",
                remedy=f"check {roots_module.TAKEOUT_PARENTS[0]} actually "
                       f"contains {roots_module.TAKEOUT_GLOB} directories",
            )
        # An export that is present but EMPTY is the failure that actually
        # happened: nine of ten exports vanished and the tenth was left as a
        # directory tree containing zero files. The glob still matched, every
        # declared path still existed, and nothing anywhere went red while
        # 274,414 records' worth of source disappeared.
        hollow = [r.label for r in exports
                  if not any(f.is_file() for f in r.path.rglob("*"))]
        if hollow:
            return CheckResult(
                "roots", BROKEN,
                f"{len(hollow)} of {len(exports)} Takeout exports contain no "
                f"files at all: {', '.join(hollow[:5])}"
                + (f", and {len(hollow) - 5} more" if len(hollow) > 5 else ""),
                remedy="an export whose directories survive but whose files do "
                       "not is data loss, not eviction. Re-request it from "
                       "takeout.google.com; Google can regenerate it.",
            )
        return CheckResult("roots", OK,
                           f"all {len(statuses)} roots present, "
                           f"{len(exports)} Takeout exports")
    names = ", ".join(f"{s.root.label} ({s.root.path})" for s in missing[:4])
    if len(missing) > 4:
        names += f", and {len(missing) - 4} more"
    return CheckResult(
        "roots", BROKEN,
        f"{len(missing)} of {len(statuses)} declared roots are not on this "
        f"machine: {names}",
        remedy="a renamed or deleted directory silently stops being read. Fix "
               "the path in lifevault/roots.py, or drop the root if it is gone "
               "for good.",
    )


def check(
    vault: str | Path,
    *,
    now: float | None = None,
    home: str | Path | None = None,
    projects: str | Path | None = None,
    loaded_agents: Callable[[], set[str] | None] | None = None,
    disk_usage: Callable | None = None,
) -> HealthReport:
    """Answer "is capture working today?" without writing anything.

    Every external dependency is injectable so that a test can build the whole
    world in a temp directory: a health test that reads the developer's real
    settings.json passes or fails depending on what they installed this morning.
    """
    vault = Path(vault)
    now = time.time() if now is None else now
    home = Path(home) if home is not None else Path.home()
    loaded_agents = loaded_agents or launchd_loaded_labels
    disk_usage = disk_usage or shutil.disk_usage

    projects = Path(projects) if projects is not None else None

    results = {
        "capture-enabled": _check_capture_enabled(vault),
        "hook-registered": _check_hook_registered(vault, home),
        "hook-firing": _check_hook_firing(vault, projects, now),
        "harvest": _check_harvest(vault, now),
        "sweep": _check_sweep(vault, now),
        "launchd": _check_launchd(vault, home, loaded_agents()),
        "backlog": _check_backlog(vault, now),
        "disk": _check_disk(vault, home, disk_usage),
        "roots": _check_roots(home),
    }
    return HealthReport(vault=str(vault), checked_at=_stamp(now),
                        checks=tuple(results[name] for name in CHECKS))


# --- writing it down -----------------------------------------------------

def write_banner(vault: str | Path, report: HealthReport) -> Path:
    """Write `_health/BANNER.md`, plus a machine-readable `_health/health.json`.

    The banner is empty exactly when the system is well, so a reader needs no
    parser: non-empty IS the alarm. It is rewritten on every run, including
    when it is empty, so a fault that clears does not leave a stale warning.
    """
    directory = Path(vault) / "_health"
    directory.mkdir(parents=True, exist_ok=True)
    banner = directory / "BANNER.md"
    banner.write_text(report.banner(), encoding="utf-8")
    (directory / "health.json").write_text(
        json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    return banner


def banner_state(vault: str | Path) -> str:
    """"ok", "alarm" or "missing" -- and missing is NOT ok.

    An absent BANNER.md means the health check itself has not run, which is
    exactly the state a dead system leaves behind. Any reader that treats a
    missing file as an empty one has reinvented the silent failure this whole
    module exists to prevent, so the distinction is made here once.
    """
    path = Path(vault) / "_health" / "BANNER.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return "missing"
    return "ok" if not text.strip() else "alarm"
