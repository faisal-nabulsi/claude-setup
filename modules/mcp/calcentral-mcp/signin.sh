#!/bin/sh
# Opens a Chrome window for the user to sign in to CalCentral themselves. See signin.py.
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE" && exec "$HERE/.venv/bin/python" "$HERE/signin.py" "$@"
