# Phase 12 — Manual Loop Trading Mode (Human-Only)

This document specifies a **Manual Loop Trading Mode** designed for a human user to run a repeated **BUY → SELL → BUY → …** loop based on simple, deterministic price conditions.

It also consolidates several operational behaviors that were implemented but not previously captured in a single place (order source tagging, reconciliation behavior, dust ledger, and PnL helpers).

No automated agent/LLM is allowed to start this feature.

---

## 12.1 Goal and Non-Goals

### Goal

Provide a CLI-driven loop session that:

- submits an entry order (BUY)
- waits until it is **fully filled** (MVP)
- submits the next order (SELL) at a configured offset
- after SELL is filled, optionally submits the next BUY at a configured offset
- repeats for a configured number of cycles (or until stopped)
- tracks **per-cycle realized PnL** and cumulative realized PnL
- is restart-safe (session state persisted in SQLite)

### Non-Goals (MVP)

- no background daemon requirement (CLI loop is acceptable)
- no strategy engine, no market planning, no Phase 5/6/7 pipeline integration
- no multi-symbol loops
- no auto dust sweep / auto conversion
- no LLM/agent initiation

---

## 12.2 Human-Only Safety Gate (Locked)

Manual Loop Trading must require:

- an explicit `--i-am-human` flag
- an interactive confirmation (TTY prompt) before starting

Optional:

- `--dry-run` mode: run validations + previews + live reads, but **no POST/DELETE** to Binance

---

## 12.3 Execution Environment (Locked)

The loop must:

- print `Network: MAINNET|TESTNET` and `Base URL: ...` in the preview
- execute against the runtime environment from TOML (`binance.testnet = true|false`)
- never mix environments within a single session

---

## 12.4 CLI Surface (Locked)

### Command namespace

Recommended:

```bash
cryptogent trade manual loop ...
```

### Core commands

```bash
cryptogent trade manual loop create ...
cryptogent trade manual loop start ...
cryptogent trade manual loop status [--loop-id <id>]
cryptogent trade manual loop stop --i-am-human [--loop-id <id>]
cryptogent trade manual loop list
cryptogent trade manual loop reconcile --i-am-human [--loop-id <id>] [--loop --interval-seconds N --duration-seconds M]
```

### UX Rule (Locked)

Normal user flow should be:

1) `loop start` (creates session, submits entry BUY, and **runs the loop**)
2) `loop status`
3) `loop stop`

`loop reconcile` is an **advanced recovery/resume** tool (restart, crash recovery, debugging) and is not required for normal usage.

### Presets (Locked)

You can store a loop configuration as a preset, then start it later by id:

```bash
cryptogent trade manual loop create --symbol SOLUSDT --quote-qty 1000 --entry-type BUY_MARKET --take-profit-pct 1.0 --rebuy-pct -1
cryptogent trade manual loop start --i-am-human --id <preset_id> --max-cycles 3
```

### Direct Start Auto-Preset (Locked)

If you start a loop without `--id`, CryptoGent will **auto-create a preset** internally and link the session to it.
This ensures every loop session has a reusable, saved strategy template.

### Start inputs (MVP)

- `--symbol` (e.g. `SOLUSDT`)
- sizing (locked): `--quote-qty` only
- entry type (locked): `--entry-type BUY_MARKET|BUY_LIMIT`
  - if `BUY_LIMIT`: must provide `--entry-limit-price`
- sell target offset:
  - either absolute: `--take-profit-abs <quote>` (e.g. `0.50`)
  - or percent: `--take-profit-pct <pct>` (e.g. `1.0`)
- rebuy target offset (after a SELL fill):
  - either absolute: `--rebuy-abs <quote>` or percent: `--rebuy-pct <pct>`
  - **signed rule (locked):**
    - `--rebuy-abs 0.05` means dip (below last sell)
    - `--rebuy-abs +0.05` means momentum (above last sell)
    - `--rebuy-pct -1` dip, `--rebuy-pct +1` momentum
  - rebuy price is calculated from the **last SELL fill average price**
