````markdown
# Phase 7 – Execution

This phase introduces the **actual trade execution pipeline** of CryptoGent.

By this stage, the system should already be able to:

- collect and validate structured trade input
- retrieve synchronized account state
- generate a trade plan
- validate the trade against Binance Spot rules
- approve or reject the trade through risk management
- produce an execution-ready candidate

Phase 7 is the first phase where CryptoGent is allowed to interact with the exchange for actual order placement.

This phase must remain conservative and execution-focused.

---

# Phase Scope

This phase implements the following steps from the implementation roadmap:

27. order execution  
28. position management  
29. persistence updates  

---

# Core Objective

After completing Phase 7, CryptoGent should be able to:

- place a Spot order on Binance
- capture the exchange response
- persist execution results
- create and update position state
- handle successful, failed, and partial execution outcomes
- keep local state consistent after execution

---

# Layers Covered in This Phase

This phase activates the following layers:

15. Order Execution Layer  
16. Position Management Layer  

Supporting layers involved:

4. Exchange Connection Layer  
5. Account State Synchronization Layer  
6. Local State, Persistence, and Recovery Layer  
13. Deterministic Validation Layer  
14. Risk Management Layer  
20. Audit, Logging, and Reporting Layer  

---

# Execution Philosophy

CryptoGent must not execute any order unless:

- deterministic validation passed
- risk management approved
- an execution candidate exists
- exchange connectivity is available
- the user confirmed execution if confirmation is required by policy

Execution should remain simple in MVP.

Recommended first execution type:

```text
market order
````

Support for limit orders can be introduced later.

---

# Order Execution Layer

The Order Execution Layer is responsible for sending approved orders to Binance Spot.

It must receive only **execution-ready candidates**.

It must never independently decide what to buy or sell.

---

# Execution Inputs

Inputs include:

* execution candidate
* symbol
* side
* approved quantity
* execution type
* environment mode
* runtime config

For MVP, the main execution side will usually be:

```text
BUY
```

because the user is typically starting a fresh trade.

---

# Supported Execution Types

MVP should support at least:

## Market Buy

Used for entering a position immediately.

## Market Sell

Used for closing a position immediately.

Optional but not required yet:

* limit buy
* limit sell
* stop-limit

Keep the first implementation minimal and reliable.

---

# Exchange Endpoint

For Spot execution, use:

```text
POST /api/v3/order
```

The order request must be signed.

Required fields depend on order type.

For market buy / sell, typical fields include:

```text
symbol
side
type
quantity
timestamp
signature
```

If quote-order-based market buys are later preferred, that can be added separately.

---

# Execution Request Construction

The execution layer must:

* receive a validated candidate
* build the correct Binance request
* sign the request
* submit to the exchange
* parse the response
* return a normalized execution result

It must not bypass the exchange client abstraction.

---

# Execution Result Handling

The layer must handle the following result types.

## Success

Order accepted by Binance.

Possible outcomes:

* fully filled immediately
* partially filled
* accepted but still open (less likely for market orders, but structure should support it)

## Failure

Order rejected due to:

* insufficient balance
* invalid quantity
* symbol issue
* exchange error
* network failure

---

# Normalized Execution Result

Suggested output structure:

```text
execution_status
exchange_order_id
symbol
side
order_type
requested_quantity
executed_quantity
average_fill_price
raw_status
message
executed_at
```

Possible statuses:

```text
submitted
filled
partially_filled
rejected
failed
```

Example:

```text
execution_status: filled
exchange_order_id: 123456789
symbol: SOLUSDT
side: BUY
order_type: MARKET
requested_quantity: 1.742
executed_quantity: 1.742
average_fill_price: 103.28
raw_status: FILLED
message: Order executed successfully
```

---

# Partial Fill Handling

Even if the first MVP mostly expects simple market fills, the system must still be able to handle partial fills safely.

If partially filled:

* store the actual executed quantity
* create position based only on executed quantity
* mark order as partially filled
* keep reconciliation-ready state

Do not assume requested quantity equals executed quantity.

---

# Position Management Layer

After a successful order result, the system must create or update a local position.

The Position Management Layer is responsible for:

* creating a new active position after entry
* updating an existing position after additional fills if allowed
* calculating entry metrics
* tracking current position status
* marking positions as open or closed

For MVP, one active position at a time is strongly recommended.

---

# Position Inputs

Inputs include:

* execution result
* trade request
* execution candidate
* target profit
* stop-loss
* deadline
* selected symbol

---

# Position Creation

If a buy order succeeds, create a new active position.

Suggested position fields:

```text
position_id
symbol
side
entry_price
quantity
target_profit_percent
stop_loss_percent
deadline
status
opened_at
updated_at
```

Suggested status values:

```text
open
closing
closed
failed
```

---

# Entry Price Calculation

For a filled order, the initial entry price should be based on the actual execution result.

Prefer:

* average fill price from the exchange response

Do not rely on pre-trade ticker price once execution is complete.

---

# Position Metrics

The position layer should calculate and expose at least:

* entry price
* quantity
* gross notional value
* target profit level
* stop-loss level
* deadline timestamp

These values will be used by the monitoring phase.

---

# Persistence Updates

This phase must persist all critical execution outcomes immediately.

Data that must be persisted includes:

* exchange order result
* updated order state
* active position state
* execution timestamps
* execution summary logs

Persistence should happen as soon as execution completes or fails.

---

# Order Persistence

The `orders` table should be updated with:

* exchange order ID
* symbol
* side
* type
* requested quantity
* executed quantity
* price or average fill price
* status
* timestamps

If the order already exists in a draft or candidate-linked form, it should be updated rather than duplicated unnecessarily.

---

# Position Persistence

The `positions` table should be updated with:

* active position details
* status
* quantity
* entry price
* target and stop-loss
* deadline

For buy execution:

* create open position

For sell execution:

* close active position if matched

---

# Local State Consistency

After execution, local state must reflect the result as closely as possible.

However, the exchange still remains the source of truth.

Recommended immediate follow-up behavior:

* persist execution result
* trigger immediate account synchronization
* update local balances
* update local orders

This ensures state drift is minimized right after execution.

---

# Immediate Post-Execution Sync

After a successful order attempt, the system should trigger an immediate synchronization flow.

Recommended sequence:

```text
Execution completed
   ↓
