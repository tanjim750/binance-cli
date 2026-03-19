````markdown
# Phase 8 – Monitoring

This phase introduces the **continuous post-execution control loop** of CryptoGent.

By this stage, the system should already be able to:

- create a validated trade request
- build a trade plan
- pass deterministic validation and risk checks
- execute a Spot order
- create and persist an active position
- synchronize account state after execution

Phase 8 is responsible for **tracking active positions and deciding when re-evaluation or exit logic should be triggered**.

This phase should not yet focus on external reconciliation reliability concerns beyond what is necessary for monitoring.  
Its main purpose is to watch the trade lifecycle after entry.

---

# Phase Scope

This phase implements the following steps from the implementation roadmap:

30. monitoring loop  
31. exit control  
32. re-evaluation triggers  

---

# Core Objective

After completing Phase 8, CryptoGent should be able to:

- continuously monitor active positions
- check price movement and PnL progression
- detect target profit conditions
- detect stop-loss conditions
- detect deadline conditions
- trigger trade re-evaluation when meaningful changes occur
- trigger an exit decision when required

This phase should prepare the system for safe automated trade closure.

---

# Layers Covered in This Phase

This phase activates the following layers:

17. Deadline and Exit Control Layer  
18. Monitoring and Re-evaluation Layer  

Supporting layers involved:

4. Exchange Connection Layer  
5. Account State Synchronization Layer  
6. Local State, Persistence, and Recovery Layer  
7. Market Data Layer  
16. Position Management Layer  
20. Audit, Logging, and Reporting Layer  

---

# Monitoring Philosophy

CryptoGent must not assume that a trade can be left unattended after execution.

Once a position is open, the system should keep track of:

- current price
- unrealized PnL
- stop-loss proximity
- target progress
- deadline pressure
- position status

The monitoring loop should remain **deterministic and event-driven where possible**.

The system should avoid unnecessary heavy computation or continuous LLM usage.

---

# Monitoring Loop

The monitoring loop is the heart of this phase.

It should periodically inspect the active position and relevant market data.

Suggested MVP behavior:

- run at a configured interval
- inspect only active positions
- load current position state from local DB
- refresh relevant market data
- evaluate exit and re-evaluation conditions
- persist monitoring state

---

# Monitoring Interval

The interval should come from configuration **by default**, with CLI override.

Priority order:

1. CLI `--interval-seconds`
2. `trading.monitoring_interval_seconds` from TOML
3. Safe fallback default

Example:

```toml
[trading]
monitoring_interval_seconds = 60
```

Meaning:

```text
every 60 seconds
```

The monitoring loop should print the chosen interval and its source on startup:

```text
interval=60 source=config
```

---

# Monitoring Inputs

Inputs include:

* active position
* latest market price
* latest candle context if needed
* current timestamp
* target profit percent
* stop-loss percent
* deadline
* position entry price
* execution metadata

---

# Position State Required

The monitoring logic depends on position data such as:

* symbol
* side
* entry price
* quantity
* target profit
* stop-loss
* deadline
* current status
* opened_at

Only open positions should be actively monitored.

---

# Price Tracking

The loop must retrieve current price for the active symbol.

Recommended endpoint:

```text
GET /api/v3/ticker/price
```

For richer context, it may also fetch:

* 24hr stats
* a small recent candle window

But current price is the minimum requirement for MVP exit logic.

---

# Profit and Loss Tracking

The monitoring phase should calculate at least the unrealized PnL.

Suggested outputs:

* current notional value
* price change percent from entry
* unrealized profit/loss
* target distance
* stop-loss distance

Example concept:

```text
price_change_percent = ((current_price - entry_price) / entry_price) * 100
```

This calculation should be deterministic and reusable.

---

# Exit Control

Exit control determines when the system should stop holding the position and prepare an exit order.

This phase should not directly manage reconciliation concerns outside monitoring, but it must produce a clean and deterministic exit trigger.

---

# Exit Conditions

At minimum, the system must support the following exit conditions.

## Target Profit Reached

If the position reaches or exceeds the target profit threshold:

* trigger exit

---

## Stop-Loss Reached

If the position reaches or crosses the stop-loss threshold:

* trigger exit

This is mandatory.

---

## Deadline Reached

If the configured deadline has passed:

* trigger exit

The system must not keep the trade open indefinitely once the deadline expires.

---

## Strategy Invalidation Trigger

The MVP may support a simple invalidation rule, such as:

* strong adverse move before target
* repeated weakness confirmed by basic monitoring logic

