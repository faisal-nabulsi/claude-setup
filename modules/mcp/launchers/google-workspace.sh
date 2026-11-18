#!/bin/sh
# Starts the google-workspace MCP (https://github.com/aaronsb/google-workspace-mcp) for all your
# Google accounts, with its OAuth client id/secret from the macOS Keychain instead of ~/.claude.json.
# Pinned version: bump it deliberately after reading the upstream changelog.
ID="$(/usr/bin/security find-generic-password -a "$USER" -s mcp:google-client-id -w 2>/dev/null)" &&
SECRET="$(/usr/bin/security find-generic-password -a "$USER" -s mcp:google-client-secret -w 2>/dev/null)" || {
  echo "google-workspace: Keychain items missing; run ~/.claude/mcp-wrappers/setup-secrets.sh" >&2; exit 1; }
export GOOGLE_CLIENT_ID="$ID" GOOGLE_CLIENT_SECRET="$SECRET"; unset ID SECRET
exec npx -y @aaronsb/google-workspace-mcp@4.5.1 "$@"
