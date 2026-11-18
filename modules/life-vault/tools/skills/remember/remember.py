#!/usr/bin/env python3
# tools/skills/remember/remember.py
"""The mechanism behind `/remember`. Synchronous, conf/stated, their own words.

This ships beside SKILL.md rather than in `tools/bin/` because the two halves
are one artifact: SKILL.md is the prompt, this is everything that must not
depend on a model's judgement. Splitting them across the repo lets one be
edited without the other.

Three rules live here, and none of them is stated only in the prompt:

1. **`conf/stated` is not an input.** There is no flag and no payload key for
   it. This path and a review line the user rewrote themselves are the only two
   sources of the top trust tier; anything a model can set is not that.

2. **The credential gate runs before anything is written.** `scan_hard` on the
   whole text, and a hit means refuse. The refusal reports kinds and offsets
   and never the value, because a log of the things too dangerous to file must
   not become the leak.

3. **Sensitivity is computed, not asked for.** `mask_soft` is run for its HITS
   rather than its output — an SSN or a Luhn-valid card number makes a record
   `sens/private` whatever area it was filed under. The masked text is
   discarded on purpose: these are their own words and `/remember` is explicit,
   so the body and the quote stay verbatim. Only the flag changes.

Nothing the user typed ever reaches a shell. The payload arrives as a JSON
file whose path is the single command-line argument, and the one write goes
through `bin/lv add` invoked as an argv list — no shell, no quoting, nothing
to escape.

Exit codes follow `lv`: 0 filed, 1 input rejected and nothing written,
2 something went wrong mid-operation.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date as _date
from pathlib import Path

#: Areas whose subject matter is sensitive by definition (spec §10 and the
#: `sens/private` row of the vocabulary: health, finance, legal, relationship
#: conflict). Relationship conflict has no area of its own — SENSITIVE_CUES
#: carries divorce/custody/abuse/affair — so `area/social` is deliberately not
#: here: flagging every friendship as private would make the flag mean nothing.
SENSITIVE_AREAS = frozenset({"area/health", "area/finance", "area/legal"})

#: Payload keys this accepts. Anything else is a rejection rather than a
#: silent ignore: `conf` and `tags` are the two a caller would reach for to
#: set its own confidence tier, and a typo'd `subjcet` would otherwise file
#: under the wrong supersession key forever.
ALLOWED_KEYS = frozenset({"text", "entity", "kind", "area", "type", "subject",
                          "date", "title", "private"})
REQUIRED_KEYS = ("text", "entity", "kind", "area", "type", "subject")

DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


class Rejected(Exception):
    """The caller's input was refused. Nothing was written; exit 1."""


def tools_dir() -> Path:
    """Locate the vault's `tools/` directory, wherever this file is running from.

    In the repo this file sits at `<vault>/tools/skills/remember/`, so
    `parents[2]` is the answer. A copy placed somewhere else — an installed
    skill directory, say — falls back to `$LIFE_VAULT/tools`. Checking for the
    package rather than assuming a layout means a wrong guess fails here, with
    a sentence, instead of as an ImportError three frames deep.
    """
    here = Path(__file__).resolve()
    root = Path(os.environ.get("LIFE_VAULT") or (Path.home() / "life"))
    for candidate in (here.parents[2], root / "tools", Path.home() / "life" / "tools"):
        if (candidate / "lifevault" / "__init__.py").exists():
            return candidate
    raise Rejected(
        f"cannot find the lifevault package from {here}; "
        f"set LIFE_VAULT to the vault root")


try:
    TOOLS = tools_dir()
except Rejected as _exc:  # a misplaced copy is bad input, not a crash
    print(str(_exc), file=sys.stderr)
    raise SystemExit(1) from None
sys.path.insert(0, str(TOOLS))

from lifevault.entry import MAX_QUOTE                               # noqa: E402
from lifevault.index import SEARCH_DIRS                             # noqa: E402
from lifevault.sources.redact import (                              # noqa: E402
    describe, is_sensitive, mask_soft, scan_hard)
from lifevault.tags import load_vocab                               # noqa: E402


