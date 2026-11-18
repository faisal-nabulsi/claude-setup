"""Generic source: "call connector X's tool Y, find the list at list_path, push items not seen yet".

params: server, tool, args, list_path ("a.b", "" = whole result), id_field, text_template
(str.format over each item, e.g. "{name} ({meta[pts]} pts)"). Only allow-listed tools work.
"""
from . import bridge, register


def _walk(obj, path: str):
    for part in [p for p in (path or "").split(".") if p]:
        obj = obj[part]
    return obj


@register("mcp_call")
def fetch_mcp_call(params: dict, ctx) -> list:
    tool = params["tool"]
    result = bridge.call(params["server"], tool, params.get("args") or {})
    rows = _walk(result, params.get("list_path", ""))
    if not isinstance(rows, list):
        raise ValueError(f"list_path {params.get('list_path')!r} did not lead to a list")
    items = []
    for row in rows:
        rid = row.get(params["id_field"]) if isinstance(row, dict) else None
        if rid is None:
            continue
        try:
            text = params.get("text_template", "{" + params["id_field"] + "}").format(**row)
        except (KeyError, IndexError, TypeError, ValueError):
            text = f"{tool}: new item {rid}"
        items.append({"key": f"{tool}:{rid}", "text": text})
    return items
