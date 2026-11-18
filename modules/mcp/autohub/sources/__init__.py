"""Source kinds. Each fetch function: fetch(params: dict, ctx) -> list[Item].

Item = {"key": str, "text": str, "group": str (optional)}. A fetch raises on failure.
Kinds register themselves in REGISTRY when their module is imported (see _load()).
"""
from typing import Callable, Dict

REGISTRY: Dict[str, Callable] = {}


def register(kind: str):
    def deco(fn):
        REGISTRY[kind] = fn
        return fn
    return deco


@register("fake")
def _fake(params, ctx):
    """Test-only kind: returns params["items"]; raises params["raise"] if set."""
    if params.get("raise"):
        raise RuntimeError(params["raise"])
    return list(params.get("items", []))


def _load():
    # Import the real source modules so they register. Each is stdlib-only and cheap to import.
    import importlib
    for mod in ("generic", "canvas", "grades", "lemonsqueezy"):
        try:
            importlib.import_module(f"{__name__}.{mod}")
        except ModuleNotFoundError as e:
            if e.name != f"{__name__}.{mod}":
                raise


_load()
