"""Your Lemon Squeezy store through the lemonsqueezy connector, read tools only.

Push lines carry amounts and plan names only, never customer names or emails, because pushes
travel through ntfy's public relay.
"""
from datetime import datetime, timedelta
import os

LABEL = os.environ.get("AUTOHUB_STORE_LABEL", "Store")

from . import bridge, register

MAX_PAGES = 5
LOUD_SUB = {"cancelled", "expired"}


def _rows(sess, tool) -> list:
    rows = []
    for page in range(1, MAX_PAGES + 1):
        res = sess.call(tool, {"pageSize": 100, "pageNumber": page})
        data = res.get("data", []) if isinstance(res, dict) else res
        rows += data or []
        last = (((res.get("meta") or {}).get("page") or {}).get("lastPage", 1)) if isinstance(res, dict) else 1
        if page >= (last or 1):
            break
    return rows


def _fetch(tools):
    try:
        with bridge.session("lemonsqueezy") as s:
            return [_rows(s, t) for t in tools]
    except bridge.BridgeError as e:
        if "LEMONSQUEEZY_API_KEY" in str(e):
            raise RuntimeError("no Lemon Squeezy key: run sh ~/code/lemonsqueezy-mcp/set-key.sh")
        raise


def _a(row) -> dict:
    return row.get("attributes", row) if isinstance(row, dict) else {}


def _variant(attrs) -> str:
    return (attrs.get("first_order_item") or {}).get("variant_name") or attrs.get("variant_name") or "product"


@register("ls_sales")
def fetch_sales(params: dict, ctx) -> list:
    orders, subs = _fetch(["ls_list_orders", "ls_list_subscriptions"])
    items = []
    for o in orders:
        a, st = _a(o), _a(o).get("status")
        text = ""
        if st == "paid":
            text = f"{LABEL}: new order {a.get('total_formatted', '')} ({_variant(a)})"
        elif st in ("refunded", "partial_refund"):
            text = f"{LABEL}: refund on {a.get('total_formatted', '')} order"
        items.append({"key": f"ord:{o.get('id')}:{st}", "text": text})
    for s in subs:
        a, st = _a(s), _a(s).get("status")
        text = f"{LABEL}: subscription {st} ({_variant(a)})" if st in LOUD_SUB else ""
        items.append({"key": f"sub:{s.get('id')}:{st}", "text": text})
    return items


def _created(a):
    ts = a.get("created_at")
    if not ts:
        return None
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)


@register("ls_weekly")
def fetch_weekly(params: dict, ctx) -> list:
    now = ctx.now if ctx else datetime.now()
    orders, subs = _fetch(["ls_list_orders", "ls_list_subscriptions"])
    since = now - timedelta(days=7)
    week = [_a(o) for o in orders if (_created(_a(o)) or datetime.min) >= since]
    paid = [a for a in week if a.get("status") == "paid"]
    refunds = [a for a in week if a.get("status") in ("refunded", "partial_refund")]
    revenue = sum(int(a.get("total_usd", a.get("total", 0)) or 0) for a in paid) / 100
    active = sum(1 for s in subs if _a(s).get("status") in ("active", "on_trial"))
    y, w, _ = now.isocalendar()
    return [{"key": f"week:{y}-W{w:02d}",
             "text": f"{LABEL} week: {len(paid)} orders, ${revenue:,.2f} revenue, {len(refunds)} refunds, {active} active subs"}]
