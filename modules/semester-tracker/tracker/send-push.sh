#!/bin/bash
# Sends one push notification to YOUR phone. Usage: ./send-push.sh "message text"
# Channel 1: ntfy (topic in .ntfy.env next to this file; subscribe to it in the ntfy phone app).
# Channel 2 (optional, macOS): iMessage to yourself through Messages.app, if IMESSAGE_TO is set in .ntfy.env.
# SEND_DRY_RUN=1 prints the payload instead of sending (used by checks and tests).
# Exits 0 even if nothing could be sent, so it is safe to call from any script.
#
# .ntfy.env (chmod 600, never commit it):
#   NTFY_TOPIC=a-long-random-topic-name
#   NTFY_SERVER=https://ntfy.sh          # optional; your own server if you self-host
#   NTFY_TOKEN=tk_...                    # optional; only for protected topics
#   NTFY_TITLE=School                    # optional
#   IMESSAGE_TO=+15555550100             # optional; your own number or Apple ID email

DIR="$(cd "$(dirname "$0")" && pwd)"
MSG="$1"
[ -n "$MSG" ] || { echo "usage: send-push.sh \"message\""; exit 1; }

if [ "$SEND_DRY_RUN" = "1" ]; then
  printf 'DRY-RUN payload: %s\n' "$MSG"
  exit 0
fi

[ -f "$DIR/.ntfy.env" ] && . "$DIR/.ntfy.env"
SERVER="${NTFY_SERVER:-https://ntfy.sh}"

if [ -n "$NTFY_TOPIC" ]; then
  AUTH=()
  [ -n "$NTFY_TOKEN" ] && AUTH=(-H "Authorization: Bearer $NTFY_TOKEN")
  HTTP=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "${AUTH[@]}" \
    -H "Title: ${NTFY_TITLE:-Tracker}" --data-binary "$MSG" "$SERVER/$NTFY_TOPIC")
  if [ "$HTTP" = "200" ]; then
    echo "ntfy push sent"
    exit 0
  fi
  echo "ntfy failed (http $HTTP)"
fi

if [ -n "$IMESSAGE_TO" ] && command -v osascript >/dev/null; then
  # Escape backslashes, then double quotes, so message text cannot break out of the AppleScript string.
  ESCAPED=$(printf '%s' "$MSG" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')
  osascript <<EOF 2>&1 && echo "iMessage sent"
tell application "Messages"
    set theService to 1st account whose service type = iMessage
    send "$ESCAPED" to participant "$IMESSAGE_TO" of theService
end tell
EOF
fi
exit 0