This can remain lightweight in the first implementation.

---

# Exit Trigger Output

The exit control layer should produce a structured exit trigger.

Suggested fields:

```text
exit_required
exit_reason
trigger_price
trigger_time
summary
```

Possible `exit_reason` values:

```text
target_reached
stop_loss_hit
deadline_reached
strategy_invalidated
```

Example:

```text
exit_required: true
exit_reason: target_reached
trigger_price: 106.12
trigger_time: 2026-03-14T18:20:00Z
summary: Profit target reached for SOLUSDT
```

---

# Re-evaluation Triggers

Not every market change should immediately trigger a re-evaluation.

The system should only re-evaluate when significant conditions occur.

Examples:

* large move toward stop-loss
* large move toward target
* deadline nearing
* abrupt momentum change
* account state update affecting trade viability

This phase should generate a **re-evaluation trigger**, not a new strategy by itself.

---

# Re-evaluation Trigger Output

Suggested structure:

```text
reevaluation_required
trigger_reason
trigger_time
priority
summary
```

Possible reasons:

```text
deadline_near
sharp_price_move
pnl_threshold_crossed
market_condition_changed
```

Example:

```text
reevaluation_required: true
trigger_reason: deadline_near
priority: high
summary: Trade has less than 2 hours remaining before deadline.
```

---

# Monitoring Decisions

At each monitoring cycle, the system should decide one of the following:

```text
hold
exit_recommended
reevaluate
data_unavailable
```

This decision must be persisted and logged.

---

# Monitoring State Persistence

Monitoring results should be stored so that the system can recover cleanly after restart.

Suggested table:

## `monitoring_events`

Fields:

```text
monitoring_event_id
position_id
created_at_utc
symbol
entry_price
current_price
pnl_percent
decision
exit_reason
deadline_utc
position_status
error_code
error_message
```

Possible `decision` values:

```text
hold
exit_recommended
reevaluate
data_unavailable
```

This helps with:

* audit trail
* debugging
* restart continuity

---

# Monitoring Loop Flow

Recommended monitoring sequence:

```text
Load active position
   ↓
Fetch latest price
   ↓
Compute PnL and thresholds
   ↓
Check stop-loss
   ↓
Check target profit
   ↓
Check deadline
   ↓
Check re-evaluation triggers
   ↓
Persist monitoring result
   ↓
Return monitoring decision
```

This sequence should remain deterministic and easy to reason about.

---

# Multiple Positions

For MVP, the project should continue assuming:

```text
one active position at a time
```

This keeps monitoring simpler.

The monitoring loop should therefore focus on:

* zero active position → no-op
* one active position → monitor fully

Support for multiple concurrent positions can come later.

---

# CLI Behavior

The monitoring phase should support CLI visibility for current trade status.

Example commands:

```text
cryptogent monitor once
cryptogent monitor loop
cryptogent position show <position_id>
cryptogent monitor events list
```

Example output:

```text
Active Position
- Symbol: SOLUSDT
- Entry Price: 103.28
- Current Price: 105.10
- PnL: +1.76%
- Target: 4%
- Stop-Loss: 2%
- Deadline Remaining: 14 hours
- Status: monitoring
```

If an exit condition is reached:

```text
Monitoring Alert
- Symbol: SOLUSDT
- Exit Trigger: target_reached
- Current Price: 107.45
- Action: ready to close position
```

---

# Error Handling

The monitoring phase must handle:

* missing active position
* missing market price
* corrupted position state
* expired deadline with inconsistent state
* temporary API failures during price retrieval
* local scheduler interruption

Errors must:

* be logged
* avoid crashing the monitoring loop unnecessarily
* mark monitoring state clearly when incomplete

If monitoring cannot continue safely, the system should surface a warning and pause the automated monitoring state if needed.

---

# Logging Requirements

Log all meaningful monitoring actions.

Minimum logs:

* monitoring cycle started
* current price retrieved
* PnL calculated
* exit trigger created
* re-evaluation trigger created
* monitoring cycle completed
* monitoring warning or failure

Example logs:

```text
[INFO] Monitoring: Active position loaded for SOLUSDT
[INFO] Monitoring: Current price 105.10, PnL +1.76%
[INFO] ExitControl: No exit condition met
[WARN] Monitoring: Deadline near for SOLUSDT
```

---

# Suggested Modules

Suggested files for this phase:

```text
monitoring/
  loop.py
  evaluator.py
  scheduler.py
  events.py

exit_control/
  rules.py
  controller.py

models/
  monitoring_result.py
  exit_trigger.py
  reevaluation_trigger.py
```

