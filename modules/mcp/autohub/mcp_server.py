"""autohub management MCP: lets Claude list, add, test, pause and switch the user's automations.

Registered with: claude mcp add -s user autohub -- ~/code/autohub/.venv/bin/python ~/code/autohub/mcp_server.py
Startup is cheap: only FastMCP and autohub (standard library) are imported here. Sources that call
connectors start those servers inside a tool call, never at import.
Rules the tools enforce: new recipes are saved as dry_run; the only action is a push to the user's
phone; connector sources must be on allowed_sources.json, which no tool here can change.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcp.server.fastmcp import FastMCP  # noqa: E402

import autohub  # noqa: E402

mcp = FastMCP("autohub", log_level="WARNING")


@mcp.tool()
def list_recipes() -> list:
    """Every automation: id, on/off, dry_run or live, schedule, last run, failures in a row, note.
    Broken recipe files show as {"error": ...}."""
    return autohub.summary()


@mcp.tool()
def add_recipe(recipe_json: str) -> dict:
    """Save a new automation (always as dry_run, whatever mode it says). recipe_json is one recipe:
    {"id": "kebab-id", "note": "one plain sentence", "enabled": true, "every_minutes": 15 (or
    "weekly_at": "Sun 18:00"), "source": {"kind": ..., ...}, "action": {"kind": "push", "title": ...}}.
    Source kinds: canvas_posted, grades, ls_sales, ls_weekly, mcp_call (server, tool, args, list_path,
    id_field, text_template; the tool must be on allowed_sources.json). Test it with run_now."""
    try:
        r = json.loads(recipe_json)
    except ValueError as e:
        return {"ok": False, "errors": [f"not valid JSON: {e}"]}
    if not isinstance(r, dict):
        return {"ok": False, "errors": ["recipe must be a JSON object"]}
    r["mode"] = "dry_run"
    errs = autohub.validate_recipe(r)
    if errs:
        return {"ok": False, "errors": errs}
    path = autohub.RECIPES_DIR / f"{r['id']}.json"
    if path.exists():
        return {"ok": False, "errors": [f"a recipe called {r['id']!r} already exists"]}
    autohub.atomic_write(path, json.dumps(r, indent=2) + "\n")
    return {"ok": True, "saved": str(path), "mode": "dry_run",
            "next": "run_now(id) to preview; it starts seeding on the next tick"}


def _op(fn, *a):
    try:
        fn(*a)
        return {"ok": True}
    except autohub.HubError as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def enable(id: str) -> dict:
    """Turn an automation on."""
    return _op(autohub.set_enabled, id, True)


@mcp.tool()
def disable(id: str) -> dict:
    """Turn an automation off (it keeps its memory of what it has seen)."""
    return _op(autohub.set_enabled, id, False)


@mcp.tool()
def set_mode(id: str, mode: str) -> dict:
    """Switch an automation between "dry_run" (log only) and "live" (really pushes the user's phone).
    Switch to live ONLY after the user says so in chat for this recipe."""
    return _op(autohub.set_mode, id, mode)


@mcp.tool()
def run_now(id: str, preview: bool = True) -> dict:
    """Run one automation now. preview=True (default) shows what it found and what it would push,
    and changes nothing. preview=False runs it for real in its own mode and records what it saw."""
    try:
        rec = autohub.run_one(id, preview=preview)
    except autohub.HubError as e:
        return {"ok": False, "error": str(e)}
    rec["preview"] = preview
    return rec


@mcp.tool()
def recent_runs(n: int = 20, id: str = None) -> list:
    """The last n run records (time, recipe, mode, items found, new, pushed, error)."""
    return autohub.recent_runs(n, id)


@mcp.tool()
def pause_all() -> dict:
    """Pause every automation (launchd keeps ticking but nothing runs)."""
    autohub.pause()
    return {"ok": True, "paused": True}


@mcp.tool()
def resume_all() -> dict:
    """Undo pause_all."""
    autohub.resume()
    return {"ok": True, "paused": False}


@mcp.tool()
def allowed_sources() -> dict:
    """Connector tools autohub may read from (read-only list; changing it needs the user's OK)."""
    from sources import bridge
    return json.loads(Path(bridge.ALLOW_FILE).read_text())


if __name__ == "__main__":
    mcp.run()