Persist execution result
   ↓
Trigger immediate account sync
   ↓
Refresh balances
   ↓
Refresh orders
   ↓
Update position state if needed
```

This keeps local state aligned with exchange reality.

---

# CLI Behavior

After execution, the CLI should display a clear result summary.

Example success output:

```text
Execution Result
- Status: FILLED
- Symbol: SOLUSDT
- Side: BUY
- Quantity: 1.742
- Average Fill Price: 103.28
- Position Status: OPEN
```

Example failure output:

```text
Execution Result
- Status: FAILED
- Symbol: SOLUSDT
- Reason: Insufficient balance at execution time
```

If confirmation is required by policy, it must have occurred before execution.

---

# Error Handling

The phase must handle:

* exchange rejection
* insufficient balance at execution time
* partial fill
* network timeout
* signed request failure
* persistence write failure
* post-execution sync failure

Errors must be:

* logged
* persisted if relevant
* surfaced to CLI clearly
* handled without corrupting state

If execution result is uncertain due to network failure, the system must not assume success or failure blindly.
It should mark the order state as uncertain and rely on synchronization to reconcile it.

---

# Uncertain Execution State

A special caution is required for cases like:

* request timeout after submit
* network disconnect during order placement
* exchange response not received

In these cases, the order may have been accepted by Binance even though the client did not receive confirmation.

Recommended behavior:

* mark execution result as uncertain
* log critical warning
* trigger immediate sync
* inspect orders and balances before continuing

This is important to avoid duplicate trades.

---

# Logging Requirements

Log all important execution events.

Minimum logs:

* execution started
* request submitted
* execution succeeded
* execution failed
* partial fill detected
* position created
* immediate sync triggered

Example logs:

```text
[INFO] Execution: Submitting MARKET BUY for SOLUSDT
[INFO] Execution: Order 123456789 filled
[INFO] PositionManager: Open position created for SOLUSDT
[INFO] Sync: Immediate post-execution synchronization triggered
```

---

# Suggested Modules

Suggested files for this phase:

```text
execution/
  executor.py
  order_builder.py
  result_parser.py

position/
  manager.py
  metrics.py
  lifecycle.py

models/
  execution_result.py
  position_model.py
