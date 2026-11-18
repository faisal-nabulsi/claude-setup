#!/bin/bash
# One-time (and rotation) setup for the Keychain-backed MCP launchers.
# RUN IT YOURSELF in a terminal. Claude never sees the values: they go straight into the
# macOS Keychain through `security`'s own prompt, never through a command line or a file.
#   1. Google OAuth client id + secret  -> Keychain items mcp:google-client-id / mcp:google-client-secret
#   2. Canvas access token               -> Keychain item mcp:canvas
#   3. Registers both servers with Claude Code to start through the launchers.
set -euo pipefail
W="$HOME/.claude/mcp-wrappers"

echo "== 1/3 Google OAuth client (skip with Enter if you don't use google-workspace)"
echo "   From Google Cloud Console > APIs & Services > Credentials > your Desktop OAuth client."
read -r -p "   Set it up now? [y/N] " A
if [ "${A:-n}" = "y" ]; then
  echo "   macOS asks for the CLIENT ID twice (hidden):"
  /usr/bin/security add-generic-password -U -a "$USER" -s mcp:google-client-id -w
  echo "   ...and the CLIENT SECRET twice (hidden):"
  /usr/bin/security add-generic-password -U -a "$USER" -s mcp:google-client-secret -w
  claude mcp remove google-workspace -s user >/dev/null 2>&1 || true
  claude mcp add google-workspace -s user -- "$W/google-workspace.sh" >/dev/null
  echo "   done: google-workspace starts via the Keychain launcher"
fi

echo "== 2/3 Canvas token (skip with Enter if you don't use Canvas)"
echo "   Make one in Canvas > Account > Settings > Approved Integrations > + New Access Token."
echo "   Give it an expiry date at the end of the term."
read -r -p "   Set it up now? [y/N] " B
if [ "${B:-n}" = "y" ]; then
  echo "   macOS asks for the token twice (hidden):"
  /usr/bin/security add-generic-password -U -a "$USER" -s mcp:canvas -w
  echo "== 3/3 register canvas"
  claude mcp remove canvas -s user >/dev/null 2>&1 || true
  claude mcp add canvas -s user -- "$W/canvas.sh" >/dev/null
fi
echo "Done. Start a NEW Claude session to get the tools. Rotate later by re-running this script."
