"""Read Gmail through the user's Google connector, from a plain script.

The connector is the `google-workspace` MCP server (aaronsb/google-workspace-mcp)
that Claude Code launches from ~/.claude.json. Both mail accounts are already
authorised on it READ-ONLY, so a nightly job can reuse it: start the same
server the same way (command, args and env straight from that config), speak
just enough MCP JSON-RPC over stdio to call one tool, and stop it again. No
OAuth code, no token handling here -- the server's launcher pulls its secrets
from the Keychain, and nothing in this module ever reads or prints them.

**Read-only twice over.** The accounts are authorised with read scopes, and
this module refuses to send any operation but `search` and `read` (`_ALLOWED`),
so a bug here cannot send, label, archive, trash or mark mail.

The server answers in markdown meant for an agent. `parse_search` and
`parse_read` turn that back into data, and they are written against the
server's own formatter (build/server/formatting/markdown.js, v4.5.1):

    search ->  ## Messages (N)\\n\\n<id> | <from> | <subject> | <short date>
    read   ->  ## <subject>\\n\\n**From:** ..\\n**To:** ..\\n**Date:** ..
               [**Labels:** ..][\\n\\n**Attachments (n):**\\n1. ..]\\n\\n<body>

and every response may end with a `---\\n**Next steps:**` footer, stripped here.
Search rows truncate sender and subject, so only the id is taken from them;
`read` supplies the full headers.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

CONFIG = Path.home() / ".claude.json"
SERVER = "google-workspace"
_ALLOWED = frozenset({"search", "read"})

_FOOTER = "\n\n---\n**Next steps:**"
_ROW = re.compile(r"^([0-9a-f]{8,})\s\|\s?(.*)$")
_HEADER = re.compile(r"^\*\*(From|To|Date|Labels):\*\*\s?(.*)$")


class ConnectorError(RuntimeError):
    """The connector could not be started, or answered with an error."""


def _strip_footer(text: str) -> str:
    head, sep, _ = text.rpartition(_FOOTER)
    return head if sep else text


def parse_search(text: str) -> tuple[list[str], list[str]]:
    """Message ids from a `search` answer, newest first, and the ids of rows
    the server failed to fetch (rendered `<id> | ⚠ <error>`).

    "No messages found" is an empty result. Anything else without rows -- the
    server's own "response was missing expected fields" warning included -- is
    an error, never a silent zero."""
    text = _strip_footer(text).strip()
    if text.startswith("No messages found"):
        return [], []
    ids, failed = [], []
    for line in text.split("\n"):
        m = _ROW.match(line.strip())
        if not m:
            continue
        (failed if m.group(2).startswith("⚠") else ids).append(m.group(1))
    if not ids and not failed:
        raise ConnectorError(f"unexpected search response: {text.splitlines()[0][:160] if text else '(empty)'}")
    return ids, failed


def parse_read(text: str) -> dict:
    """subject / sender / to / date / labels / body from a `read` answer.

    `date` is the message's Date header as an aware UTC datetime, or None when
    the header is missing or unparseable."""
    text = _strip_footer(text)
    title, _, rest = text.partition("\n\n")
    subject = title[3:] if title.startswith("## ") else title
    if subject == "(no subject)":
        subject = ""
    block, _, body = rest.partition("\n\n")
    fields = {}
    for line in block.split("\n"):
        m = _HEADER.match(line)
        if m:
            fields[m.group(1).lower()] = m.group(2).strip()
    if body.startswith("**Attachments ("):
        _, _, body = body.partition("\n\n")
    return {"subject": subject.strip(), "sender": fields.get("from", ""),
            "to": fields.get("to", ""), "date": _parse_date(fields.get("date", "")),
            "labels": [l.strip() for l in fields.get("labels", "").split(",") if l.strip()],
            "body": body}


def _parse_date(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:  # RFC 2822 "-0000": UTC with no claim about local time
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc)


class GoogleConnector:
    """One running connector server; reuse it for every call in a run.

    Use as a context manager so the server is always stopped."""

    def __init__(self, config: Path = CONFIG, timeout: float = 90.0):
        try:
            spec = json.loads(config.read_text(encoding="utf-8"))["mcpServers"][SERVER]
        except (OSError, ValueError, KeyError) as exc:
            raise ConnectorError(f"no `{SERVER}` server in {config} ({type(exc).__name__})") from None
        env = dict(os.environ)
        env.update(spec.get("env") or {})
        try:
            self._proc = subprocess.Popen(
                [spec["command"], *(spec.get("args") or [])],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=env, text=True, bufsize=1)
        except OSError as exc:
            raise ConnectorError(f"cannot start the connector: {exc.strerror}") from None
        self._timeout = timeout
        self._next_id = 0
        self._replies: dict[int, dict] = {}
        self._cv = threading.Condition()
        threading.Thread(target=self._read_stdout, daemon=True).start()
        # The server logs to stderr; drain it so it can never block, and never
        # echo it -- launcher output is not something to copy into a log file.
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        try:
            self._request("initialize", {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "lifevault-mail", "version": "1.0"}}, timeout=180)
            self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except Exception:
            self.close()
            raise

    def __enter__(self) -> GoogleConnector:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def alive(self) -> bool:
        return self._proc.poll() is None

    def close(self) -> None:
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()

    def _read_stdout(self) -> None:
        for line in self._proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if "id" in msg and ("result" in msg or "error" in msg):
                with self._cv:
                    self._replies[msg["id"]] = msg
                    self._cv.notify_all()
        with self._cv:  # server exited: wake any waiter so it fails fast
            self._cv.notify_all()

    def _drain_stderr(self) -> None:
        for _ in self._proc.stderr:
            pass

    def _send(self, msg: dict) -> None:
        try:
            self._proc.stdin.write(json.dumps(msg) + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            raise ConnectorError("the connector exited") from None

    def _request(self, method: str, params: dict, timeout: float | None = None) -> dict:
        self._next_id += 1
        rid = self._next_id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        deadline = time.monotonic() + (timeout or self._timeout)
        with self._cv:
            while rid not in self._replies:
                if self._proc.poll() is not None:
                    raise ConnectorError(f"the connector exited (code {self._proc.returncode}) during {method}")
                left = deadline - time.monotonic()
                if left <= 0:
                    raise ConnectorError(f"{method} timed out after {timeout or self._timeout:.0f}s")
                self._cv.wait(min(left, 1.0))
            reply = self._replies.pop(rid)
        if "error" in reply:
            raise ConnectorError(str(reply["error"].get("message", "error"))[:200])
        return reply["result"]

    def email(self, **arguments) -> str:
        """Call `manage_email`; only the read-only operations are allowed."""
        if arguments.get("operation") not in _ALLOWED:
            raise ValueError(f"refusing manage_email operation {arguments.get('operation')!r}: "
                             f"only {sorted(_ALLOWED)} are allowed")
        result = self._request("tools/call", {"name": "manage_email", "arguments": arguments})
        text = "".join(part.get("text", "") for part in result.get("content", []))
        if result.get("isError"):
            # Errors arrive as multi-line JSON or prose; flatten to one log line.
            raise ConnectorError(" ".join(_strip_footer(text).split())[:240] or "tool error")
        return text

    def search(self, account: str, query: str, max_results: int = 50) -> tuple[list[str], list[str]]:
        return parse_search(self.email(operation="search", email=account,
                                       query=query, maxResults=max_results))

    def read(self, account: str, message_id: str) -> dict:
        return parse_read(self.email(operation="read", email=account, messageId=message_id))
