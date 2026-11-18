#!/bin/bash
# claude-setup installer. Safe to re-run: it never overwrites an existing file, backs up
# ~/.claude/settings.json before merging into it, and asks before each module.
#
#   ./install.sh --dry-run          show what would happen, change nothing
#   ./install.sh                    interactive install
#   ./install.sh --modules core,hooks,life-vault --yes   non-interactive (values from env, see below)
#
# Non-interactive values (env): CS_NAME, CS_EMAILS, CS_LABEL_PREFIX, CS_TIMEZONE, CS_CANVAS_HOST
set -euo pipefail
command -v python3 >/dev/null || { echo "python3 is required (macOS: xcode-select --install)"; exit 1; }
exec python3 "$(cd "$(dirname "$0")" && pwd)/scripts/installer.py" "$@"