```

Possible responsibilities:

## `executor.py`

* orchestrates order submission

## `order_builder.py`

* builds Binance order payloads

## `result_parser.py`

* normalizes exchange execution responses

## `manager.py`

* creates and updates positions

## `metrics.py`

* calculates position-level metrics

## `lifecycle.py`

* manages open/close state transitions

---

# Deliverables

Phase 7 is complete when:

* an approved execution candidate can be sent to Binance
* the exchange response can be parsed and normalized
* orders are persisted correctly
* positions are created or updated correctly
* immediate post-execution synchronization is triggered
* success, failure, and uncertain states are handled safely

---

# Success Criteria

Phase 7 is successful when the system can:

* execute a Spot order from an approved candidate
* persist the resulting order state
* create a valid active position from the real fill result
* keep local state consistent after execution
* avoid unsafe assumptions in timeout or uncertain execution scenarios

---

# Phase 7.X — SELL Execution Extensions (Partial Close + Realized PnL + Dust)

> This section extends Phase 7 with locked execution behavior for **MARKET_SELL** and **LIMIT_SELL**.  
> It does **not modify previous Phase 7 text**.

## 7.X.1 SELL Is Position-Based

SELL execution must operate on a known position lifecycle object.

Rule:

* a SELL execution must reference a position (directly or via a candidate that references a position)
* SELL must not be “sell from balance” by default in MVP

## 7.X.2 Partial Close Behavior

SELL execution must support partial closes.

Rule:

* `executed_sell_qty` is determined only by the exchange fill result
* position remaining quantity must decrement by executed filled quantity
* position must be marked closed only when remaining is zero or dust (see dust policy below)

## 7.X.2a SELL Reservations (Locked Quantity)

When a SELL execution is submitted and remains open, its remaining quantity must be treated as **reserved** against the position.

Rule:

* open SELL executions reserve quantity
* reserved quantity reduces available-to-sell for new candidates
* `positions.locked_qty` is a snapshot for visibility and should be recomputed during reconciliation
* reserved quantities must never be negative (clamp at zero)
* execution must re-check available-to-sell (pos qty, free balance, reserved) before submitting a SELL order

## 7.X.3 LIMIT_SELL Partial Fill Awareness

LIMIT_SELL orders may remain open and fill incrementally.

Required behavior:

* persist execution state transitions (`open`, `partially_filled`, `filled`)
* reconciliation must update the execution row from `GET /api/v3/order` (by `origClientOrderId`)
* position remaining quantity must update only from executed filled quantity

## 7.X.4 Realized PnL (Executed SELL Fills Only)

### MVP Cost Basis

MVP cost basis must use **Average Cost**.

### Realized PnL Timing

Realized PnL must be computed only from **executed SELL fills**, never from:

* requested quantity
* unfilled limit orders
* planning snapshots

### Required PnL Inputs

At minimum:

* `avg_entry_price` (position)
* `executed_sell_qty` (from fills)
* `total_quote_received` (from fills)
* fee breakdown (from fills)

### Suggested Formula (Quote Currency)

```text
proceeds_quote = total_quote_received
cost_quote = executed_sell_qty × avg_entry_price
realized_pnl_quote = proceeds_quote − cost_quote − quote_fees
```

Fee handling:

* if fee asset is quote (e.g., USDT): subtract directly
* if fee asset is base: reflect via net quantities (position qty is net-of-fee)
* if fee asset is neither base nor quote: record warning + store separately (do not silently convert in MVP)

## 7.X.5 Fee Asset Is Exchange-Determined

Execution must treat `fills[].commissionAsset` as the source of truth.

Rule:

* do not assume fee asset is base
* do not assume fee asset is quote
* do not assume BNB-fee mode is available on all environments (e.g., Spot Testnet may not expose SAPI toggles)

Persist commission breakdown for auditability.

## 7.X.6 Dust Policy (Post-SELL Remainders)

After a SELL fill, position remainder may be below `minQty` due to:

* stepSize rounding
* base-asset fees

Rule:

* if remaining quantity is `0` → close position
* if remaining quantity is below `minQty` → treat as dust and close position as dust

Dust must not block subsequent trades indefinitely.

## 7.X.7 Dust Ledger Integration (Accounting Only)

When a position is closed due to dust, the leftover dust quantity must be tracked for future realized PnL accounting.

### New Table (Recommended)

```text
dust_ledger
-----------
dust_id
asset
dust_qty
avg_cost_price
needs_reconcile
created_at_utc
updated_at_utc
```

### What the Dust Ledger Represents

* dust ledger tracks **unpositioned free dust only**
* it is not the source of truth for tradable funds
* Binance balances remain authoritative for tradable quantity

### Deterministic Write (Immediate)

On dust-close:

* write dust quantity into `dust_ledger` immediately
* set `needs_reconcile = 1`
* compute `avg_cost_price` using weighted average cost

### Reconciliation Rule (Next Balance Sync)

Dust ledger must be reconciled against Binance balances on the next successful balance sync.

Key clamp rule to prevent double-counting:

```text
max_unpositioned_free = max(0, binance_free_balance - open_position_qty_total)
effective_dust = min(dust_ledger_qty, max_unpositioned_free)
dust_ledger_qty = effective_dust
```

If a clamp occurs:

* log a warning
* keep `avg_cost_price` unchanged

### Dust Trading

Dust is not automatically traded.

If accumulated dust becomes tradable (>= `minQty` and `minNotional`), the system may:

* warn the user
* provide a manual sweep command (later phase)

## Reference Document

This phase must follow:

* `phases/locked_rules_sell_pnl_dust.md`

# Phase 7.X — Execution Refinements (MVP Critical Additions)

> This section **extends Phase 7** and adds execution-critical rules required for safe MVP implementation.  
> It **does not modify previous Phase 7 text**, only adds deterministic constraints and behavior.

---

## 7.X Execution Preconditions (Gating)

Phase 7 execution must only run when a valid **execution candidate** exists.

Execution must be allowed only if Phase 6 returned:

- `safe`
- `safe_with_warning`

Execution must **not proceed** if the safety result is:

- `unsafe`
- `expired`
- `not_feasible`

### CLI Confirmation Policy

Execution should require explicit confirmation.

Example:

```bash
cryptogent trade execute <plan_id>
```

Optional confirmation flag:

```bash
cryptogent trade execute <plan_id> --yes
```

If confirmation is not provided, the CLI must ask for confirmation interactively.

---

## 7.X Execution Environment Consistency

Execution must strictly follow the environment defined in the trade plan.

The plan must contain:

```text
execution_environment = mainnet | testnet
```

Execution must:

- use the correct Binance endpoint
- log the execution environment
- never mix planning market-data environment with execution environment

Example rule:

If:

```text
market_data_environment = mainnet_public
execution_environment = testnet
```

Execution must still submit orders to **testnet**, not mainnet.

### Runtime Environment Source (MVP Rule)

For MVP, **use `binance.testnet = true/false` from config** to select the execution endpoint.

Deterministic constraint:

- if config says `testnet=true` then execution endpoint must be testnet
- if config says `testnet=false` then execution endpoint must be mainnet
- if the plan’s `execution_environment` does not match the config at runtime → **hard stop**

This prevents accidental mainnet execution when the system is configured for testnet (or vice versa).

---

## 7.X Idempotency and Retry Safety

Every submitted order must include a **`newClientOrderId`**.

This value must:

- be unique per order submission
- be persisted in the database
- be used for reconciliation in case of retry

### Required Fields to Persist

```text
client_order_id
plan_id
symbol
side
timestamp
```

### Retry Behavior

If a request times out or returns an unknown result:

1. Do **not immediately retry submission**
2. Attempt reconciliation using:

```text
origClientOrderId = client_order_id
```

3. Query the exchange to determine whether the order exists

If the order exists:

- treat it as submitted
- continue order synchronization

If the order does not exist:

- retry submission with the **same client_order_id**

This prevents duplicate orders.

---

## 7.X Market BUY Sizing Rule

For **BUY MARKET orders**, the system must use:

```text
quoteOrderQty
```

Example:

```text
quoteOrderQty = approved_budget
```

This ensures that the system spends the exact approved budget in quote currency (e.g. USDT).

### Reason

Using `quantity` for BUY orders can produce errors because:

- price may move
- rounding errors may violate min-notional
- final cost may exceed budget

Using `quoteOrderQty` avoids these issues.

### SELL Orders

SELL MARKET orders must use:

```text
quantity
```

because the base asset amount is already known.

---

## 7.X Order Submission Structure

Example structure for MARKET BUY:

```text
symbol = BTCUSDT
side = BUY
type = MARKET
quoteOrderQty = approved_budget
newClientOrderId = generated_id
```

Example structure for MARKET SELL:

```text
symbol = BTCUSDT
side = SELL
type = MARKET
quantity = base_asset_quantity
newClientOrderId = generated_id
```

---

## 7.X Fill Parsing Rules

When the exchange returns a filled order, the system must parse the **`fills[]`** array.

Do **not rely on `price` field** for MARKET orders.

### Required Calculations

From `fills[]` compute:

```text
executed_quantity
avg_fill_price
commission
commission_asset
```

### Average Price Formula

```text
avg_fill_price =
sum(fill_price × fill_qty) / sum(fill_qty)
```

### Persisted Fields

```text
executed_quantity
avg_fill_price
total_quote_spent
commission_total
commission_asset
fills_count
```

---

## 7.X Uncertain State Model

Execution must support a first-class **uncertain submission state**.

Example status:

```text
uncertain_submitted
```

This state occurs when:

- network timeout occurs
- exchange response not received
- submission result cannot be confirmed

### Required Behavior

When `uncertain_submitted` occurs:

1. Persist the state
2. Prevent further execution attempts
3. Run reconciliation before any retry

---

## 7.X Reconciliation Procedure

When execution state is uncertain:

```text
sync + find order by clientOrderId
```

Steps:

```text
1. query exchange for order using clientOrderId
2. if found → update local order state
3. if not found → retry submission safely
```

Reconciliation must occur **before allowing further execution actions**.

---

## 7.X Execution State Machine

Recommended execution states:

| State                 | Meaning                      |
| --------------------- | ---------------------------- |
| `execution_candidate` | plan ready for execution     |
| `submitting`          | order submission in progress |
| `submitted`           | exchange confirmed order     |
| `uncertain_submitted` | submission result unknown    |
| `filled`              | order completed              |
| `partially_filled`    | order partially filled       |
| `cancelled`           | order cancelled              |
| `failed`              | submission failed            |

---

## 7.X Logging Requirements

Execution logs must include:

```text
plan_id
client_order_id
symbol
side
order_type
execution_environment
approved_budget
timestamp
```

### Do Not Log

Avoid logging full raw exchange payloads.

Log summarized values only:

```text
fills_count
avg_fill_price
executed_quantity
commission_total
```

---

## 7.X Execution Safety Summary

Phase 7 execution must enforce the following rules:

1. execution only allowed when safety validation passes
2. execution must follow plan's execution environment (and config `testnet` flag)
3. all orders must use `newClientOrderId`
4. BUY MARKET orders must use `quoteOrderQty`
5. SELL MARKET orders must use `quantity`
6. fills must be parsed from `fills[]`
7. uncertain submissions must enter reconciliation flow
8. retries must use the same client order id

---

## 7.X Architectural Principle

Execution must remain **deterministic and auditable**.

The plan approved by Phase 6 must be executed **exactly as approved**, without regeneration of planning logic.

Duplicate execution protection:

* a candidate may be executed **once**; any existing execution row (open or closed) blocks re-execution

---

# Phase 7.Y — Implementation Decisions Lock-In

This section locks the remaining implementation decisions required before Phase 7 execution work starts.

---

## 7.Y Decision 1 — Command Input

### Final Decision

Phase 7 should execute using:

```bash
cryptogent trade execute <candidate_id>
```

Optional confirmation flag:

```bash
cryptogent trade execute <candidate_id> --yes
```

### Reason

Phase 6 produces the execution candidate, so execution should consume the exact object that has already passed safety validation.

If Phase 7 takes `plan_id` instead:

- it must look up a candidate indirectly
- ambiguity can happen if multiple candidates exist for one plan
- auditability becomes weaker
- execution may accidentally use the wrong safety result

### Rule

- `plan_id` remains the planning object
- `candidate_id` becomes the execution input
- Phase 7 must execute the exact approved execution candidate
- Phase 7 must not auto-select “latest safe candidate” from a plan unless explicitly requested as a separate helper command

---

## 7.Y Decision 2 — MVP Scope

### Final Decision

Phase 7 MVP should implement:

- **BUY market only**

Not included in MVP:

- MARKET SELL
- partial close
- full close
- reduce-only style logic
- position netting behavior

### Reason

BUY market only keeps MVP smaller and safer because:

- Phase 5 and Phase 6 already center around approved budget
- `quoteOrderQty` for BUY maps cleanly to approved quote budget
- SELL introduces additional questions (position tracking, available base balance, partial close logic, and fee-adjusted sellable quantity)

---

## 7.Y Decision 3 — Persistence Shape

### Final Decision

Create a **new `executions` table**.

Do **not** overload the existing `orders` table for Phase 7 execution tracking.

### Reason

Execution is a separate lifecycle object from planning and from exchange order snapshots.

Using a dedicated table keeps clean separation between:

- trade request
- trade plan
- execution candidate
- execution attempt
- exchange order state / reconciliation

### Recommended Core Columns

```text
executions