Possible responsibilities:

## `loop.py`

* orchestrates each monitoring cycle

## `evaluator.py`

* calculates PnL and checks thresholds

## `scheduler.py`

* handles periodic loop scheduling

## `events.py`

* records monitoring events

## `controller.py`

* exit trigger decision logic

## `rules.py`

* target, stop-loss, and deadline checks

---

# Deliverables

Phase 8 is complete when:

* a periodic monitoring loop exists
* active positions can be monitored continuously
* PnL is calculated from current price and entry price
* target, stop-loss, and deadline exits are detected
* re-evaluation triggers are generated when needed
* monitoring events are logged and persisted

No external change reconciliation logic should be the focus here beyond what is needed to keep monitoring operational.

---

# Success Criteria

Phase 8 is successful when the system can:

* load an active position
* observe it at a fixed interval
* detect whether it should continue, re-evaluate, or exit
* persist and log monitoring state
* provide a clear monitoring decision for the next phase

# Phase 8.X — Monitoring Refinements and Lock-Ins

> This section extends **Phase 8 Monitoring** with deterministic rules required before implementation.  
> It does **not modify previous Phase 8 text**.  
> Monitoring must remain auditable, restart-safe, and clearly separated from execution.

---

# 8.X Monitoring Configuration Format

The configuration example must use valid **TOML**, not YAML-like syntax.

## Locked Example

```toml
[trading]
monitoring_interval_seconds = 60
monitoring_price_environment = "mainnet_public"
```

Allowed values for `monitoring_price_environment`:

* `"mainnet_public"`
* `"testnet"`

---

# 8.X Remaining Implementation Checklist

This section lists the remaining items required to complete Phase 8.

## 1) Config-driven monitoring interval

**Status:** Implemented  
**Notes:** Uses `--interval-seconds` when provided, otherwise `trading.monitoring_interval_seconds`, else fallback. Prints source on startup (`cli|config|fallback`).

## 2) Monitoring failure backoff policy

**Status:** Implemented  
**Notes:** Backoff on repeated monitoring fetch failures:

* first failure → next delay = interval × 2  
* repeated failures → next delay = interval × 5  
* success → reset to normal interval  

Required monitoring events:

```text
monitoring_fetch_failed
monitoring_backoff_applied
monitoring_recovered
```

## 3) Re-evaluation trigger support

**Status:** Implemented  
**Notes:** Decision type `reevaluate` with reason codes (e.g. `soft_deadline_reached`, `profit_threshold_reached`, `drawdown_warning`) is persisted and shown in CLI outputs.

## 4) Configuration format cleanup

**Status:** Implemented  
**Notes:** Replaced YAML-like examples with valid TOML. Example:

```toml
[trading]
monitoring_interval_seconds = 15

[monitoring]
use_server_time = true
```

Optional future extension:

```toml
[monitoring]
use_server_time = true
backoff_multiplier_first = 2
backoff_multiplier_repeated = 5
```

---

# 8.X Monitoring Price Environment

## Final Decision

Phase 8 monitoring must use the **plan / position market-data environment**, not the runtime execution environment.

## Rule

Monitoring price checks must use:

```text
position.market_data_environment
```

or, if the position was created from a plan:

```text
trade_plan.market_data_environment
```

Monitoring must **not** implicitly switch to:

```text
binance.testnet = true | false
```

for price evaluation.

## Reason

Execution environment and price-observation environment are separate concerns.

Example:

* `execution_environment = testnet`
* `market_data_environment = mainnet_public`

In this case:

* execution uses testnet
* monitoring price uses mainnet public data

This preserves consistency with earlier phase rules.

---

# 8.X Monitoring Scope

## Final Decision

Phase 8 monitors **positions only**.

It does **not** monitor open LIMIT execution lifecycle as part of core position monitoring.

## Rule

Phase 8 starts only **after a position exists**.

Meaning:

* MARKET BUY that created a position → Phase 8 monitors it
* LIMIT BUY still open and not filled → not a Phase 8 position-monitoring concern

## Responsibility Split

| Concern                        | Responsible Component                             |
| ------------------------------ | ------------------------------------------------- |
| open LIMIT execution lifecycle | `trade reconcile` / execution reconciliation flow |
| active position monitoring     | Phase 8                                           |

## Clarification

If a LIMIT BUY remains open with no filled quantity, Phase 8 must not create a synthetic position just to monitor it.

---

