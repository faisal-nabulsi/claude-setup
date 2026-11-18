# The MCP bridge: calling connectors from scripts and scheduled jobs

**The problem:** MCP tools (Canvas, Gmail, Gradescope...) are loaded into interactive Claude
sessions. But scheduled tasks, launchd jobs and plain scripts often run without them, and a
model with no connector reaches for the browser instead, which is slow and fragile.

**The fix:** `modules/mcp/bridge/mcp_bridge.py` (installed to `~/code/scripts/mcp_bridge.py`) is
a ~150-line MCP client with no dependencies. It reads how a server is started from
`~/.claude.json` (the same entry `claude mcp add` wrote), starts it, does the MCP handshake,
calls one tool, prints the result and exits.

```bash
python3 ~/code/scripts/mcp_bridge.py <server> list                          # every tool + its JSON input schema
python3 ~/code/scripts/mcp_bridge.py <server> call <tool> '<json args>' [timeout_s] [--max-chars N]

python3 ~/code/scripts/mcp_bridge.py canvas call get_my_upcoming_assignments '{}'
python3 ~/code/scripts/mcp_bridge.py google-workspace call manage_email \
  '{"operation":"search","email":"you@gmail.com","query":"newer_than:1d"}'
```

Where it's used in this repo:
- the morning-brief scheduled task (Canvas + mail checks),
- `assignment-watch` (Canvas, from launchd),
- autohub sources,
- the life vault's mail feed.

Notes:
- **Secrets:** the bridge passes through whatever the server's launcher does (Keychain, `.env`).
  It never prints secrets, and the `secrets_guard` hook blocks running the launchers by hand.
- **Write gates:** servers with a write switch (espn-fantasy `WRITES_ENABLED`, lemonsqueezy
  `LEMONSQUEEZY_WRITES_ENABLED`) read it from the environment, so you can unlock writes for ONE
  bridge call by prefixing that call, and only after you've ordered that exact write in chat.
- **Output:** full results by default. `--max-chars N` cuts and says so on stderr and as the last
  stdout line, so a cut never goes unnoticed.
- `GW_DEBUG=1` shows the server's stderr if a call hangs.
