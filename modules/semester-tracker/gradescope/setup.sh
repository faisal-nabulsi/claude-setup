#!/bin/bash
# One-time (and password-change) setup for the Gradescope reader. Run it yourself:
#   bash ~/semester-tracker/gradescope/setup.sh
# It stores your Gradescope email and password in the macOS Keychain and test-logs-in.
# Claude never sees either value; the password is typed straight into macOS's own prompt.
set -euo pipefail
PY="$HOME/.local/share/gradescope-reader/venv/bin/python"
DIR="$(cd "$(dirname "$0")" && pwd)"
if [ ! -x "$PY" ]; then
  echo "Creating the reader's own Python environment (one time)..."
  python3 -m venv "$HOME/.local/share/gradescope-reader/venv"
  "$PY" -m pip install -q "gradescopeapi==1.8.1"
fi

echo "Gradescope reader setup"
echo "You need a Gradescope password (separate from your school's single sign-on). If you"
echo "only ever used 'School Credentials', set one first: gradescope.com > Log In >"
echo "Forgot your password? with your school email. School login keeps working either way."
echo
read -r -p "Gradescope email: " EMAIL
EMAIL=$(printf '%s' "$EMAIL" | tr -d '[:space:]')
case "$EMAIL" in *@*.*) ;; *) echo "That doesn't look like an email; nothing saved."; exit 1;; esac
printf 'add-generic-password -U -a "%s" -s gradescope-email -w "%s"\n' "$USER" "$EMAIL" | /usr/bin/security -i >/dev/null
echo "Saved the email to Keychain (item gradescope-email)."
echo
echo "Now macOS asks for the password twice (nothing shows while you type):"
/usr/bin/security add-generic-password -U -a "$USER" -s gradescope-password -w
echo "Saved the password to Keychain (item gradescope-password)."
echo
echo "Testing the login (reads your course list, changes nothing)..."
if "$PY" "$DIR/reader.py" --check; then
  echo
  echo "Works. Tell Claude \"gradescope setup done\" and it will review a dry run with you"
  echo "before the reader's first real run and its 4-times-a-day schedule."
else
  echo
  echo "Login failed. Re-run this script to retype the password. If Gradescope says your school"
  echo "requires school login, the reader can't work; tell Claude."
  exit 1
fi