def vault_root() -> Path:
    return Path(os.environ.get("LIFE_VAULT") or (Path.home() / "life"))


def read_payload(path: Path) -> dict:
    """Parse the inbox file, then delete it.

    The delete is unconditional and happens before any validation, because the
    file holds a verbatim copy of whatever was typed — which on the one path
    this refuses is a live credential. `_queue/` is gitignored, so it would not
    reach a commit, but a plaintext key sitting in a predictable filename is
    not something to leave behind either.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise Rejected(f"cannot read the input file {path}: {exc}") from None
    finally:
        try:
            path.unlink()
        except OSError:
            pass

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise Rejected(
            f"the input file is not valid JSON (line {exc.lineno}, column "
            f"{exc.colno}: {exc.msg}). Nothing was written.") from None
    if not isinstance(payload, dict):
        raise Rejected("the input file must hold a JSON object")

    unknown = sorted(set(payload) - ALLOWED_KEYS)
    if unknown:
        raise Rejected(
            f"unrecognised field(s): {', '.join(unknown)}. Accepted fields are "
            f"{', '.join(sorted(ALLOWED_KEYS))}. Note that there is no `conf` "
            f"and no `tags` field: /remember always files at conf/stated, and "
            f"the sensitivity tag is computed, not chosen.")

    missing = [k for k in REQUIRED_KEYS
               if not isinstance(payload.get(k), str) or not payload[k].strip()]
    if missing:
        raise Rejected(f"missing or empty required field(s): {', '.join(missing)}")
    return payload


def check_date(value: str) -> str:
    if not DATE_RE.match(value):
        raise Rejected(f"date must be YYYY-MM-DD, not {value!r}")
    try:
        _date.fromisoformat(value)
    except ValueError:
        raise Rejected(f"date {value!r} is not a real calendar date") from None
    return value


def check_tags(vault: Path, area: str, type_: str) -> None:
    """Validate against the closed vocabulary here, so the message is useful.

    `lv add` rejects the same tags, but its message names the tag without
    offering the alternatives — and the caller is a model that will otherwise
    invent a second wrong one.
    """
    try:
        vocab = load_vocab(vault / "_index" / "tags.tsv")
    except (OSError, ValueError) as exc:
        raise Rejected(str(exc)) from None
    for label, tag in (("area", area), ("type", type_)):
        if tag not in vocab or not tag.startswith(f"{label}/"):
            options = " ".join(sorted(t for t in vocab if t.startswith(f"{label}/")))
            raise Rejected(f"unknown {label} tag: {tag}\nchoose one of: {options}")


def gate(text: str) -> None:
    """Refuse the whole record if it carries a credential.

    Not masked and filed: a redacted secret still proves the secret existed,
    records when it was said, and usually identifies which one it was.
    """
    secrets = scan_hard(text)
    if secrets:
        raise Rejected(
            f"refused: this looks like it contains a secret — {describe(secrets)}.\n"
            f"Nothing was written and the text was not kept. The value itself is "
            f"deliberately not repeated here.\n"
            f"If that credential is live, rotate it. If you meant to file the "
            f"surrounding fact, re-run /remember with the credential removed.")


def sensitivity(text: str, area: str, private: bool) -> str:
    """`sens/private` or `sens/normal`, decided mechanically.

    `mask_soft` is consulted for its hit flag only. Without it an SSN under
    `area/admin` — no cue word, not a sensitive area — filed at conf/stated
    carrying `sens/normal`, a tag whose own vocabulary row reads "Explicitly
    not sensitive; safe to include in outbound content".

    `private` can only ever add the flag. There is no way to clear it, because
    declassifying is a decision made by a human in the file, not by whatever
    assembled this payload.
    """
    _masked, soft_hit = mask_soft(text)
    if private or soft_hit or area in SENSITIVE_AREAS or is_sensitive(text):
        return "sens/private"
    return "sens/normal"


def file_it(vault: Path, payload: dict) -> tuple[str, str, list[str], str]:
    text = payload["text"].strip()
    entity = payload["entity"].strip()
    kind = payload["kind"].strip()
    area = payload["area"].strip()
    type_ = payload["type"].strip()
    subject = payload["subject"].strip()

    # First, before every other check. A payload that is wrong in two ways at
    # once must not have the credential in it go unmentioned because the date
    # was also malformed — and no branch below may be reachable with an
    # unscanned text.
    gate(text)

    when = check_date(str(payload.get("date") or _date.today().isoformat()))

    if kind not in SEARCH_DIRS:
        # Validated here rather than by argparse's `choices`, because argparse
        # exits 2 on a bad choice and 2 means "internal error" to every caller
        # of this family of tools. A bad kind is rejected input: exit 1.
        raise Rejected(
            f"unknown kind: {kind}\nchoose one of: {' '.join(SEARCH_DIRS)}")

    check_tags(vault, area, type_)

    tags = [area, type_, "conf/stated",
            sensitivity(text, area, bool(payload.get("private")))]

    title = str(payload.get("title") or "").strip() or re.sub(r"\s+", " ", text)[:80]
    # The text IS the source, so the quote anchors by construction and
    # conf/stated is earned rather than asserted. Spec §12.1 wants a
    # distinctive span, not a copy, so a very long remember keeps the whole
    # text as the body and the leading span as the anchor.
    quote = text if len(text) <= MAX_QUOTE else text[:MAX_QUOTE]

    lv = TOOLS / "bin" / "lv"
    # An argv list, never a string: this is the last hop and the one place a
    # shell could still creep in. `text` reaches `lv` as one element of a list
    # and is never parsed by anything.
    result = subprocess.run(
        [sys.executable, str(lv), "add",
         "--entity", entity, "--kind", kind, "--date", when,
         "--title", title, "--subject", subject, "--tags", " ".join(tags),
         "--source", f"remember/{when}", "--body", text, "--quote", quote],
        capture_output=True, text=True,
        env={**os.environ, "LIFE_VAULT": str(vault)})

    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        # Pass the code through unchanged. 1 still means "your input was
        # rejected and nothing was written"; 2 still means a retry may work.
        raise SystemExit(result.returncode)

    verdict, _, posting = result.stdout.strip().partition("\t")
    return verdict, posting, tags, text


def reindex(vault: Path) -> str | None:
    """Rebuild `_index/tags/`, and do not fail the filing if it cannot.

    `lv add` writes the log; only this makes the fact visible to `lv find` and
    to the lookup procedure MEMORY.md tells Claude to follow. A fact filed at
    the top trust tier that nothing can retrieve is the failure this exists to
    prevent — but the fact IS on disk by now, so an index problem is a warning,
    not an exit code.
    """
    from lifevault.index import rebuild
    try:
        rebuild(vault)
    except Exception as exc:  # noqa: BLE001 - the fact is already filed
        return f"{type(exc).__name__}: {exc}"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="remember.py",
        description="File one thing into the life vault at conf/stated.")
    parser.add_argument(
        "--input", required=True,
        help="path to a JSON payload; it is read once and then deleted")
    args = parser.parse_args()

    vault = vault_root()
    try:
        payload = read_payload(Path(args.input))
        verdict, posting, tags, text = file_it(vault, payload)
    except Rejected as exc:
        print(str(exc), file=sys.stderr)
        return 1

    # The verdict word is always printed, including in the friendly branch:
    # it is the vocabulary `store.append_entry` speaks, so it is what a person
    # reading this output can grep the code for.
    lead = ("already filed, nothing changed (duplicate)"
            if verdict == "duplicate" else f"filed ({verdict})")
    print(f"{lead}: {posting}")
    print(f"  subject: {payload['subject'].strip()}")
    print(f"  tags:    {' '.join(tags)}")
    print(f"  body:    {text}")
    if verdict == "conflict":
        print("  note:    this contradicts a fact already filed under the same "
              "subject on the same day. Both are marked `conflicted` and the "
              "pair is in _review/conflicts.md.")

    problem = reindex(vault)
    if problem:
        print(f"  warning: filed, but the tag index could not be rebuilt "
              f"({problem}). Run `lv reindex`.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Rejected as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
    except (ValueError, OSError) as exc:
        print(f"remember: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2)
