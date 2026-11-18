#!/bin/sh
# Starts canvas-mcp (https://github.com/vishalsachdev/canvas-mcp), read-only, with its token
# from the macOS Keychain item "mcp:canvas", so the token is never written into ~/.claude.json.
# Setup / rotation: ~/.claude/mcp-wrappers/setup-secrets.sh  (you run it, not Claude)
TOKEN="$(/usr/bin/security find-generic-password -a "$USER" -s mcp:canvas -w 2>/dev/null)" || {
  echo "canvas-mcp: Keychain item mcp:canvas missing; run ~/.claude/mcp-wrappers/setup-secrets.sh" >&2; exit 1; }
export CANVAS_API_TOKEN="$TOKEN"; unset TOKEN
export CANVAS_API_URL="https://{{CANVAS_HOST}}/api/v1"
export CANVAS_ROLE=student            # student tool profile only
# Only side-effect tool allowed: save a course file to a folder on this Mac.
# It never changes Canvas (and refuses to overwrite an existing local file).
export ALLOWED_WRITE_TOOLS=download_course_file
export STUDENT_WRITE_TOOLS=           # no submit/message/planner writes
export TIMEZONE="{{TIMEZONE}}"
export ENABLE_DATA_ANONYMIZATION=true LOG_REDACT_PII=true ACCESSIBILITY_CHECKERS=none
cd "$HOME/.local/share/mcp/canvas" || exit 1   # no stray .env from the session folder
exec "$HOME/.local/share/mcp/canvas/bin/canvas-mcp-server" "$@"