- `--max-cycles <n>` (locked): `0 = infinite`, default `1`
- optional stop condition: `--stop-loss-abs` or `--stop-loss-pct` (reference: last BUY fill average price)
  - stop-loss action (locked):
    - `stop_only` (default): stop loop + cancel open order, do not sell
    - `stop_and_exit`: stop loop + cancel open order + submit MARKET SELL for protective exit

---

## 12.5 Persistence Model (Implemented)

Tables:

### `loop_sessions` (one row per loop run)

- `loop_id` (PK)
- `created_at_utc`, `updated_at_utc`
- `status` (`running|stopped|completed|error|dry_run`)
- `execution_environment` (`mainnet|testnet`)
- `base_url`
- `preset_id` (links the session to a stored preset; direct start auto-creates one)
- `symbol`
- sizing config (`quote_qty`)
- offsets config (take-profit + rebuy)
- stop-loss config
- `max_cycles`
- `cycles_completed`
- `cumulative_realized_pnl_quote`
- `pnl_quote_asset` (symbol quote)
- `last_error` / `last_warning`

### `loop_legs` (one row per submitted order/leg)

Tracks each submitted order and its lifecycle.

- `leg_id` (PK)
- `loop_id` (FK)
- `cycle_index`
- `side` (`BUY|SELL`)
- `order_type` (MVP uses `MARKET_BUY|LIMIT_BUY|LIMIT_SELL`)
- `client_order_id`, `binance_order_id`
- `requested_qty`, `quote_order_qty`, `limit_price`, `time_in_force`
- `local_status` (`submitting|open|filled|cancelled|expired|uncertain_submitted|failed`)
- execution outputs (`executed_qty`, `avg_fill_price`, `total_quote_value`, fee breakdown)
- timestamps (`submitted_at_utc`, `reconciled_at_utc`, etc.)

### `loop_events`

Append-only audit trail for session decisions/actions:

- `loop_event_id`, `loop_id`, `created_at_utc`
- `event_type` (submit, reconcile, filled, stop_triggered, etc.)
- structured fields (when relevant): `preset_id`, `symbol`, `side`, `cycle_number`, `client_order_id`, `binance_order_id`, `price`, `quantity`, `message`
- `details_json` (extra context)

---

## 12.6 Idempotency and Reconciliation (Locked)

All loop-submitted orders must:

- include `newClientOrderId`
- persist `client_order_id` before submission
- reuse the same `client_order_id` on retry

On timeouts/unknown submission result:

- mark as `uncertain_submitted`
- reconcile via `GET /api/v3/order` using `origClientOrderId`
- retry at most once (MVP)

---

## 12.7 “One Active Position” Rule (Locked)

For a given loop session:

- only one loop-managed position may be open at a time
- the loop must not open a second exposure before the previous BUY is fully resolved (MVP)

This prevents unintended exposure growth.

---

## 12.8 Stop-Loss Rule (Locked)

Stop-loss reference price is the **last BUY fill average price**.

### Stop-Loss Action (Locked)

Default behavior is **stop_only** (safer).
Optionally, users can enable **stop_and_exit** to submit a MARKET SELL on stop-loss.

---

## 12.8.1 Order Cleanup Policy (Locked)

When a loop stops or completes, it must apply an **Order Cleanup Policy**.

Default policy: `cancel-open-and-exit`.

### When it applies

The cleanup policy applies when the loop:

- is force-stopped (Ctrl‑B in the reconcile runner)
- is manually stopped (`cryptogent trade manual loop stop ...`)
- reaches `max_cycles` and completes

### Policies (locked)

- `cancel-open` (default)  
  Cancel all **open (or partially-filled) LIMIT orders created by this loop session** when the loop stops/completes.
  Filled orders remain unchanged and are kept as history.

- `none`  
  Do nothing; loop-created open orders remain on the exchange.

- `cancel-open-and-exit`  
  Cancel loop-created open LIMIT orders, then attempt to **exit any remaining base balance** with a MARKET SELL (best-effort).
  This is intended to leave the wallet flat for the loop symbol after stopping (subject to minQty/minNotional).

