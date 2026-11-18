#!/bin/sh
# Paste ESPN cookies into .env without showing them on screen.
# Get them from fantasy.espn.com (logged in): DevTools > Application > Cookies > https://fantasy.espn.com
cd "$(dirname "$0")" || exit 1
printf "espn_s2 (hidden): "; stty -echo; read -r S2; stty echo; echo
printf "SWID, with braces (hidden): "; stty -echo; read -r SW; stty echo; echo
S2="$S2" SW="$SW" python3 - <<'PY'
import os, re
p = ".env"
t = open(p).read()
for k, v in (("ESPN_S2", os.environ["S2"].strip()), ("ESPN_SWID", os.environ["SW"].strip())):
    t = re.sub(rf"^{k}=.*$", lambda m: f"{k}={v}", t, flags=re.M)
open(p, "w").write(t)
os.chmod(p, 0o600)
print("Saved to .env (values not shown).")
PY
