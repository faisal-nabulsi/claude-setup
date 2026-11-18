# lemonsqueezy-mcp (optional)

A local MCP server for a Lemon Squeezy store (Lemon Squeezy sells digital products and subscriptions). It is **read-only by default**.

## What it is

- **Upstream:** [@yawlabs/lemonsqueezy-mcp](https://github.com/YawLabs/lemonsqueezy-mcp), version **1.1.3**, pinned in `package.json` and installed with `npm install --ignore-scripts`. It is a community project (MIT), not an official Lemon Squeezy one; Lemon Squeezy has no official MCP. It was picked because it is actively maintained and has its own guardrails.
- **Code review (1.1.3):** the only network host it calls is `api.lemonsqueezy.com`. A webhook "sink" URL is optional and not set. It runs subprocesses only for `LEMONSQUEEZY_API_KEY_COMMAND`, which we don't use. It sends no telemetry.
- **`ro_proxy.py`** sits between Claude and the upstream server. It is the read-only gate.
- **`run.sh`** is the launcher. It reads the key from `.env` and sets the upstream guardrails.
- **`set-key.sh`** is the one-time setup that the user runs themselves.

## Read-only gate: how writes are kept off

1. **The gate (`ro_proxy.py`).** Claude only sees the 41 tools the upstream marks `readOnlyHint` (stores, products, variants, prices, files, orders, order items, subscriptions, subscription invoices and items, usage, discounts, discount redemptions, license keys and instances, checkouts, webhooks, affiliates, customers, and license validation). The gate answers any other tool call with an error itself, so that call never reaches Lemon Squeezy.
2. **Upstream guardrails, as a second lock.** These are set in `run.sh`:
   - `LEMONSQUEEZY_DISABLE_CLASSES=mutate,money,recurring,key,webhook`
   - refund cap `0`
   - destructive-call rate limit `0`

**Writes** (refunds, subscription cancels or changes, discounts, checkouts, license-key changes, customer edits) need two things:

1. The user orders that exact write in chat.
2. A single process is started with `LEMONSQUEEZY_WRITES_ENABLED=true`:
   ```bash
   LEMONSQUEEZY_WRITES_ENABLED=true python3 ~/code/scripts/mcp_bridge.py lemonsqueezy call ls_refund_order '{"id":"...","amount":...}'
   ```
   In that mode the gate passes every tool through. The upstream still caps a refund at $80 (`LEMONSQUEEZY_MAX_REFUND_AMOUNT_CENTS=8000`) and allows at most 2 destructive calls a minute. The registered server (the one Claude Code starts) never has the flag set.

## Setup (the user, once)

1. Create an API key at https://app.lemonsqueezy.com/settings/api.
2. Run `sh ~/code/lemonsqueezy-mcp/set-key.sh` and paste the key. Your typing stays hidden, and the key is saved to `.env` (chmod 600).

Claude never reads, prints or echoes `.env`.

The key works for the whole Lemon Squeezy account, which has every store and every customer. Lemon Squeezy has no read-only keys, and that is why the gate exists.

## Use

- **In Claude Code:** the tools are `mcp__lemonsqueezy__ls_*`. Useful ones:
  - `ls_list_stores`: shows revenue totals and 30-day revenue.
  - `ls_list_orders` and `ls_list_subscriptions`: each takes a `storeId` filter.
  - `ls_list_license_keys`
  - `ls_list_customers`
- **Scripts and scheduled runs:**
  ```bash
  python3 ~/code/scripts/mcp_bridge.py lemonsqueezy call ls_list_stores '{}'
  ```
- **Registered with:**
  ```bash
  claude mcp add --scope user lemonsqueezy -- /bin/sh ~/code/lemonsqueezy-mcp/run.sh
  ```
  Remove it with `claude mcp remove lemonsqueezy -s user`.
- **Startup:** about 0.2 s (Python gate plus Node 22). Node comes from nvm and is found by `run.sh`.

## Upgrading

1. Change the version in `package.json`.
2. Run `npm install --ignore-scripts`.
3. Re-check the code: network hosts and `child_process` (see the review notes above).
4. Run the probe in the next section and confirm that the tool list is still read-only.

The gate goes by the upstream's `readOnlyHint` annotations, so a write tool added in a new version stays hidden unless the upstream wrongly marks it read-only.

## Test without a key

Run `sh run.sh` and send `initialize` and `tools/list`. You should see 41 tools. A call to `ls_refund_order` should return "Blocked by the read-only gate".
