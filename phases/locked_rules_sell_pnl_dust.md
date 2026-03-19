````markdown
# Locked Rules — Sell Candidate Sizing, PnL, and Dust Management

This document locks the implementation rules for:

1. SELL candidate sizing
2. Unrealized and realized profit
3. Dust management

These rules are deterministic and must be followed during implementation.

---

# 1. SELL Candidate Sizing Rules

## 1.1 SELL is Position-Based

SELL always must be generated from a **position**, not from buy budget.

Required input base:

- `position_id`
- `close_mode`
- close value depending on mode

Allowed `close_mode` values:

- `amount`
- `percent`
- `all`

---

## 1.2 Close Mode Behavior

### `amount`

User provides a base-asset quantity to sell.

Example:

- position remaining = `2.000 SOL`
- close amount = `1.000 SOL`

Result:

- requested sell qty = `1.000 SOL`

### `percent`

User provides a percentage of the remaining position.

Example:

- position remaining = `2.000 SOL`
- close percent = `50`

Result:

- requested sell qty = `1.000 SOL`

### `all`

Sell the full remaining tradable position quantity.

---

## 1.3 Quantity Source of Truth

For SELL execution and safety checks, the **Binance balance is always the source of truth**.

The system must always display:

- position remaining quantity
- Binance free balance
- approved tradable quantity

If there is any mismatch, Binance free balance has priority.

### Hard Rule

Approved sell quantity must never exceed:

```text
min(position.remaining_qty, binance_free_base_balance)
```

---

## 1.4 Quantity Rounding Rules

SELL quantity must always be rounded **down** using symbol `stepSize`.

```text
sell_qty_rounded = round_down(requested_qty, stepSize)
```

After rounding, validate:

* `sell_qty_rounded > 0`
* `sell_qty_rounded >= minQty`
* `sell_qty_rounded * reference_price >= minNotional`

If any of these fail:

* hard stop
* do not create executable SELL candidate

### Reference Price Rule (for minNotional checks)

`reference_price` must be deterministic:

* for `LIMIT_SELL`: use the candidate `limit_price` (after tick-size rounding)
* for `MARKET_SELL`: use current live price at safety time (for gating only)

---

## 1.5 Hard Stops for SELL Candidate

SELL candidate must fail if:

* position not found
* position already closed
* requested quantity becomes `0` after rounding
* rounded quantity is below `minQty`
* rounded notional is below `minNotional`
* rounded quantity is greater than Binance free base balance
* symbol / environment mismatch exists
* required rules snapshot is missing

---

## 1.6 LIMIT_SELL Partial Fill Awareness

LIMIT_SELL must be fill-aware.

Rules:

* requested quantity is not treated as realized quantity
* position remaining is reduced only by **executed filled quantity**
* profit is computed only from **executed fills**
* open remainder stays open until filled / cancelled / expired

---

## 1.7 Dust Handling During SELL

If a SELL leaves a very small remainder below tradable threshold:

* position remainder must not be treated as tradable
* remainder may be moved into dust handling flow
* if remaining quantity after reconciliation is below tradable threshold, position may be closed as dust

---

# 2. Unrealized Profit and Realized Profit Rules

## 2.1 Realized Profit

Realized profit must be computed **only from executed SELL fills**.

Never compute realized profit from:

* requested sell quantity
* submitted order quantity
* open LIMIT_SELL quantity

### Realized PnL Rule

For MVP, use **Average Cost** as cost basis.

```text
realized_pnl_quote =
sell_proceeds_quote
- (executed_sell_qty × avg_entry_price)
- quote_fee_adjustment
```

Where:

* `sell_proceeds_quote` = total quote received from executed fills
* `executed_sell_qty` = actual filled base quantity
* `avg_entry_price` = position average cost
* `quote_fee_adjustment` = direct quote fee subtraction only

---

## 2.2 Fee Handling in Realized PnL

### If fee asset = quote asset

Subtract directly from realized profit.

### If fee asset = base asset

Reflect it through reduced net quantity / remaining quantity logic.

### If fee asset is neither base nor quote

Then:

* store fee separately
* attach warning
* do not silently convert into quote
* realized PnL in quote must be flagged as incomplete / excluding non-quote fee conversion

Required warning example:

```text
realized_pnl_excludes_non_quote_fee_conversion
```

---

## 2.3 Average Cost Basis

MVP cost basis is locked as:

```text
Average Cost
```

Not FIFO, not LIFO.

If multiple buys exist for the same position, the position must maintain:

* `avg_entry_price`
* `remaining_qty`

