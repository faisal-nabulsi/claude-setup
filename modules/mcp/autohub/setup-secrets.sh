#!/bin/sh
# The user runs this themselves, once (and again if the token expires or they revokes it).
# It stores autohub's own Canvas access token in the macOS Keychain (item "autohub:canvas").
# Claude never runs this and never sees the token.
#
# 1. In Canvas: Account > Settings > "+ New Access Token". Purpose: autohub. Leave expiry empty
#    or set one. Copy the token it shows (it is shown only once).
# 2. Run:  sh ~/code/autohub/setup-secrets.sh
#    macOS asks for the token twice ("password data"). Your typing stays hidden.
set -e
echo "Paste autohub's Canvas token when asked (twice). Nothing you type is shown."
security add-generic-password -U -s autohub:canvas -a "$USER" -l "autohub Canvas token" -w
echo "Saved to Keychain item autohub:canvas."
echo "Check it: python3 ~/code/autohub/autohub.py run canvas-posted --preview"
