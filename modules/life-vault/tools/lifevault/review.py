"""The approval half of the nightly sweep: reading `_review/INBOX.md` back.

`lv-sweep` proposes facts it is not confident enough to file on its own. It
writes each one as JSON under `_review/pending/` and renders them into
`_review/INBOX.md` as checkboxes. Until this module existed, ticking a box did
nothing at all: no code ever read the file back, so the loop the whole capture
layer is built around could not close.

Three decisions run through everything below.

**The JSON is authoritative, the markdown is a view.** `_review/pending/*.json`
is the record of what was proposed; INBOX.md is what was put in front of the user
so they could answer. That asymmetry is what makes a mangled hand-edit
survivable — the proposal cannot be lost by damaging the view.

**A tick and an edit are different tiers.** Ticking a box unchanged means
"Claude said it and the user agreed", which is exactly the definition of
`conf/confirmed` in `_index/tags.tsv`. Rewriting the sentence and then ticking
means the words are now their, which is `conf/stated`. Spec §6 hangs on that
distinction: a derived or confirmed claim may never override something the user
stated, so collapsing the two tiers would let a model's guess retire a fact they
gave us. Nothing in this module promotes a claim to `conf/stated` on their behalf.

**Absence is only a decision when the file is demonstrably intact.** Reading
"this proposal is not in the file" as "they deleted it, reject it forever" is the
worst irreversible action available here, and the held set is by definition the
high-value material: new people, health, supersessions, anything unanchored. A
missing file, a truncated one, or a single line an editor hard-wrapped would
otherwise become a bulk permanent discard. So `apply` refuses the whole run and
writes nothing whenever it cannot account for every proposal it rendered.

The comparison that makes that possible is `_review/.inbox.rendered`, the
byte-exact mirror `lv-sweep` keeps of its last render. It is the only record of
what was actually shown, and without it a proposal that was never rendered
cannot be told from one that was rendered and deleted.

`lv-sweep` owns the format of INBOX.md and this module deliberately does not
re-implement it. Blocks are parsed generically (a `- [ ]` line plus the
indented lines under it) and rewritten by deleting whole blocks out of the
bytes lv-sweep itself produced, so a change to the rendering does not silently
desynchronise the two halves.

**A known gap, stated plainly:** `lv-sweep.hold()` does not consult
`decided.tsv`, so it will re-create the pending JSON for a claim that was
already rejected. Suppression is therefore enforced on the read side here —
`pending()` hides decided claims and `sweep_decided()` archives them — which
means a rejected claim never reaches the user again but does cost one wasted file
write per night. `decisions()` and `is_decided()` are the seam lv-sweep should
call in `hold()` to close it properly.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date as _date
from pathlib import Path

from .atomic import write_atomic

#: `bin/lv`. Filing goes through the CLI as an argv list and nothing else, so
#: this module can never become a second writer with its own idea of the rules.
LV = Path(__file__).resolve().parents[1] / "bin" / "lv"
LV_TIMEOUT = 120

REVIEW_DIR = "_review"
INBOX_NAME = "INBOX.md"
MIRROR_NAME = ".inbox.rendered"
PENDING_DIR = "pending"
DECIDED_NAME = "decided.tsv"
DECIDED_HEADER = "# hold_id\tdecision\tdate\treason\tsession"

#: A markdown task-list item at the start of a line. Deliberately not tied to
#: the rest of lv-sweep's layout: the only structural promise this module makes
#: is that a proposal begins with a checkbox and continues until the next one.
CHECKBOX_RE = re.compile(r"^[ \t]*- \[([ xX])\][ ]?(.*)$")

#: `lv add` exits 0 for these too, but the entry it wrote is not current and
#: never will be, so retrieval will never return it. Reporting one of them as
#: accepted would tell the user their correction landed while the stale fact stays
#: authoritative.
NOT_ACCEPTED = frozenset({"blocked", "duplicate", "conflict"})

#: Mirrors lv-sweep's TITLE_MAX. Duplicated rather than imported because
#: lv-sweep is an extension-less script, and importing it would run its
#: module-level `LIFEVAULT_NO_CAPTURE` side effect inside every caller.
TITLE_MAX = 80

CANDIDATE_FIELDS = ("entity", "kind", "date", "title", "subject", "tags",
                    "source", "body", "quote")


class ReviewRefused(Exception):
    """The inbox could not be interpreted. Nothing was written, nothing decided.

    Deliberately not an OSError and deliberately not raised for anything that
    happened after the first write: a caller must be able to treat this as
    "your file was rejected, fix it and run again" and know the vault is
    untouched. `bin/lv` maps it to exit 1 for exactly that reason.
    """


@dataclass(frozen=True)
class Hold:
    """One parked proposal, as `lv-sweep.hold()` wrote it."""

    id: str
    reason: str
    session: str
    created: str
    candidate: dict
    attempts: int = 0
    last_error: str = ""
    path: Path | None = None

    @property
    def body(self) -> str:
        return str(self.candidate.get("body", ""))

    @property
    def rendered_body(self) -> str:
        """The body as it appears on the checkbox line lv-sweep renders."""
        return " ".join(self.body.replace("\n", " ").split())


@dataclass
class _Block:
    """A proposal as it appears in a markdown file: checkbox line plus detail."""

    start: int
    end: int                       # exclusive
    ticked: bool
    body: str
    lines: list[str] = field(default_factory=list)

    @property
    def signature(self) -> tuple[str, ...]:
        """The detail lines, which the reviewer is not asked to edit.

        This is what identifies a proposal whose BODY they rewrote: the entity,
        date, hold reason, quote and tags are still the ones that were
        rendered. Trailing blank lines are dropped so that deleting or keeping
        the blank line between items is not a difference.
        """
        detail = [line.rstrip() for line in self.lines[1:]]
        while detail and not detail[-1]:
            detail.pop()
        return tuple(detail)


# --- paths ------------------------------------------------------------------

def inbox_path(vault: str | Path) -> Path:
    return Path(vault) / REVIEW_DIR / INBOX_NAME


def mirror_path(vault: str | Path) -> Path:
    return Path(vault) / REVIEW_DIR / MIRROR_NAME


def pending_dir(vault: str | Path) -> Path:
    return Path(vault) / REVIEW_DIR / PENDING_DIR


def _decided_path(vault: Path) -> Path:
    return vault / REVIEW_DIR / DECIDED_NAME


def _archive_dir(vault: Path, kind: str) -> Path:
    return vault / REVIEW_DIR / "archive" / kind


# --- small file helpers -----------------------------------------------------

def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(path, lambda fh: fh.write(text))


def _append_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _tsv(value: object) -> str:
    """One field, guaranteed not to invent a column or a row."""
    return " ".join(str(value).split())


# --- the ledger of decisions ------------------------------------------------

def decisions(vault: str | Path) -> dict[str, str]:
    """hold id -> the most recent decision recorded for it.

    Last row wins: ticking a proposal that was rejected last week is the user
    changing their mind, and the newer answer is the true one.
    """
    path = _decided_path(Path(vault))
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) < 2:
            continue
        out[fields[0]] = fields[1]
    return out


def is_decided(vault: str | Path, hold_id: str) -> bool:
    """The seam `lv-sweep.hold()` should call before writing a pending file.

    Until it does, a rejected claim is re-proposed to disk every night and
    suppressed again on the read side here. See the module docstring.
    """
    return hold_id in decisions(vault)


def _record(vault: Path, hold: Hold, decision: str, when: str) -> None:
    path = _decided_path(vault)
    if not path.exists():
        _append_line(path, DECIDED_HEADER)
    _append_line(path, "\t".join(_tsv(v) for v in (
        hold.id, decision, when, hold.reason, hold.session)))


def _archive(vault: Path, hold: Hold, kind: str) -> None:
    """Move a decided proposal out of pending/, never delete it.

    A rejection is a decision about a claim, not permission to destroy the
    record that the claim was ever made. `_review/archive/` is what makes
    "why did the vault stop proposing this?" a question with an answer.
    """
    if hold.path is None or not hold.path.exists():
        return
    destination = _archive_dir(vault, kind) / hold.path.name
    _write_text(destination, hold.path.read_text(encoding="utf-8"))
    try:
        hold.path.unlink()
    except OSError:
        pass


# --- reading what is parked -------------------------------------------------

def _load_holds(vault: Path, warn: bool = True) -> list[Hold]:
    """Every pending proposal, including ones already decided.

    A file that does not parse is reported and skipped, never repaired and
    never counted as a decision. Skipping is safe precisely because the
    markdown block it belongs to then binds to no hold, so `apply` does nothing
    with it rather than deciding it blind.
    """
    directory = pending_dir(vault)
    if not directory.is_dir():
        return []
    holds: list[Hold] = []
    for path in sorted(directory.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            if warn:
                print(f"lv review: skipping unreadable hold {path.name}: {exc}",
                      file=sys.stderr)
            continue
        candidate = raw.get("candidate")
        hold_id = raw.get("id") or raw.get("hold_id") or path.stem
        if not isinstance(candidate, dict) or "body" not in candidate:
            if warn:
                print(f"lv review: skipping malformed hold {path.name}: "
                      "no candidate body", file=sys.stderr)
            continue
        holds.append(Hold(
            id=str(hold_id),
            reason=str(raw.get("reason", "")),
            session=str(raw.get("session", "")),
            created=str(raw.get("created", "")),
            candidate=candidate,
            attempts=int(raw.get("attempts") or 0),
            last_error=str(raw.get("last_error") or ""),
            path=path,
        ))
    return holds


def pending(vault: str | Path) -> list[Hold]:
    """What is genuinely waiting on the user: parked, and not already decided."""
    vault = Path(vault)
    already = decisions(vault)
    return [h for h in _load_holds(vault) if h.id not in already]


def sweep_decided(vault: str | Path) -> int:
    """Archive parked proposals that were already decided. Returns the count.

    lv-sweep re-creates the pending file for a rejected claim because it does
    not read decided.tsv yet, so without this the inbox would print a proposal
    the user has already answered.
    """
    vault = Path(vault)
    already = decisions(vault)
    moved = 0
    for hold in _load_holds(vault):
        decision = already.get(hold.id)
        if decision is None:
            continue
        _archive(vault, hold, "rejected" if decision == "rejected" else "accepted")
        moved += 1
    return moved


def format_list(vault: str | Path) -> str:
    """Human-readable `--list` output. Also the only place a stuck proposal is
    visible: a filing that keeps failing records its error on the hold, and the
    rendered inbox has nowhere to show it.

    A query, so it archives nothing. A claim lv-sweep re-parked after the user
    rejected it is filtered out here and cleaned up by the next `--apply`; it
    cannot accumulate in the meantime because the re-parked file carries the
    same id, and therefore the same filename, every night.
    """
    vault = Path(vault)
    holds = pending(vault)
    if not holds:
        return "nothing is waiting for review\n"
    lines = [f"{len(holds)} waiting in {REVIEW_DIR}/{INBOX_NAME}", ""]
    for hold in holds:
        candidate = hold.candidate
        lines.append(f"{hold.id}  {candidate.get('entity', '')} · "
                     f"{candidate.get('date', '')} · {hold.reason}")
        lines.append(f"    {hold.rendered_body}")
        if hold.attempts:
            lines.append(f"    ! could not be filed ({hold.last_error}) "
                         f"— {hold.attempts} attempt(s)")
        lines.append("")
    lines.append("Tick a box in the inbox to accept it, rewrite the line and tick "
                 "it to accept your wording, or delete the whole item to reject "
                 "it. Then run `lv review --apply`.")
    return "\n".join(lines) + "\n"


# --- parsing the view -------------------------------------------------------

def _split(text: str) -> tuple[list[str], list[_Block]]:
    """Split a rendered inbox into its preamble and its proposal blocks.

    `split("\\n")` rather than `splitlines()` so that rejoining is byte-exact:
    this module rewrites the file by deleting whole blocks out of it, and a
    line-ending the parser normalised would be a silent edit of a file the user
    owns.
    """
    lines = text.split("\n")
    starts = [i for i, line in enumerate(lines) if CHECKBOX_RE.match(line)]
    prefix = lines[:starts[0]] if starts else lines
    blocks: list[_Block] = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        match = CHECKBOX_RE.match(lines[start])
        blocks.append(_Block(
            start=start, end=end,
            ticked=match.group(1).lower() == "x",
            body=" ".join(match.group(2).split()),
            lines=lines[start:end],
        ))
    return prefix, blocks


def _trimmed(lines: list[str]) -> list[str]:
    out = [line.rstrip() for line in lines]
    while out and not out[-1]:
        out.pop()
    return out


def _strip_blocks(text: str, blocks: list[_Block], drop: set[int]) -> str:
    """Rewrite `text` without the listed blocks, in lv-sweep's own bytes.

    Rebuilding the file from a template here instead would mean re-implementing
    a rendering this module does not own; the next edit to lv-sweep's layout
    would make our render differ from its mirror, and every later nightly
    render would be diverted to INBOX.new.md forever.
    """
    lines = text.split("\n")
    removed = set()
    for index in drop:
        removed.update(range(blocks[index].start, blocks[index].end))
    kept = [line for i, line in enumerate(lines) if i not in removed]
    while kept and not kept[-1].strip():
        kept.pop()
    kept.append("")
    return "\n".join(kept)


def _pair_up(mirror: list[_Block], current: list[_Block],
             mirror_left: list[int], current_left: list[int],
             key) -> list[tuple[int, int]]:
    """Match blocks on `key`, refusing rather than guessing when it is not unique."""
    m_index: dict[object, list[int]] = {}
    for i in mirror_left:
        m_index.setdefault(key(mirror[i]), []).append(i)
    c_index: dict[object, list[int]] = {}
    for i in current_left:
        c_index.setdefault(key(current[i]), []).append(i)

    pairs: list[tuple[int, int]] = []
    for value, mine in m_index.items():
        theirs = c_index.get(value)
        if not theirs:
            continue
        if len(mine) > 1 or len(theirs) > 1:
            raise ReviewRefused(
                "two proposals in the inbox are indistinguishable from each "
                "other, so a tick cannot be attributed to one of them: "
                f"{value!r}. Nothing was decided."
            )
        pairs.append((mine[0], theirs[0]))
    return pairs


# --- filing -----------------------------------------------------------------

def _title_for(text: str, fallback: str) -> str:
    """First sentence, no trailing period, capped.

    Mirrors lv-sweep's `title_from_body`, because a body the user rewrote makes
    the stored title a description of a sentence that is no longer there.
    """
    flat = " ".join((text or "").split())
    match = re.match(r"^(.*?)[.!?](?:\s|$)", flat)
    first = (match.group(1) if match else flat).strip().rstrip(".!?").strip()
    if len(first) > TITLE_MAX:
        first = first[:TITLE_MAX - 3].rstrip() + "..."
    return first or fallback


def _lv_run(argv: list[str], vault: Path) -> subprocess.CompletedProcess:
    """Seam for tests. A deliberately minimal child environment: the vault, a
    PATH and a HOME, and nothing that could switch capture back on."""
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=LV_TIMEOUT,
        stdin=subprocess.DEVNULL,
        env={"LIFE_VAULT": str(vault), "PATH": "/usr/bin:/bin",
             "LIFEVAULT_NO_CAPTURE": "1", "HOME": str(Path.home())})


def _file_through_lv(vault: Path, fields: dict) -> tuple[bool, str, str]:
    """Hand one approved proposal to `lv add`. Returns (accepted, verdict, detail).

    An argv list, never a shell string: the body and the quote are text a model
    wrote about whatever the user happened to paste into a session, and there is
    no version of that which belongs on a command line a shell will parse.

    No vault lock is held across this call. flock is per open file description,
    so a child is not granted a lock its parent holds — the child would spin
    until it timed out and every approval would be recorded as a failure.
    `lv add` takes the real lock itself and these calls are serial.
    """
    argv = [sys.executable, str(LV), "add",
            "--entity", fields["entity"],
            "--kind", fields["kind"],
            "--date", fields["date"],
            "--title", fields["title"],
            "--subject", fields["subject"],
            "--tags", " ".join(fields["tags"]),
            "--source", fields["source"],
            "--body", fields["body"]]
    if fields.get("quote"):
        argv += ["--quote", fields["quote"]]
    try:
        proc = _lv_run(argv, vault)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, "error", f"lv add did not complete: {type(exc).__name__}"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()[:300] or "no output"
        return False, "rejected", f"lv add exited {proc.returncode}: {detail}"
    verdict, _, posting = proc.stdout.strip().partition("\t")
    verdict = verdict or "unknown"
    if verdict in NOT_ACCEPTED:
        return False, verdict, (
            f"the entry was written as {verdict} and is not current. "
            "If this is meant to replace something you stated, rewrite the "
            "line in your own words before ticking it — that files it as "
            "conf/stated, which conf/confirmed is not allowed to outrank."
        )
    return True, verdict, posting


def _note_failure(vault: Path, hold: Hold, error: str) -> None:
    """Record why an approved proposal could not be filed, on the proposal.

    The hold stays pending on purpose. What must not happen is the tick
    silently disappearing overnight with the reason buried in a launchd log —
    so the count and the error are persisted here and printed by `--list`.
    """
    if hold.path is None or not hold.path.exists():
        return
    try:
        raw = json.loads(hold.path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return
    raw["attempts"] = int(raw.get("attempts") or 0) + 1
    raw["last_error"] = error[:300]
    _write_text(hold.path, json.dumps(raw, ensure_ascii=False, indent=2,
                                      sort_keys=True) + "\n")


# --- the two commands that decide things ------------------------------------

def reject(vault: str | Path, hold_id: str, when: str | None = None) -> bool:
    """Record that a claim is never to be proposed again. False if unknown.

    The key is the hold id, which lv-sweep derives from the session, the source
    record's uuid, the entity and the subject — none of which a model authors.
    Keying on the proposed sentence instead would mean a rejection lasted
    exactly one night: an all-held session marks nothing processed, so the same
    evidence is re-extracted tomorrow, the model words the fact differently,
    and the paraphrase arrives with a brand-new identity.
    """
    vault = Path(vault)
    when = when or _date.today().isoformat()
    for hold in _load_holds(vault):
        if hold.id != hold_id:
            continue
        _record(vault, hold, "rejected", when)
        _archive(vault, hold, "rejected")
        return True
    return False


def apply(vault: str | Path, kind: str | None = None,
          when: str | None = None) -> dict:
    """Read `_review/INBOX.md` back and act on every answer in it.

    Ticked unchanged files at `conf/confirmed`; ticked after a rewrite files at
    `conf/stated`; an item deleted from the file is rejected forever; anything
    untouched stays pending. Raises `ReviewRefused` — having written nothing at
    all — if the file cannot be accounted for against the mirror of what was
    rendered.

    `kind` supplies the missing vault directory for proposals lv-sweep parked
    with `unresolved-entity`: it writes `kind=""` when it cannot resolve the
    name, and guessing one here would mint a new entity in the wrong directory,
    which is the alias collision the hold exists to prevent.
    """
    vault = Path(vault)
    when = when or _date.today().isoformat()
    results = {"confirmed": 0, "stated": 0, "rejected": 0, "failed": 0,
               "resuppressed": 0}

    holds = _load_holds(vault)
    if not holds:
        return results

    mirror = mirror_path(vault)
    inbox = inbox_path(vault)
    if not mirror.exists():
        raise ReviewRefused(
            f"{REVIEW_DIR}/{MIRROR_NAME} is missing, so there is no record of "
            "which proposals were put in front of you. A proposal you deleted "
            "cannot be told from one that was never rendered. Nothing was "
            "decided; the next sweep will render the inbox again."
        )
    if not inbox.exists():
        raise ReviewRefused(
            f"{REVIEW_DIR}/{INBOX_NAME} is missing. Treating that as "
            f"'everything was deleted' would reject all {len(holds)} pending "
            "proposals permanently. Nothing was decided; the next sweep will "
            "render it again."
        )

    mirror_text = mirror.read_text(encoding="utf-8")
    inbox_text = inbox.read_text(encoding="utf-8")
    mirror_prefix, mirror_blocks = _split(mirror_text)
    inbox_prefix, inbox_blocks = _split(inbox_text)

    if _trimmed(mirror_prefix) != _trimmed(inbox_prefix):
        raise ReviewRefused(
            f"{REVIEW_DIR}/{INBOX_NAME} is not the file that was rendered — the "
            "text above the first proposal has changed, so it may be "
            "truncated, overwritten, or left with the indented detail lines of "
            "an item whose checkbox line alone was deleted. Nothing was decided "
            "and your file was left exactly as it is. To reject a proposal, "
            "delete the whole item including its indented lines; delete the "
            "file entirely if you want a fresh copy rendered."
        )

    # Which proposal each rendered block IS. Matching on the body is what makes
    # this independent of lv-sweep's layout; two proposals that render the same
    # sentence are refused rather than guessed between.
    by_body: dict[str, list[Hold]] = {}
    for hold in holds:
        by_body.setdefault(hold.rendered_body, []).append(hold)
    binding: dict[int, Hold | None] = {}
    for index, block in enumerate(mirror_blocks):
        matches = by_body.get(block.body, [])
        if len(matches) > 1:
            raise ReviewRefused(
                f"{len(matches)} pending proposals share the wording "
                f"{block.body!r}, so a tick cannot be attributed to one of "
                "them. Nothing was decided."
            )
        binding[index] = matches[0] if matches else None

    mirror_left = list(range(len(mirror_blocks)))
    inbox_left = list(range(len(inbox_blocks)))
    pairs: list[tuple[int, int]] = []

    # Untouched items first, on body AND detail, so an item whose wording was
    # rewritten cannot steal the identity of one that was left alone.
    for key in (lambda b: (b.body, b.signature), lambda b: b.signature):
        matched = _pair_up(mirror_blocks, inbox_blocks, mirror_left, inbox_left, key)
        pairs.extend(matched)
        used_mirror = {m for m, _ in matched}
        used_inbox = {i for _, i in matched}
        mirror_left = [i for i in mirror_left if i not in used_mirror]
        inbox_left = [i for i in inbox_left if i not in used_inbox]

    # A block still in the file that matches nothing rendered is damage, not an
    # answer: an editor hard-wrapping one long proposal is enough to produce
    # it. Reading it as a deletion would permanently discard a proposal that is
    # still sitting in the file in front of them.
    if inbox_left:
        broken = [inbox_blocks[i].body[:60] for i in inbox_left]
        raise ReviewRefused(
            f"{len(inbox_left)} item(s) in {REVIEW_DIR}/{INBOX_NAME} do not "
            "match anything that was rendered — most often an editor "
            f"hard-wrapped a long line: {broken}. Nothing was decided and your "
            "file was not touched. To reject a proposal, delete the whole item "
            "including its indented detail lines."
        )

    # Everything above this line can refuse. Nothing above it writes.
    already = decisions(vault)
    drop_from_mirror: set[int] = set()
    drop_from_inbox: set[int] = set()

    for mirror_index, inbox_index in sorted(pairs):
        hold = binding[mirror_index]
        block = inbox_blocks[inbox_index]
        if hold is None:
            if block.ticked:
                print(f"lv review: ticked item {block.body[:60]!r} has no pending "
                      "proposal behind it; ignored", file=sys.stderr)
            continue
        if not block.ticked:
            if already.get(hold.id) == "rejected":
                _archive(vault, hold, "rejected")
                drop_from_mirror.add(mirror_index)
                drop_from_inbox.add(inbox_index)
                results["resuppressed"] += 1
            continue

        accepted, kind_used, detail = _accept(vault, hold, block, kind, when)
        if accepted:
            drop_from_mirror.add(mirror_index)
            drop_from_inbox.add(inbox_index)
            results[kind_used] += 1
        else:
            print(f"lv review: {hold.id} could not be filed — {detail}",
                  file=sys.stderr)
            _note_failure(vault, hold, detail)
            results["failed"] += 1

    for mirror_index in mirror_left:
        hold = binding[mirror_index]
        if hold is None:
            continue
        if already.get(hold.id) == "rejected":
            _archive(vault, hold, "rejected")
            results["resuppressed"] += 1
        else:
            _record(vault, hold, "rejected", when)
            _archive(vault, hold, "rejected")
            results["rejected"] += 1
        drop_from_mirror.add(mirror_index)

    # A proposal parked AFTER the last render was never shown, so it cannot
    # have been deleted. It simply waits for tonight's render -- unless it is a
    # claim already answered, which lv-sweep re-parks because it does not read
    # decided.tsv. Those are archived here so pending/ stays a true statement of
    # what is undecided.
    results["resuppressed"] += sweep_decided(vault)

    if drop_from_mirror or drop_from_inbox:
        # The mirror keeps the pristine rendering minus what was decided; the
        # inbox keeps HIS file minus what was decided. They come out equal
        # exactly when nothing is left unconsumed — which is the signal
        # lv-sweep reads before deciding whether it may re-render.
        _write_text(mirror, _strip_blocks(mirror_text, mirror_blocks,
                                          drop_from_mirror))
        _write_text(inbox, _strip_blocks(inbox_text, inbox_blocks,
                                         drop_from_inbox))
    return results


def _accept(vault: Path, hold: Hold, block: _Block, kind: str | None,
            when: str) -> tuple[bool, str, str]:
    """File one approved proposal. Returns (accepted, "confirmed"/"stated", detail)."""
    candidate = hold.candidate
    edited = block.body != hold.rendered_body

    entity_kind = candidate.get("kind") or kind or ""
    if not entity_kind:
        return False, "", (
            "this proposal has no vault directory (kind) because the entity "
            f"{candidate.get('entity', '')!r} is new, and guessing one would "
            "file it in the wrong place. Re-run as "
            "`lv review --apply --kind people` (or orgs, projects, ...)."
        )

    tags = [t for t in candidate.get("tags", []) if not str(t).startswith("conf/")]
    if edited:
        # Their words now, so the anchor is their words too. Carrying the model's
        # original quote forward would attach a citation to a sentence that
        # citation does not support — which is the one thing spec §12.1's
        # verbatim-span rule exists to make impossible.
        conf, body, quote = "conf/stated", block.body, block.body
        source = f"review/{when}"
        title = _title_for(block.body, candidate.get("title", ""))
    else:
        conf, body = "conf/confirmed", candidate.get("body", "")
        quote = candidate.get("quote", "")
        source = candidate.get("source", "")
        title = candidate.get("title", "") or _title_for(body, "")
    tags.append(conf)

    accepted, verdict, detail = _file_through_lv(vault, {
        "entity": candidate.get("entity", ""), "kind": entity_kind,
        "date": candidate.get("date", ""), "title": title,
        "subject": candidate.get("subject", ""), "tags": tags,
        "source": source, "body": body, "quote": quote,
    })
    if not accepted:
        return False, "", f"{verdict}: {detail}"
    _record(vault, hold, "accepted", when)
    _archive(vault, hold, "accepted")
    return True, "stated" if edited else "confirmed", verdict
