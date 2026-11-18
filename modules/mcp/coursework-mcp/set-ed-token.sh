#!/bin/sh
# The user runs this themselves to store their Ed Discussion API token in .env (chmod 600).
# Make the token at https://edstem.org/us/settings/api-tokens (read access is enough).
# The token is read without echo and never printed.
HERE="$(cd "$(dirname "$0")" && pwd)"
printf "Paste your Ed API token (input hidden), then press Return: "
stty -echo 2>/dev/null
IFS= read -r TOK
stty echo 2>/dev/null
echo
if [ -z "$TOK" ]; then echo "No token entered; nothing changed."; exit 1; fi
umask 077
printf 'ED_API_TOKEN=%s\n' "$TOK" > "$HERE/.env"
chmod 600 "$HERE/.env"
unset TOK
echo "Saved to $HERE/.env (only you can read it)."