### Important notes

- Filled orders cannot be cancelled (exchange has already executed them).
- Partially filled LIMIT orders: the remaining unfilled portion is cancelable and must be cancelled by `cancel-open` / `cancel-open-and-exit`.
- Cleanup must only manage **orders created by this loop session** (tracked in `loop_legs`). It must not cancel:
  - unrelated manual orders
  - execution-phase orders
  - external orders created outside CryptoGent
- Stop-loss action precedence:
  - If stop-loss triggers and `stop_loss_action = stop_only`, cleanup must **not** force an exit sell (it may still cancel open orders).
  - If `stop_loss_action = stop_and_exit`, the protective exit is executed by stop-loss logic; cleanup must not double-sell.

### Exit behavior details (for `cancel-open-and-exit`)

- The MARKET SELL exit must respect exchange rules (stepSize/minQty/minNotional).
- If the remaining balance is too small to sell (dust), the exit must be skipped and the loop should stop cleanly.
- Filled exit orders are recorded as a loop leg in `loop_legs` for auditability.

### CLI configuration

Cleanup policy can be set at preset creation:

```bash
cryptogent trade manual loop create ... --cleanup-policy cancel-open|none|cancel-open-and-exit
```

And can be overridden at runtime:

```bash
cryptogent trade manual loop start ... --cleanup-policy cancel-open|none|cancel-open-and-exit
```

---

## 12.9 Partial Fill Handling (Locked)

The loop advances **only after FULL fill**.
Partial fills update execution quantities but do **not** advance the cycle.

---

## 12.10 Profit Accounting (Locked rules reused)

### Realized PnL

- computed **only from executed SELL fills**
- cost basis = **Average Cost** (per-loop position basis)
- quote fees reduce proceeds directly
- non-base/non-quote fees: store and warn; no silent conversion

### Unrealized PnL

- applies only to an open loop-managed position
- Decimal-only arithmetic

---

## 12.11 Dust Integration (Locked rules reused)

Dust is:

- stored per asset in `dust_ledger`
- accounting-only, not a tradable source
- reconciled against Binance balances (Binance is source of truth)
- not auto-traded in MVP

When the loop closes a position and a remainder is below tradable threshold:

- move remainder into `dust_ledger` immediately with `needs_reconcile=1`

---

## 12.12 Interaction With External Orders (Locked)

The loop must not interfere with orders not created by the loop.

Use order source tagging (see Appendix A) to avoid cancelling or managing:

- `order_source = external`

---

# Appendix A — Order Source Tagging (Implemented)

The cached `orders` table includes:

- `order_source = execution|manual|external`

During `sync open-orders`:

- if `order_id` exists in `executions.binance_order_id` → `execution`
- if `order_id` exists in `manual_orders.binance_order_id` → `manual`
- otherwise → `external`

Default rule:

- if source cannot be identified, treat as `external`

This prevents accidental interference with orders created outside CryptoGent.

---

# Appendix B — Reconciliation Commands (Implemented)

CryptoGent provides three reconcile loops:

- `cryptogent trade reconcile`  
  Reconciles execution orders (Phase 7). Prompts for auto-cancel of expired LIMIT orders (default: No).

- `cryptogent trade manual reconcile`  
  Reconciles manual orders tracked in `manual_orders`.

- `cryptogent trade reconcile-all`  
  Convenience wrapper that covers both, and can also reconcile cached open orders progressively.

All loops support:

- `--loop --interval-seconds N --duration-seconds M`
- Ctrl-B vs Ctrl-C:
  - Ctrl-B: force-stop the loop/session and apply the configured cleanup policy
  - Ctrl-C: stop the local runner only (session remains `running`)

---

# Appendix C — PnL Helper Commands (Implemented)

- `cryptogent pnl realized ...` reads realized PnL from SELL executions.
- `cryptogent pnl unrealized ...` computes unrealized PnL for open positions (live price).

---

# Appendix D — Dust Ledger Commands (Implemented)

- `cryptogent dust list`
- `cryptogent dust show <asset>`