execution_id
candidate_id
plan_id
trade_request_id
symbol
side
order_type
execution_environment
client_order_id
binance_order_id
quote_order_qty
requested_quantity
executed_quantity
avg_fill_price
total_quote_spent
commission_total
commission_asset
fills_count
local_status
raw_status
submitted_at
reconciled_at
created_at
updated_at
```

### Status Model

`local_status` should support:

- `execution_candidate`
- `submitting`
- `submitted`
- `uncertain_submitted`
- `filled`
- `partially_filled`
- `failed`
- `rejected`

---

## 7.Y Decision 4 — Reconciliation Endpoint

### Final Decision

Add Binance:

```text
GET /api/v3/order
```

using:

```text
origClientOrderId
```

for idempotency reconciliation.

### Reason

This is the safest and most deterministic reconciliation path.

Relying only on `openOrders` and account sync is weaker because filled market orders may disappear from open orders immediately, and balance-delta matching is less reliable after timeouts.

### Rule

On timeout / unknown submission result:

1. mark execution as `uncertain_submitted`
2. do not submit again immediately
3. call `GET /api/v3/order` using `origClientOrderId`
4. if found: update execution state from exchange result and continue normal sync
5. if not found: allow safe retry using the same `client_order_id`

---

## 7.Y Final Locked Decisions Summary

| Area | Decision |
| --- | --- |
| Execution input | `cryptogent trade execute <candidate_id>` |
| MVP scope | BUY market only |
| Persistence | new `executions` table |
| Reconciliation | `GET /api/v3/order` by `origClientOrderId` |

---

## 7.Y Implementation Principle

Phase flow:

| Phase | Output |
| --- | --- |
| Phase 5 | `trade_plan` |
| Phase 6 | `execution_candidate` |
| Phase 7 | `execution` |

This keeps the system deterministic, auditable, and easy to reason about.

---

# Phase 7.Z — Final Implementation Lock-Ins (Phase 7 Execution)

This section confirms the remaining execution decisions required to implement Phase 7 without ambiguity.

---

## 7.Z Execution Input and Lookup

### Final Decision

Phase 7 command is:

```bash
cryptogent trade execute <candidate_id>
```

### Lookup Rule

Phase 7 must load **exactly that `execution_candidates` row**.

It must **not**:

- auto-select the latest candidate
- auto-select the most recent safe candidate for a plan
- fall back to another candidate
- infer a candidate from `plan_id`

Execution is always **candidate-first**.

---

## 7.Z Candidate Gating (Hard Stops)

### Final Decision

Phase 7 may execute only if all of the following are true:

- `execution_ready = 1`
- `risk_status IN ('approved', 'approved_with_warning')`
- `validation_status = 'passed'`

These are mandatory execution gates.

### Failure Behavior

If any gating condition fails:

- do **not** submit to Binance
- do **not** create an execution attempt row
- print the rejection reason
- exit with non-zero status

Reason: a failed gate means execution was never legitimately attempted, and persisting an `executions` row would mix eligibility failures with actual execution attempts.

---

## 7.Z Budget Used for BUY MARKET

### Final Decision

For BUY MARKET execution, use:

```text
quoteOrderQty = execution_candidates.approved_budget_amount
approved_budget_asset = execution_candidates.approved_budget_asset
```

### Required Constraint

`approved_budget_asset` must match the symbol quote asset.

Example allowed:

```text
symbol = BTCUSDT
approved_budget_asset = USDT
```

Example invalid (hard stop):

```text
symbol = BTCUSDT
approved_budget_asset = BUSD
```

---

## 7.Z Execution Environment Source

### Final Decision

Execution environment values in the database must use:

```text
mainnet | testnet
```

### Source of Truth

Execution must use the configured runtime environment:

```text
binance.testnet = true | false
```

Derived runtime environment:

- `true` → `testnet`
- `false` → `mainnet`

### Validation Rule

The runtime execution environment must match the environment stored in the candidate / plan.

If they do not match:

- hard stop
- do not submit order
- print environment mismatch
- exit non-zero

---

## 7.Z Idempotency Key Format

### Final Decision

This format is acceptable:

```text
cg_<candidate_id>_<utc_ts>_<rand>
```

Example:

```text
cg_42_20260315T112530Z_a7k2
```

### Retry Rule

The **same `client_order_id` must be reused on retries** for the same execution attempt.

It must not be regenerated after a timeout.

---

## 7.Z Reconciliation Behavior

### Final Decision

If submission times out or returns an unknown result:

1. mark execution state as:

```text
uncertain_submitted
```

2. run:

```text
GET /api/v3/order
```

using:

```text
origClientOrderId = client_order_id
```

3. reconcile before any retry

### MVP Retry Limit

Maximum automatic retry count:

```text
1
```

### After Retry Limit

If reconciliation still cannot prove final state after the allowed retry behavior:

- remain in `uncertain_submitted`
- require manual reconcile / sync flow
- do not auto-submit again

This avoids duplicate market buys.

---

## 7.Z Persistence Schema Details

### Final Decision

Table name:

```text
executions
```

Primary key:

```text
execution_id INTEGER PRIMARY KEY
```

Datetime storage: UTC ISO strings, recommended naming:

- `created_at_utc`
- `updated_at_utc`
- `submitted_at_utc`
- `reconciled_at_utc`

Example:

```text
2026-03-15T11:25:30Z
```

### Minimum MVP Columns

```text
execution_id
candidate_id
plan_id
trade_request_id
symbol
side
order_type
execution_environment
client_order_id
binance_order_id
quote_order_qty
requested_quantity
executed_quantity
avg_fill_price
total_quote_spent
commission_total
commission_asset
fills_count
local_status
raw_status
retry_count
submitted_at_utc
reconciled_at_utc
created_at_utc
updated_at_utc
```

---

## 7.Z Hard-Stop Summary

Execution must stop immediately if any of the following are true:

- candidate row not found
- `execution_ready != 1`
- `risk_status` not in approved states
- `validation_status != 'passed'`
- approved budget missing
- approved budget asset missing
- approved budget asset does not match symbol quote asset
- environment mismatch
- client order id missing during retry path

In all such cases:

- no Binance submit
- no execution row creation if submit never started
- exit non-zero

---

## 7.Z Implementation Principle

Phase 7 must execute only the **explicitly approved execution candidate**, using the **approved quote budget**, in the **approved execution environment**, with **idempotent submission and reconciliation-first retry behavior**.

---

# Phase 7.X — LIMIT BUY Execution Extension

> This section **extends Phase 7** to support **LIMIT BUY orders**.  
> It does **not modify existing Phase 7 MVP rules** (which implement MARKET BUY).  
> LIMIT BUY is introduced as an **additional execution mode** that can be enabled after the MARKET BUY path is stable.

---

## 7.X.1 Scope

LIMIT BUY execution allows the system to place a **price-constrained buy order** instead of an immediate MARKET BUY.

This is useful when:

- price control is required
- slippage must be limited
- the strategy expects a pullback entry
- liquidity conditions favor resting orders

LIMIT BUY must still follow all previously defined rules for:

- execution candidate gating
- idempotency
- environment consistency
- audit logging
- reconciliation

---

## 7.X.2 Execution Preconditions

LIMIT BUY execution may proceed only when:

- `execution_ready = 1`
- `risk_status IN ('approved', 'approved_with_warning')`
- `validation_status = 'passed'`
- `order_type = 'LIMIT_BUY'`

If any condition fails:

- do not submit to Binance
- print reason
- exit non-zero

---

## 7.X.3 Order Structure

LIMIT BUY must use the Binance parameters:

```text
symbol
side = BUY
type = LIMIT
timeInForce = GTC
price
quantity
newClientOrderId
```

Example:

```text
symbol = BTCUSDT
side = BUY
type = LIMIT
timeInForce = GTC
price = 42000.10
quantity = 0.0023
newClientOrderId = cg_42_20260315T112530Z_ab12
```

---

## 7.X.4 Limit Price Source

The limit price must come from the **execution candidate**.

Candidate fields:

```text
order_type
limit_price
approved_budget_amount
approved_budget_asset
```

Execution must **not recompute price**.

The stored candidate price is the single source of truth.

---

## 7.X.5 Tick Size Enforcement

LIMIT price must comply with exchange tick size.

Procedure:

```text
price = round_down_to_tick_size(limit_price)
```

If rounding produces a price that invalidates the order (e.g., notional falls below minimum), execution must hard-stop.

---

## 7.X.6 Quantity Calculation

LIMIT BUY uses **base quantity**, not `quoteOrderQty`.

Steps:

```text
raw_quantity = approved_budget_amount / limit_price
rounded_quantity = round_down_to_step_size(raw_quantity)
```

Then verify:

- `rounded_quantity >= minQty`
- `rounded_quantity * limit_price >= minNotional`

If any rule fails, execution must stop.

---

## 7.X.7 Time-In-Force

For MVP LIMIT BUY:

```text
timeInForce = GTC
```

Future versions may support IOC/FOK, but MVP must use **GTC only**.

---

## 7.X.8 Order Lifecycle

Unlike MARKET orders, LIMIT orders may remain open.

Execution states must support:

| State | Meaning |
| --- | --- |
| `submitted` | order accepted by exchange |
| `open` | order resting on book |
| `partially_filled` | partial execution occurred |
| `filled` | order fully executed |
| `cancelled` | order cancelled |
| `expired` | order expired |

---

## 7.X.9 Partial Fill Handling

LIMIT orders may fill incrementally.

Execution tracking must support:

- accumulating executed quantity
- updating average fill price
- tracking commission

Fields updated from fills:

```text
executed_quantity
avg_fill_price
commission_total
commission_asset
fills_count
```

---

## 7.X.10 Reconciliation

If submission status becomes uncertain:

```text
local_status = uncertain_submitted
```

Reconciliation procedure:

```text
GET /api/v3/order
origClientOrderId = client_order_id
```

If found: update order status.

If not found: retry submission once.

Retry limit:

```text
max_retries = 1
```

---

## 7.X.11 Limit Order Expiration Policy

LIMIT orders should not remain open indefinitely.

Recommended default:

```text
limit_order_timeout = 30 minutes
```

When timeout is reached (MVP default behavior):

- mark the local execution state as `expired`
- persist `expired_at_utc`
- do **not** auto-cancel on Binance by default

---

## 7.X.12 Logging Requirements

LIMIT BUY execution logs must include:

```text
plan_id
candidate_id
client_order_id
symbol
side
price
quantity
execution_environment
submitted_at
```

Avoid logging full raw exchange payloads.

Log summarized fill data only.

---

## 7.X.13 Execution Summary

LIMIT BUY execution must follow these rules:

1. only execute approved execution candidates
2. price must come from candidate
3. price must respect tick size
4. quantity must respect lot size
5. order must use `timeInForce = GTC`
6. idempotent submission required
7. reconciliation must use `origClientOrderId`
8. partial fills must update execution state
9. open orders must support expiration handling

---

## 7.X.14 Architectural Principle

LIMIT BUY extends the execution phase without changing the planning model.

Planning still occurs in:

- Phase 5 — plan creation
- Phase 6 — safety validation

Execution behavior is simply extended to support **price-constrained entry orders**.

This keeps the architecture deterministic and auditable.

---

# Phase 7.X — LIMIT BUY Final Lock-Ins

This section locks the remaining decisions required to implement LIMIT BUY without ambiguity.

---

## 7.X.15 Candidate Model Change

Phase 6 must persist the following fields into `execution_candidates` for LIMIT BUY support:

- `order_type`
- `limit_price`

Phase 7 must **not** accept `--limit-price` at runtime for normal execution flow.

Hard stop: if a LIMIT BUY candidate is missing either `order_type` or `limit_price`, execution must hard-stop and exit non-zero.

Allowed order type values (MVP):

- `MARKET_BUY`
- `LIMIT_BUY`

---

## 7.X.16 Budget Asset Rule

LIMIT BUY keeps the same hard-stop rule:

```text
approved_budget_asset must equal the symbol quote asset
```

If the approved budget asset does not match the symbol quote asset:

- do not submit the order
- do not create an execution attempt if submit never started
- print clear mismatch error
- exit non-zero

---

## 7.X.17 Expiration Behavior

At:

```text
limit_order_timeout = 30 minutes
```

MVP behavior is:

- only mark the execution as `expired` locally
- do **not** auto-cancel on Binance by default

---

## 7.X.18 No Auto-Cancel Unless Enabled

LIMIT BUY orders must not be auto-cancelled on the exchange unless an explicit cancellation policy is enabled.

Recommended future flag:

```text
auto_cancel_expired_limit_orders = true | false
```

Default for MVP:

```text
auto_cancel_expired_limit_orders = false
```

If disabled:

- local state may become `expired`
- exchange order may still remain open
- reconciliation / manual cancel flow must handle the rest
- on cancel, refresh cached open orders and recompute position `locked_qty` (reservation snapshot)
- `trade reconcile` prompts for auto-cancel (default: No) unless `--auto-cancel-expired` is provided
- `orders cancel` updates local execution/manual rows, refreshes open orders/balances, and recomputes `locked_qty`

---

## 7.X.19 Rules Source for LIMIT BUY Validation

LIMIT BUY execution must validate primarily against the **stored plan / candidate snapshot**, not ad-hoc live rule fetching.

Use the stored snapshot for:

- tick size
- step size
- minQty
- minNotional

Hard stop: if the required stored rule snapshot is missing for a LIMIT BUY candidate, hard-stop and exit non-zero.

Allowed live rechecks are limited to safety comparison only (e.g., symbol still trading, environment still valid, optional mismatch detection). Live rules must **not** silently replace stored approved sizing during normal execution.

---

## 7.X.20 Open Orders Visibility Note

Open orders will usually include active LIMIT orders, but filled MARKET orders may not appear there because they are typically completed immediately.

Practical meaning:

- LIMIT BUY reconciliation may use open-order state meaningfully
- MARKET BUY reconciliation must not rely on open orders alone
- MARKET BUY still needs direct order lookup by `origClientOrderId`

---

## 7.X.21 Required Candidate Fields for LIMIT BUY

To support LIMIT BUY cleanly, `execution_candidates` should include at minimum:

- `candidate_id`
- `plan_id`
- `trade_request_id`
- `symbol`
- `order_type`
- `limit_price`
- `approved_budget_amount`
- `approved_budget_asset`
- `execution_environment`
- `execution_ready`
- `risk_status`
- `validation_status`
- `created_at_utc`
- `updated_at_utc`

---

## 7.X.22 LIMIT BUY Timeout Summary

When `limit_order_timeout` is reached:

1. detect elapsed time
2. mark local execution as `expired`
3. persist `expired_at_utc`
4. do not auto-cancel on Binance by default
5. require reconciliation or manual cancel path if needed

---

## 7.X.23 Final Locked Decisions Summary

| Area | Locked Decision |
| --- | --- |
| LIMIT price source | persist in `execution_candidates` |
| Runtime `--limit-price` | not allowed for normal execution |
| Budget asset rule | must equal symbol quote asset |
| Timeout behavior | local `expired` only |
| Auto-cancel default | disabled |
| Rules source | stored snapshot first |
| Open-order note | add explicitly |

---

## 7.X.24 Implementation Principle

LIMIT BUY execution must remain:

- candidate-driven
- snapshot-validated
- environment-consistent
- non-ambiguous
- free from runtime price overrides

---

# Phase 7.X — LIMIT BUY Timeout Enforcement Clarification

> This section clarifies **how `limit_order_timeout` is actually enforced**.  
> Phase 7 itself performs **submission only** and does not run continuously, so timeout detection must occur in a later operational phase.

---

## 7.X.25 Timeout Enforcement Responsibility

Phase 7 **does not enforce timeouts itself** after submission.

Reason:

- Phase 7 is an **on-demand CLI execution step**
- it runs once to submit the order
- it does not run continuously or schedule background checks

Therefore, `limit_order_timeout` must be enforced by **later operational processes**.

---

## 7.X.26 Enforcement Mechanisms

Timeout detection may occur via one of the following mechanisms.

### A. Reconciliation Command (Manual / On-demand)

Recommended command:

```bash
cryptogent trade reconcile
```

Responsibilities:

- synchronize local execution state with exchange
- inspect open LIMIT orders
- check if `limit_order_timeout` has been exceeded
- mark executions as `expired` when necessary
- optionally trigger manual follow-up actions

Example workflow:

```text
execution submitted
  ↓