# 8.X Position Quantity Definition

PnL logic must define whether monitored quantity is **gross** or **net**.

## Final Decision

Phase 8 position quantity must use **net position quantity**.

## Rule

If BUY commission is charged in base asset:

```text
net_position_quantity = executed_quantity - base_asset_commission
```

If commission is charged in quote asset or another asset:

* base quantity remains unchanged
* fee must still be reflected in PnL accounting where applicable

## Example

If a BUY fill returns:

* `executed_quantity = 0.01000000 BTC`
* `commission = 0.00001000 BTC`
* `commission_asset = BTC`

Then:

```text
net_position_quantity = 0.00999000 BTC
```

This net quantity is the monitored position size.

---

# 8.X PnL Precision Rules

## Final Decision

PnL calculations must use **Decimal**, never float.

## Rule

All of the following must be handled using decimal-safe arithmetic:

* entry price
* current price
* quantity
* quote value
* fee adjustments
* percentage calculations

## Reason

Float arithmetic introduces drift and can break:

* threshold checks
* stop-loss logic
* take-profit logic
* audit reproducibility

---

# 8.X PnL Definition

Phase 8 must define unrealized PnL deterministically.

## Required Stored Inputs

At minimum:

* `entry_price`
* `current_price`
* `net_position_quantity`
* fee-adjusted cost basis where available

## Recommended Formula

For long spot position:

```text
position_market_value = current_price × net_position_quantity
position_cost_basis = entry_price × net_position_quantity
unrealized_pnl = position_market_value - position_cost_basis
pnl_percent = unrealized_pnl / position_cost_basis × 100
```

## Fee Treatment

If fees were charged in base asset:

* reflected through reduced `net_position_quantity`

If fees were charged in quote asset:

* may be added to cost basis if stored and available

If fees were charged in another asset:

* record them separately
* do not silently convert unless a deterministic conversion rule exists

---

# 8.X Monitoring Outputs Are Decisions, Not Actions

## Final Decision

Phase 8 monitoring must produce **decision outputs only**.

It must **not** place exit orders.

## Allowed Outputs

* hold
* warning
* exit_triggered
* deadline_reached
* stop_loss_triggered
* take_profit_triggered
* monitoring_paused

## Rule

If an exit condition is met, Phase 8 must:

1. create and persist an exit trigger / monitoring event
2. print the decision summary
3. stop there

Actual exit execution belongs to a later phase.

## Architectural Principle

| Phase    | Responsibility   |
| -------- | ---------------- |
| Phase 8  | observe + decide |
| Phase 9+ | execute exit     |

---

# 8.X Monitoring Event Persistence

The minimal event shape must be expanded so events are useful for:

* audit
* restart recovery
* debugging
* post-trade analysis

## Required `monitoring_events` Fields

* `monitoring_event_id`
* `position_id`
* `created_at_utc`
* `symbol`
* `entry_price`
* `current_price`
* `pnl_percent`
* `decision`
* `exit_reason`
* `deadline_utc`
* `position_status`
* `error_code`
* `error_message`

## Notes

* `exit_reason` may be null when no exit trigger exists
* `error_code` / `error_message` are used for monitoring failures
* numeric fields must be persisted in decimal-safe string or fixed-precision form

---

# 8.X Position State Persistence

Phase 8 should assume monitoring may restart at any time.

Therefore position state must be restart-safe.

## Minimum Recommended Position Fields

* `position_id`
* `symbol`
* `market_data_environment`
* `execution_environment`
* `entry_price`
* `net_position_quantity`
* `position_status`
* `deadline_utc`
* `created_at_utc`
* `updated_at_utc`
* `last_monitored_at_utc`

---

# 8.X Monitoring Worst-Case Scenarios

The following additional worst cases must be explicitly handled.

## 1. Price Endpoint Unavailable or Rate-Limited

### Behavior

* apply backoff
* persist monitoring event
* set decision/state:

```text
pause_due_to_state_issue
```

### Required Event Data

* `error_code`
* `error_message`
* `created_at_utc`

---

## 2. Clock Skew / Bad Local Time

### Risk

Deadline triggers may fire incorrectly if local system time is wrong.

## Rule

Use server time when available for deadline-sensitive comparisons.

If server time is unavailable:

* use local UTC time
* persist warning event
* mark monitoring tick as degraded if skew is suspected

---

## 3. Position Missing Fields After Restart

### Rule

If required fields are missing after restart:

* fail the monitoring tick closed
* do not compute PnL
* persist warning / error event
* leave position unchanged until repaired

