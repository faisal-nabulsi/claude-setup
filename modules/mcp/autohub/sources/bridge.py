"""The one door from autohub to the user's connectors (MCP servers).

Every connector call goes through call() or session(), which refuse any (server, tool, operation)
not listed in allowed_sources.json. At launch that list holds read tools only. Adding a tool to it
needs the user's OK in chat. The calls themselves reuse ~/code/scripts/mcp_bridge.py, which starts
the server the same way Claude Code does (its launcher reads its own secrets).
"""
import atexit
import contextlib
import importlib.util
import json
from pathlib import Path

ALLOW_FILE = Path(__file__).resolve().parent.parent / "allowed_sources.json"
BRIDGE_PATH = Path.home() / "code" / "scripts" / "mcp_bridge.py"
INIT_TIMEOUT = 40
_open_clients = set()


class NotAllowed(Exception):
    pass


class BridgeError(Exception):
    pass


def _allow() -> dict:
    return json.loads(Path(ALLOW_FILE).read_text())


def is_allowed(server: str, tool: str, args: dict) -> bool:
    srv = _allow().get(server)
    rule = srv.get(tool) if isinstance(srv, dict) else None
    if not isinstance(rule, dict):
        return False
    ops = rule.get("operation")
    return ops is None or (args or {}).get("operation") in ops


def _mcp_bridge():
    spec = importlib.util.spec_from_file_location("mcp_bridge", BRIDGE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@atexit.register
def _close_all():
    for c in list(_open_clients):
        with contextlib.suppress(Exception):
            c.close()


class Session:
    def __init__(self, server: str, client):
        self.server, self.client = server, client

    def call(self, tool: str, args: dict = None, timeout: int = 45):
        args = args or {}
        if not is_allowed(self.server, tool, args):
            raise NotAllowed(f"{self.server}.{tool} is not on autohub's allowed list")
        r = self.client.call("tools/call", {"name": tool, "arguments": args}, timeout=timeout)
        if "error" in r:
            raise BridgeError(json.dumps(r["error"])[:200])
        res = r.get("result") or {}
        text = "\n".join(p.get("text", "") for p in res.get("content", []) if isinstance(p, dict))
        if res.get("isError"):
            raise BridgeError(text.strip()[:200] or "tool returned an error")
        try:
            return json.loads(text)
        except ValueError:
            raise BridgeError("non-JSON result: " + text.strip()[:160])


@contextlib.contextmanager
def session(server: str):
    """One started server for several calls. Raises NotAllowed before starting anything if the
    server has no allowed tools at all."""
    if not isinstance(_allow().get(server), dict):
        raise NotAllowed(f"{server} has no tools on autohub's allowed list")
    mb = _mcp_bridge()
    mb.SERVER = server
    client = mb.Client()
    _open_clients.add(client)
    try:
        r = client.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                       "clientInfo": {"name": "autohub", "version": "1.0"}},
                        timeout=INIT_TIMEOUT)
        if "error" in r:
            raise BridgeError(f"{server} failed to start: " + json.dumps(r["error"])[:150])
        client.send("notifications/initialized", notify=True)
        yield Session(server, client)
    finally:
        _open_clients.discard(client)
        with contextlib.suppress(Exception):
            client.close()


def call(server: str, tool: str, args: dict = None, timeout: int = 45):
    if not is_allowed(server, tool, args or {}):
        raise NotAllowed(f"{server}.{tool} is not on autohub's allowed list")
    with session(server) as s:
        return s.call(tool, args, timeout)
