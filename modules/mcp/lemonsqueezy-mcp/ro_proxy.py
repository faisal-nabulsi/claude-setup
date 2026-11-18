#!/usr/bin/env python3
"""Read-only gate in front of @yawlabs/lemonsqueezy-mcp (stdio MCP, JSON-RPC one message per line).

Why: the upstream server has 64 tools, including refunds, subscription cancels, discounts and
checkouts. The user's rule is that nothing that moves money or changes customers runs without their
order in chat, and by default it should not even be possible. So:

- Writes OFF (default): tools/list only shows tools the upstream marks readOnlyHint=true, minus
  the webhook-sink tools (no sink is configured). A tools/call for any other tool is answered here
  with an error and never reaches the upstream server. run.sh also sets the upstream's own
  guardrails (class blocks, refund cap 0, destructive rate limit 0) as a second lock.
- Writes ON (LEMONSQUEEZY_WRITES_ENABLED=true in the environment of ONE process, e.g. through
  mcp_bridge.py): every tool passes through. Even then, Claude sends a write only after the user
  orders that exact write in chat.

Only the standard library, so startup stays fast.
"""
import json
import os
import subprocess
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
UPSTREAM = os.path.join(HERE, "node_modules", "@yawlabs", "lemonsqueezy-mcp", "dist", "index.js")
WRITES = os.environ.get("LEMONSQUEEZY_WRITES_ENABLED", "").strip().lower() in ("1", "true", "yes")
HIDDEN_PREFIXES = ("ls_sink_",)

out_lock = threading.Lock()
pending_lists = set()      # ids of tools/list requests we must filter
allowed = None             # tool names allowed after the first tools/list (None = not seen yet)
allowed_lock = threading.Lock()


def write_client(msg: dict) -> None:
    with out_lock:
        sys.stdout.write(json.dumps(msg, separators=(",", ":")) + "\n")
        sys.stdout.flush()


def tool_ok(tool: dict) -> bool:
    name = tool.get("name", "")
    if name.startswith(HIDDEN_PREFIXES):
        return False
    if WRITES:
        return True
    return bool((tool.get("annotations") or {}).get("readOnlyHint"))


def blocked_reply(req_id, name: str) -> dict:
    text = (f"Blocked by the read-only gate: '{name}' is not a read tool. Writes are off by default "
            "(LEMONSQUEEZY_WRITES_ENABLED is not set). A write needs the user's explicit order in chat, "
            "then one process started with LEMONSQUEEZY_WRITES_ENABLED=true (see README).")
    return {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": text}], "isError": True}}


def pump_upstream(proc: subprocess.Popen) -> None:
    global allowed
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        mid = msg.get("id")
        if mid is not None and mid in pending_lists and "result" in msg:
            pending_lists.discard(mid)
            tools = [t for t in msg["result"].get("tools", []) if tool_ok(t)]
            with allowed_lock:
                allowed = (allowed or set()) | {t["name"] for t in tools}
            msg["result"]["tools"] = tools
            if mid == "ro-gate-list":  # our own internal request: the client never asked for it
                continue
        write_client(msg)
    os._exit(proc.wait() or 0)


def is_allowed(name: str, proc_list_fetch) -> bool:
    if name.startswith(HIDDEN_PREFIXES):
        return False
    if WRITES:
        return True
    with allowed_lock:
        known = allowed
    if known is None:  # client called a tool before listing: ask upstream for the list ourselves
        known = proc_list_fetch()
    return name in known


def main() -> None:
    env = dict(os.environ)
    node = env.get("LS_NODE") or "node"
    proc = subprocess.Popen([node, UPSTREAM], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=sys.stderr, env=env, text=True, bufsize=1)
    threading.Thread(target=pump_upstream, args=(proc,), daemon=True).start()

    def send_up(msg: dict) -> None:
        proc.stdin.write(json.dumps(msg, separators=(",", ":")) + "\n")
        proc.stdin.flush()

    def fetch_list():
        # Rare path: the gate needs the read-only set before any tools/list went by.
        import time
        rid = "ro-gate-list"
        pending_lists.add(rid)
        send_up({"jsonrpc": "2.0", "id": rid, "method": "tools/list"})
        for _ in range(300):
            with allowed_lock:
                if allowed is not None:
                    return allowed
            time.sleep(0.05)
        return set()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        method = msg.get("method")
        if method == "tools/list" and "id" in msg:
            pending_lists.add(msg["id"])
        elif method == "tools/call" and "id" in msg:
            name = (msg.get("params") or {}).get("name", "")
            if not is_allowed(name, fetch_list):
                write_client(blocked_reply(msg["id"], name))
                continue
        send_up(msg)
    proc.stdin.close()
    proc.wait()


if __name__ == "__main__":
    main()
