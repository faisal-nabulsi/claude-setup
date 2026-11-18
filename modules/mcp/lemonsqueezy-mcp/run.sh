#!/bin/sh
# Starts the Lemon Squeezy MCP behind the read-only gate (ro_proxy.py).
# The API key lives in .env next to this file (chmod 600), written only by ./set-key.sh,
# which the user runs themselves. Nothing here prints the key.
HERE="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$HERE/.env"

if [ -f "$ENV_FILE" ]; then
  # Read only the one variable we need, without echoing it.
  LEMONSQUEEZY_API_KEY="$(sed -n 's/^LEMONSQUEEZY_API_KEY=//p' "$ENV_FILE" | head -n 1 | tr -d '"'"'"' \r')"
  export LEMONSQUEEZY_API_KEY
fi
if [ -z "$LEMONSQUEEZY_API_KEY" ]; then
  echo "lemonsqueezy-mcp: no API key yet. The user runs: sh $HERE/set-key.sh" >&2
  # Still start, so the server registers; every API tool will return an auth error until the key is set.
fi

# Node: nvm's node is not on PATH in launchd/scheduled runs, so look in the usual places.
LS_NODE="$(command -v node 2>/dev/null)"
[ -z "$LS_NODE" ] && LS_NODE="$(ls -d "$HOME"/.nvm/versions/node/v2[2-9]*/bin/node 2>/dev/null | tail -n 1)"
[ -z "$LS_NODE" ] && [ -x /opt/homebrew/bin/node ] && LS_NODE=/opt/homebrew/bin/node
export LS_NODE

export LEMONSQUEEZY_MCP_RUNTIME=node   # run on plain Node, skip the oam probe
export LEMONSQUEEZY_LOG=error

case "$(printf %s "$LEMONSQUEEZY_WRITES_ENABLED" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes)
    # Writes on for THIS process only. Still: only after the user orders the exact write in chat.
    unset LEMONSQUEEZY_DISABLE_CLASSES
    export LEMONSQUEEZY_DESTRUCTIVE_RATE_LIMIT=2
    export LEMONSQUEEZY_MAX_REFUND_AMOUNT_CENTS="${LEMONSQUEEZY_MAX_REFUND_AMOUNT_CENTS:-8000}"
    export LEMONSQUEEZY_LOG=audit
    ;;
  *)
    # Default: second lock behind the gate. The gate already hides every non-read tool.
    export LEMONSQUEEZY_DISABLE_CLASSES=mutate,money,recurring,key,webhook
    export LEMONSQUEEZY_MAX_REFUND_AMOUNT_CENTS=0
    export LEMONSQUEEZY_DESTRUCTIVE_RATE_LIMIT=0
    ;;
esac

exec /usr/bin/python3 "$HERE/ro_proxy.py"
