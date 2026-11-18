#!/usr/bin/env python3
"""PreToolUse hook: connectors and APIs before the browser, the screen or AppleScript.

Why: a connector (an MCP server or an API) is faster, returns clean data, and uses far
less context than reading web pages or clicking around the screen. CLAUDE.md states the
rule ("Tool order"); this hook enforces it.

The FIRST call in a session to one of these kinds of "control" is denied, with a
reason that names the connector to use instead:
  browser   mcp__claude-in-chrome__*, mcp__Claude_Browser__* (not preview_*),
            mcp__remote-devices__Claude_Browser__*
  screen    mcp__computer-use__*, mcp__remote-devices__computer_*
  apps      Bash running osascript against Mail, Messages, Calendar, Chrome or Safari
If no connector covers the job, Claude simply retries: every later call of that
kind in the same session goes through. So a real fallback costs one bounce, and
skipping the connector by habit stops. Any error here allows the call (exit 0).
"""
import json, re, sys, time
from pathlib import Path

STATE = Path.home() / ".claude/state/connectors-first"
APPS = re.compile(r'osascript[\s\S]*tell\s+application\s+"?(Mail|Messages|Calendar|Google Chrome|Safari)\b', re.I)

# Your own "job -> connector" map. Edit ~/.claude/connectors-first.txt (one line per job,
# e.g. "school LMS -> canvas MCP"); the installer writes a starter copy.
MAP_FILE = Path.home() / ".claude/connectors-first.txt"
DEFAULT_MAP = ("school LMS -> canvas MCP; mail/Drive/Docs/calendar -> google-workspace MCP or the claude.ai "
               "Google connectors; Slack -> Slack connector; Gradescope/PollEv/Ed -> coursework MCP; "
               "if those MCP tools aren't loaded (e.g. a scheduled run) -> "
               "python3 ~/code/scripts/mcp_bridge.py <server> call <tool> '<json>'")


def _map() -> str:
    try:
        lines = [l.strip() for l in MAP_FILE.read_text().splitlines() if l.strip() and not l.startswith("#")]
        return "; ".join(lines) or DEFAULT_MAP
    except OSError:
        return DEFAULT_MAP


REASON = (
    "Connectors first (standing rule; ~/.claude/CLAUDE.md 'Tool order'). Before {what}, "
    "use the connector that covers the job if one does: {map}. "
    "If none covers it (a site with no API or connector), retry the same call: it will go through. "
    "Then tell the user in one line why {short} was needed."
)
WHAT = {"browser": ("using the browser", "the browser"),
        "screen": ("controlling the screen (computer use)", "screen control"),
        "apps": ("driving an app with AppleScript", "AppleScript")}


def kind(tool: str, tin: dict) -> str | None:
    if re.match(r"mcp__(claude-in-chrome|Claude_Browser|remote-devices__Claude_Browser)__", tool):
        return None if "preview_" in tool else "browser"  # local dev-server previews aren't "control"
    if re.match(r"mcp__(computer-use__|remote-devices__computer)", tool):
        return "screen"
    if tool == "Bash" and APPS.search(str(tin.get("command", ""))):
        return "apps"
    return None


def main() -> None:
    data = json.loads(sys.stdin.read() or "{}")
    k = kind(data.get("tool_name", ""), data.get("tool_input") or {})
    sid = re.sub(r"[^A-Za-z0-9_-]", "", str(data.get("session_id", "")))[:80]
    if not k or not sid:
        return
    STATE.mkdir(parents=True, exist_ok=True)
    flag = STATE / f"{sid}.{k}"
    if flag.exists():
        return  # already reminded in this session: the retry goes through
    flag.write_text(time.strftime("%Y-%m-%dT%H:%M"))
    for old in STATE.glob("*"):  # keep the folder small
        if time.time() - old.stat().st_mtime > 7 * 86400:
            old.unlink(missing_ok=True)
    what, short = WHAT[k]
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": REASON.format(what=what, short=short, map=_map())}}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # never block a tool because this hook broke
