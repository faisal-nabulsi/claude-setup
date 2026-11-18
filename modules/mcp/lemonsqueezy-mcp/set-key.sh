#!/bin/sh
# The user runs this themselves to store their Lemon Squeezy API key in .env (chmod 600).
# Get a key at https://app.lemonsqueezy.com/settings/api ("+" to create one, name it e.g. "claude-mcp").
# The key is read without echo and never printed.
HERE="$(cd "$(dirname "$0")" && pwd)"
printf "Paste your Lemon Squeezy API key (input hidden), then press Return: "
stty -echo 2>/dev/null
IFS= read -r KEY
stty echo 2>/dev/null
echo
if [ -z "$KEY" ]; then echo "No key entered; nothing changed."; exit 1; fi
umask 077
printf 'LEMONSQUEEZY_API_KEY=%s\n' "$KEY" > "$HERE/.env"
chmod 600 "$HERE/.env"
unset KEY
echo "Saved to $HERE/.env (only you can read it). Restart Claude Code sessions, or use mcp_bridge.py, to pick it up."