Realized PnL for SELL always uses that average entry price.

---

## 2.4 Unrealized Profit

Unrealized profit applies only to an **active position**.

It must never be mixed with realized PnL.

### Unrealized PnL Rule

```text
unrealized_pnl =
(current_price × net_position_qty)
- (avg_entry_price × net_position_qty)
```

### Important Rules

* use **Decimal**, never float
* use net position quantity
* current price must come from the monitoring price environment
* unrealized PnL belongs to monitoring / position evaluation, not SELL execution accounting

---

## 2.5 Net Position Quantity

If BUY fee is charged in base asset:

```text
net_position_qty = executed_buy_qty - base_fee
```

This net quantity is the usable position quantity for:

* monitoring
* SELL sizing
* unrealized PnL

---

# 3. Dust Management Rules

## 3.1 Dust Purpose

Dust management exists for:

* preserving leftover quantity accounting
* preserving cost basis for future realized PnL
* avoiding silent loss of tiny leftover balances

Dust management does **not** define tradable balance by itself.

Tradable balance always comes from Binance.

---

## 3.2 Dust Ledger Scope

Dust must be tracked **per asset** and only as **unpositioned dust**.

It must not include:

* active open position quantity
* synthetic quantity not confirmed by exchange balance

---

## 3.3 Dust Storage

Dust must be stored in a dedicated table:

```text
dust_ledger
```

Recommended required fields:

* `dust_id`
* `asset`
* `dust_qty`
* `avg_cost_price`
* `needs_reconcile`
* `created_at_utc`
* `updated_at_utc`

Do not store dust ledger inside `system_state` JSON.

---

## 3.4 Dust Creation Rule

Dust must be written to the ledger only when a position is closed as dust.

When a position is closed with leftover dust:

* move leftover into `dust_ledger` immediately
* preserve cost basis immediately
* mark row as needing reconciliation

Reason:

* accounting must not lose dust context on crash / restart

---

## 3.5 Dust Average Cost Rule

Dust cost basis must use **weighted average**, not simple average.

If previous dust exists:

```text
new_total_qty = prev_dust_qty + new_dust_qty

new_avg_cost =
(
  prev_dust_qty × prev_avg_cost
  +
  new_dust_qty × new_dust_cost
)
/
new_total_qty
```

If no previous dust exists:

```text
dust_qty = new_dust_qty
avg_cost_price = new_dust_cost
```

Usually:

```text
new_dust_cost = position.avg_entry_price
```

---

## 3.6 Dust vs Binance Balance Rule

The **Binance balance is always the source of truth**.

Dust ledger is only an accounting helper.

If mismatch occurs:

```text
max_unpositioned_free = max(0, binance_free_balance_for_asset - open_position_qty_total_for_asset)
effective_dust = min(dust_ledger_qty, max_unpositioned_free)
```

If:

* `dust_ledger_qty > binance_balance`

then ledger must be reconciled / clamped.

Example:

* `dust_ledger_SOL = 0.02`
* `binance_SOL_balance = 0`

Result:

* effective dust for accounting/tradable consideration = `0`
* ledger must be adjusted and warning logged

---

## 3.7 Dust Is Not Auto-Traded

Dust must not be auto-traded in MVP.

If dust accumulates and becomes tradable:

* report it
* warn if threshold exceeded
* allow manual sweep later

Recommended MVP behavior:

* ignore + report + threshold warning + manual sweep

---

## 3.8 Dust and Realized Profit

Dust ledger exists partly to preserve cost basis for future accounting.

If dust is later sold through a proper flow, realized PnL can use:

* dust quantity
* dust average cost

But dust ledger alone must not create synthetic tradable quantity.

---

# 4. Final Locked Summary

## SELL Candidate Sizing

* SELL is position-based
* `close_mode` supports `amount`, `percent`, `all`
* Binance balance is the source of truth for tradable SELL quantity
* quantity must be rounded down by `stepSize`
* validate `minQty`, `minNotional`, and free balance
* LIMIT_SELL must be partial-fill aware

## Profit Rules

* MVP cost basis = Average Cost
* realized PnL only from executed SELL fills
* unrealized PnL only for active positions
* use Decimal, never float
* non-base / non-quote fees must be stored separately and warned

## Dust Management

* dust tracked per asset in `dust_ledger`
* dust is accounting-only, not a tradable source
* Binance balance is always the source of truth
* dust leftover moves to ledger immediately with reconciliation flag
* dust average cost uses weighted average
* dust is not auto-traded in MVP

```
```
````
