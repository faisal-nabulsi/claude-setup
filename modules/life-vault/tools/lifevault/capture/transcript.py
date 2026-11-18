# tools/lifevault/capture/transcript.py
"""Safe reading of Claude Code session transcripts.

Every rule here is a response to something measured in the real corpus, not a
precaution. See the plan header for the numbers.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

PROJECTS = Path.home() / ".claude" / "projects"


def projects_dir(override: str | Path | None = None) -> Path:
    """Where transcripts live.

    LV_PROJECTS exists so the orchestrator tests can point at a fixture
    directory. Without it `lv-harvest` under test read all 72 of the user's real
    transcripts, redacted them, and spooled them into tmp_path — non-hermetic,
    slow, and with results that changed depending on what they had been doing
    that day. `harvest()` already took a `projects` argument; the orchestrators
    simply never plumbed it.

    It doubles as the escape hatch if Claude Code ever moves the directory:
    set it in both plists and capture keeps working.
    """
    if override is not None:
        return Path(override)
    return Path(os.environ.get("LV_PROJECTS") or PROJECTS)

SYSREM = re.compile(r"<system-reminder>.*?</system-reminder>\s*", re.S)
CMDBLK = re.compile(r"<command-(?:message|name|args|contents)>.*?</command-\w+>\s*", re.S)
LOCALOUT = re.compile(r"<local-command-(?:stdout|stderr)>.*?</local-command-\w+>\s*", re.S)
INTERRUPT = re.compile(r"^\[Request interrupted by user.*?\]$")

# origin.kind is undocumented, appeared at 2.1.187, and has already been seen
# on a newer build than any of the research covered. An unrecognised value must
# surface as a log line, never as silent data loss.
KNOWN_ORIGIN_KINDS = frozenset({"human", "task-notification"})


@dataclass(frozen=True)
class Turn:
    uuid: str
    session_id: str
    ts: str
    cwd: str
    text: str


def depth1_transcripts(projects: str | Path | None = None) -> list[Path]:
    """Real sessions only: <projects>/<slug>/<uuid>.jsonl and nothing deeper."""
    projects = projects_dir(projects)
    if not projects.is_dir():
        return []
    out: list[Path] = []
    for project in sorted(projects.iterdir()):
        if not project.is_dir():
            continue
        for candidate in sorted(project.glob("*.jsonl")):
            if candidate.is_file():
                out.append(candidate)
    return out


def transcript_for(session_id: str, projects: str | Path | None = None) -> Path | None:
    """Resolve a session id to its transcript by filename.

    Verified across all 72 depth-1 files on this machine: the LAST record's
    sessionId equals the filename in every case. Earlier records in a forked
    file carry the parent's id, which is why records are never filtered by
    sessionId — only the filename is authoritative. The cwd -> project-slug
    mapping is lossy and must never be computed in reverse.
    """
    for path in depth1_transcripts(projects):
        if path.stem == session_id:
            return path
    return None


def iter_records(path: str | Path):
    """Yield parsed records. Splits on b"\\n" ONLY, never splitlines()."""
    data = Path(path).read_bytes()
    for raw in data.split(b"\n"):
        if not raw.strip():
            continue
        try:
            yield json.loads(raw.decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError):
            continue  # truncated tail of a file being appended to right now


def message_text(obj: dict) -> str:
    content = (obj.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def clean(text: str) -> str:
    text = SYSREM.sub("", text)
    text = LOCALOUT.sub("", text)
    text = CMDBLK.sub("", text)
    text = text.strip()
    return "" if INTERRUPT.match(text) else text


def is_human_turn(obj: dict) -> bool:
    """Strict origin.kind == "human".

    The version-aware variant that also accepts origin-absent records on builds
    before 2.1.187 recovers roughly 700 more historical turns, but carries 4.6x
    the character volume and five of the six credential hits in the entire human
    corpus. Legacy recovery is Plan 3's problem, opt-in and heavily redacted.
    """
    if obj.get("type") != "user":
        return False
    if obj.get("isSidechain"):
        return False
    if obj.get("toolUseResult") is not None:
        return False
    if obj.get("isMeta") or obj.get("isCompactSummary"):
        return False
    if obj.get("isVisibleInTranscriptOnly"):
        return False
    origin = obj.get("origin")
    return isinstance(origin, dict) and origin.get("kind") == "human"


def human_turns(path: str | Path) -> list[Turn]:
    """Every human turn, in file order, duplicates included.

    Duplicates are returned rather than collapsed because uuid is not unique
    even within one file, and the caller dedups by SET membership. Collapsing
    here would hide the phenomenon from the ledger.
    """
    out: list[Turn] = []
    for obj in iter_records(path):
        if obj.get("type") == "queue-operation":
            continue  # drafts the user typed and then deleted before sending
        if not is_human_turn(obj):
            continue
        text = clean(message_text(obj))
        if not text:
            continue
        out.append(Turn(
            uuid=obj.get("uuid") or "",
            session_id=obj.get("sessionId") or "",
            ts=obj.get("timestamp") or "",
            cwd=obj.get("cwd") or "",
            text=text,
        ))
    return out


def unknown_origin_kinds(path: str | Path) -> set[str]:
    kinds: set[str] = set()
    for obj in iter_records(path):
        if obj.get("type") != "user":
            continue
        origin = obj.get("origin")
        if isinstance(origin, dict):
            kind = origin.get("kind")
            if kind and kind not in KNOWN_ORIGIN_KINDS:
                kinds.add(kind)
    return kinds


def origin_stats(path: str | Path) -> dict:
    """Counts that make a SCHEMA CHANGE distinguishable from a quiet week.

    `origin.kind` is undocumented, appeared at 2.1.187, and is the single field
    the entire capture layer depends on. If a future build renames it, drops it,
    or moves the projects directory, `human_turns` returns [] for every file --
    and every stage downstream then completes successfully with nothing in it.
    Harvest beats its heartbeat, health reports OK, and MEMORY.md says capture is
    fine, permanently.

    `unknown_origin_kinds` cannot catch that: it only fires when `origin` is
    still a dict carrying an unrecognised value. It sees nothing when the field
    vanishes. So the harvester also reports how many `type == "user"` records it
    LOOKED at against how many turns it KEPT, and health.write alarms when the
    first is large and the second is zero.
    """
    stats = {"user_records": 0, "human_turns": 0, "origin_absent": 0,
             "unknown_kinds": set()}
    for obj in iter_records(path):
        if obj.get("type") != "user":
            continue
        stats["user_records"] += 1
        origin = obj.get("origin")
        if not isinstance(origin, dict):
            stats["origin_absent"] += 1
        else:
            kind = origin.get("kind")
            if kind is None:
                stats["origin_absent"] += 1
            elif kind not in KNOWN_ORIGIN_KINDS:
                stats["unknown_kinds"].add(kind)
        if is_human_turn(obj):
            stats["human_turns"] += 1
    return stats


def adjacent_context(path: str | Path, limit: int = 1200) -> dict[str, tuple[str, str]]:
    """human uuid -> (assistant text before, assistant text after), truncated.

    Snapshotted alongside every retained turn so a quote stays interpretable
    after the transcript is deleted, and so a future confirmed-tier pass has
    something to read. Never itself a source of facts in this plan.
    """
    records = [r for r in iter_records(path) if not r.get("isSidechain")]
    result: dict[str, tuple[str, str]] = {}
    for i, obj in enumerate(records):
        if not is_human_turn(obj):
            continue
        before = after = ""
        for j in range(i - 1, -1, -1):
            if records[j].get("type") == "assistant":
                before = clean(message_text(records[j]))[:limit]
                break
        for j in range(i + 1, len(records)):
            if records[j].get("type") == "assistant":
                after = clean(message_text(records[j]))[:limit]
                break
        result[obj.get("uuid") or ""] = (before, after)
    return result