Required missing-field examples:

* missing entry price
* missing quantity
* missing market data environment
* missing deadline when deadline-based monitoring is enabled

---

## 4. Symbol Halted / Not Trading

### Rule

If a monitored symbol becomes halted or non-trading:

* persist immediate warning event
* create re-evaluation / exit-trigger candidate
* do not silently continue normal monitoring

This is a serious state change and must be surfaced.

---

# 8.X Backoff Rule

Monitoring failures must not hammer APIs.

## Recommended Rule

On repeated price-fetch failures:

* first failure: record warning
* repeated failures: exponential or stepped backoff
* persist each state transition

Example policy:

```text
attempt 1 → next check in normal interval
attempt 2 → backoff to 2 × interval
attempt 3+ → backoff to 5 × interval, capped
```

---

# 8.X CLI Interface

Because CLI commands are the real external API, Phase 8 must define concrete commands early.

## Locked Commands

```bash
cryptogent monitor once
cryptogent monitor loop
cryptogent position show <position_id>
cryptogent monitor events list
```

## Command Roles

### `cryptogent monitor once`

* run one monitoring tick
* evaluate active positions
* persist monitoring events
* print summary

### `cryptogent monitor loop`

* repeatedly run monitoring ticks
* sleep using `monitoring_interval_seconds`
* intended for long-running local or server process

### `cryptogent position show <position_id>`

* display stored position details
* show entry price, quantity, environment, status, latest monitoring result

### `cryptogent monitor events list`

* list monitoring history
* show warnings, trigger decisions, errors, pauses

---

# 8.X Open Orders Visibility Note

Add this note to Phase 8 documentation:

> Active LIMIT orders may appear in open-order views, but Phase 8 does not treat open executions as positions. Monitoring begins only after a position exists.

This avoids confusion between:

* execution lifecycle
* position lifecycle

---

# 8.X Rules Snapshot vs Live Rules

## Final Decision

Phase 8 position monitoring does **not** need full live rule re-validation on every tick.

It should primarily use persisted position / plan context and current market price.

Live rule rechecks may be used only when needed for:

* symbol trading status
* environment consistency
* exceptional state changes

Monitoring is not a re-planning phase.

---

# 8.X Final Locked Summary

| Area                           | Locked Decision                             |
| ------------------------------ | ------------------------------------------- |
| Config format                  | valid TOML                                  |
| Price environment              | use `market_data_environment`               |
| Scope                          | monitor positions only                      |
| Open LIMIT handling            | handled by reconcile, not core Phase 8      |
| Position quantity              | net quantity                                |
| Arithmetic                     | Decimal only                                |
| Monitoring result              | decision output only                        |
| Exit action                    | deferred to later phase                     |
| Event persistence              | expanded audit fields required              |
| Timeout / deadline time source | prefer server time                          |
| CLI                            | `monitor once`, `monitor loop`, `position show`, `monitor events list` |

---

# 8.X Implementation Principle

Phase 8 must remain a **state observer and decision engine** for active positions.

It must:

* read persisted position state
* fetch current price using the correct market-data environment
* compute fee-aware PnL with Decimal
* produce auditable monitoring decisions
* avoid placing any orders

```
```

---

# Phase 8.X — Dust Ledger Visibility (Accounting Only)

> This section extends Phase 8 with dust-ledger monitoring guidance.  
> It does **not modify previous Phase 8 text**.

## Dust Is Not a Position

Dust balances must not be treated as positions for monitoring decisions.

Rule:

* Phase 8 monitors positions only
* dust ledger is accounting-only and should not trigger exit logic

## Dust Ledger Purpose in Phase 8

The dust ledger exists to preserve cost basis for accumulated dust so that if dust is later sold (manually), realized PnL can be computed deterministically.

## Recommended Visibility Outputs

Phase 8 (or adjacent CLI/status views) should be able to report:

* dust quantity per asset
* average cost price
* whether the dust row needs reconciliation (`needs_reconcile`)

If dust becomes tradable (>= `minQty` and `minNotional`):

* emit a warning event
* recommend a manual dust sweep flow (future phase)

## Source of Truth Reminder

Binance balances remain the source of truth.

The dust ledger must be reconciled (clamped) on successful balance syncs using:

```text
max_unpositioned_free = max(0, binance_free_balance - open_position_qty_total)
effective_dust = min(dust_ledger_qty, max_unpositioned_free)
```

## Reference Document

This phase must follow:

* `phases/locked_rules_sell_pnl_dust.md`