limit order remains open
  ↓
trade reconcile runs
  ↓
timeout detected
  ↓
execution status updated to expired
```

This approach works well for CLI-driven systems.

### B. Monitoring / Scheduler (Phase 8)

Phase 8 may introduce a **background monitoring process**.

Example mechanisms:

- cron job
- scheduled worker
- daemon service
- event loop monitor

Responsibilities:

- periodically scan `executions`
- detect expired LIMIT orders
- reconcile exchange state
- optionally trigger cancel policies if enabled

Example architecture:

```text
Phase 7 → submit order
Phase 8 → monitor open executions
```

---

## 7.X.27 Recommended MVP Approach

For MVP implementation:

- use the reconciliation command approach first

Example operational flow:

```bash
cryptogent trade execute <candidate_id>
cryptogent trade reconcile
```

Benefits:

- simpler implementation
- no background process required
- deterministic CLI workflow

Monitoring can be added later in Phase 8.

---

## 7.X.28 Future Monitoring Architecture (Phase 8)

Phase 8 may introduce:

- periodic order reconciliation
- automatic timeout detection
- automated cancellation policies
- execution health monitoring

Typical schedule example:

```text
every 60 seconds:
  check open LIMIT executions
  detect expired orders
  reconcile exchange status
```

---

## 7.X.29 Final Clarification

`limit_order_timeout` detection must be performed by:

- `trade reconcile` command, or
- Phase 8 monitoring service

Phase 7 **only records the timeout configuration** but does not enforce it actively.

---

## 7.X.30 Architectural Principle

Execution submission and lifecycle monitoring must remain **separate responsibilities**.

| Component | Responsibility |
| --- | --- |
| Phase 7 | submit execution |
| reconcile command | sync and detect expiration |
| Phase 8 | continuous monitoring (optional) |

This keeps the execution phase deterministic and avoids long-running execution logic inside the CLI command.

````
