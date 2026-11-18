# Hooks

A **hook** is a small script Claude Code runs automatically at a fixed moment: when a session
starts (`SessionStart`), before a tool runs (`PreToolUse`), after it runs (`PostToolUse`), or
when Claude finishes a reply (`Stop`). Hooks are wired up in `~/.claude/settings.json`.

Why hooks: a rule written in CLAUDE.md is a request; a hook is enforcement. These ones turn the
most important rules into guarantees.

| Hook | When | What it does | Module |
|---|---|---|---|
| `today.py` | SessionStart | Prints your next deadlines into every session (full list in personal folders, a 3-line summary elsewhere), so Claude starts already knowing what's due. Also feeds the `/todo` panel and the status band. Reads `~/semester-tracker` (or `$SEMESTER_TRACKER`). | semester-tracker |
| `connectors_first.py` | PreToolUse on browser / screen / AppleScript tools | The FIRST browser, screen-control or AppleScript call in a session is refused with a reminder naming the connector to use instead (from `~/.claude/connectors-first.txt`). Retrying goes through, so a real fallback costs one bounce. | hooks |
| `secrets_guard.py` | PreToolUse on Bash and Read | Refuses commands that would print a Keychain secret (`security find-*-password -w`, `dump-keychain`), dump another process's environment, or run the MCP launchers by hand. Raises the bar against a prompt-injected command; it is not a sandbox. | hooks |
| `deadlines_guard.py` | Pre + PostToolUse on Bash/Write/Edit | Protects `deadlines.json`: blocks `cp`/`mv`/redirects onto it, refuses edits that drop a row or un-finish a done row, and if a script did it anyway, puts the rows back. | semester-tracker |
| `icloud_guard.py` | SessionStart | macOS only. Warns when an active git repo lives in iCloud-synced Desktop/Documents (iCloud can evict files and corrupt `.git`). Fix it suggests: `xattr -w 'com.apple.fileprovider.ignore#P' 1 <folder>`. | optional |
| `lv-capture` | Stop | Life vault capture pointer (see `modules/life-vault`). | life-vault |

## Install

`install.sh` copies the scripts to `~/.claude/hooks/` and **merges** the matching blocks from
`modules/core/settings-hooks.json` into `~/.claude/settings.json`. It backs up settings.json
first and never overwrites it. To do it by hand, copy the blocks you want from that snippet
into the `"hooks"` object of your settings.json, replacing `{{HOME}}` with your home folder.

All hooks fail open (an error lets the tool call through) except where noted: the secrets and
deadlines guards fail closed only when the call looks secret- or deadline-related.
