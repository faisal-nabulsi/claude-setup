#!/bin/sh
# Smoke-test the deployed /todo glance. Prints status codes and the first 3 lines
# of the authorized reply, nothing else.
# The key is read from the macOS keychain item "heartbeat-glance-key" and handed
# to curl on stdin (-H @-), so it never shows up in output, in `ps`, or in a file.
#
#   sh scripts/check.sh https://heartbeat-kicker.<subdomain>.workers.dev
set -eu
BASE="${1:?usage: sh scripts/check.sh https://heartbeat-kicker.<subdomain>.workers.dev}"
BASE="${BASE%/}"
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }

echo "GET /todo, no key     -> $(code "$BASE/todo")        (want 401)"
echo "GET /todo, wrong key  -> $(printf 'Authorization: Bearer wrong\n' | code -H @- "$BASE/todo")        (want 401)"
echo "POST /todo            -> $(code -X POST "$BASE/todo")        (want 404)"
echo "GET /                 -> $(code "$BASE/")        (want 404)"

BODY="$(mktemp)"
trap 'rm -f "$BODY"' EXIT
KEY="$(security find-generic-password -a "$USER" -s heartbeat-glance-key -w)"
STATUS="$(printf 'Authorization: Bearer %s\n' "$KEY" | curl -s -o "$BODY" -w '%{http_code}' -H @- "$BASE/todo")"
unset KEY
echo "GET /todo, keychain key -> $STATUS      (want 200)"
head -n 3 "$BODY"
