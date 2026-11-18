#!/usr/bin/env python3
"""PreToolUse guard (Bash|Read): keep Claude from reading secrets that the MCP
launchers in ~/.claude/mcp-wrappers pull from the macOS Keychain.

Refuses: `security find-*-password -w/-g`, `security dump-keychain|export|-i`,
Keychain API calls from scripts (SecItemCopyMatching, keyring.get_password),
printing other processes' environments (`ps e…` / `ps -E`), running or sourcing
the launchers by hand (Claude Code starts them; the user runs setup-secrets.sh),
and Read on ~/Library/Keychains. Reading the launcher scripts themselves is fine:
they hold no secrets.

This raises the bar against a prompt-injected command; it is not a sandbox. A
determined process running as the user can still reach the Keychain.
"""
import json, os, re, sys

HOME = os.path.expanduser("~")
REASON = ("Blocked by secrets guard: this would expose a secret from the macOS Keychain or a running "
          "MCP server's environment. MCP tokens stay in Keychain and are read only by the launchers in "
          "~/.claude/mcp-wrappers when Claude Code starts them. If the user needs to change a secret, they run "
          "~/.claude/mcp-wrappers/setup-secrets.sh themselves.")

RULES = [
    r"\bsecurity\b[^\n;|&]*\bfind-(generic|internet)-password\b[^\n;|&]*\s-[a-zA-Z]*[wg][a-zA-Z]*\b",
    r"\bsecurity\b[^\n;|&]*\b(dump-keychain|export)\b",
    r"\bsecurity\s+-i\b",
    r"SecItemCopyMatching|kSecReturnData|SecKeychainFindGenericPassword",
    r"\bkeyring\.get_password\b|\bkeyring\s+get\b",
    r"\bps\b[^\n;|&]*(\s-[a-zA-Z]*E|\s[auxwjhlmrcSTvZAU]*e[auxwjhlmrcSTvZAU]*\b)",
]
SAFE_WRAPPER_USE = re.compile(r"^\s*(ls|cat|head|tail|wc|grep|sed\s+-n|bash\s+-n|sh\s+-n|chmod|stat)\b")


def deny() -> None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                             "permissionDecision": "deny",
                                             "permissionDecisionReason": REASON}}))
    sys.exit(0)


def main(raw: str) -> None:
    data = json.loads(raw or "{}")
    tool, ti = data.get("tool_name"), data.get("tool_input") or {}
    if tool == "Read":
        p = os.path.realpath(os.path.expanduser(ti.get("file_path", "")))
        if p.startswith(HOME + "/Library/Keychains"):
            deny()
        return
    if tool != "Bash":
        return
    cmd = ti.get("command", "")
    if any(re.search(r, cmd) for r in RULES):
        deny()
    if "mcp-wrappers" in cmd:
        for seg in re.split(r"&&|\|\||;|\n", cmd):
            if "mcp-wrappers" in seg and not SAFE_WRAPPER_USE.match(seg.split("|")[0]):
                deny()


if __name__ == "__main__":
    RAW = sys.stdin.read()
    try:
        main(RAW)
    except SystemExit:
        raise
    except Exception as e:  # fail closed when the call looks secret-related
        print(f"secrets guard error: {e!r}", file=sys.stderr)
        if re.search(r"security|keychain|mcp-wrappers", RAW, re.I):
            deny()
