````markdown
# Phase 9 – Reliability

This phase introduces the **state reliability and recovery protection** layer of CryptoGent.

By this stage, the system should already be able to:

- maintain local state in SQLite
- synchronize account data from Binance
- execute trades
- create and manage positions
- monitor active trades
- trigger exit or re-evaluation decisions

Phase 9 ensures that the system remains reliable when:

- the user manually changes the Binance account outside the bot
- local state drifts from exchange state
- the application crashes
- the machine restarts
- execution results are uncertain
- the system resumes after interruption

This phase focuses on **state correctness, reconciliation, and recovery robustness**.

---

# Phase Scope

This phase implements the following steps from the implementation roadmap:

33. external change detection  
34. reconciliation logic  
35. crash recovery  
36. restart tests  

---

# Core Objective

After completing Phase 9, CryptoGent should be able to:

- detect account changes made outside the bot
- detect local/exchange mismatches
- reconcile local state with exchange truth
- recover safely after crash or restart
- prevent duplicate or inconsistent actions after recovery
- validate resumed state before continuing automation

---

# Layers Covered in This Phase

This phase activates the following layer:

19. External Change Detection and Reconciliation Layer  

Supporting layers involved:

5. Account State Synchronization Layer  
6. Local State, Persistence, and Recovery Layer  
15. Order Execution Layer  
16. Position Management Layer  
18. Monitoring and Re-evaluation Layer  
20. Audit, Logging, and Reporting Layer  

---

# Reliability Philosophy

CryptoGent must always assume:

```text
The exchange is the source of truth.
Local state is only an operational cache.
````

This means the system must be able to handle situations where the user does something outside the bot, such as:

* manually buying a coin in Binance
* manually selling a tracked position
* cancelling an order from the Binance app
* depositing more funds
* moving funds between assets
* changing the account state during bot runtime

The system must detect such changes and respond safely.

---

# External Change Detection

External change detection means identifying account or order changes that did not originate from the current controlled bot action flow.

Examples of external changes:

* balance changed unexpectedly
* open order disappeared unexpectedly
* unknown order appeared
* position quantity no longer matches expected state
* asset holdings changed without a recorded bot event

The system must identify these cases during synchronization and monitoring.

---

# External Change Inputs

Inputs include:

* latest synchronized account balances
* latest synchronized open orders
* latest synchronized trades if available
* current local balances
* current local orders
* current local position state
* known recent bot actions

The system should compare:

```text
exchange state vs local expected state
```

---

# Types of External Change

Suggested external change categories:

## Balance Mismatch

Example:

* local state expects 300 USDT
* exchange now shows 180 USDT
* no known bot event explains the difference

---

## Unknown Order

Example:

* Binance returns an open or filled order not known to local order history

---

## Missing Expected Order

Example:

* local state expects an order to be open
* Binance no longer reports it

---

## Position Mismatch

Example:

* local position says BTCUSDT quantity = 0.01
* exchange holdings imply position no longer exists

---

## Manual Override

Example:

* user manually sold the tracked asset from the Binance app

---

# Detection Rules

Detection should remain deterministic.

Basic rules:

* if a tracked balance differs materially from latest exchange state, flag mismatch
* if a tracked open order is missing, flag mismatch
* if an unknown order appears, flag mismatch
* if position quantity differs from exchange-reconcilable quantity, flag mismatch
* if order lifecycle changed without local record, flag mismatch

The system should not guess silently.

It should explicitly record a reconciliation event.

---

# Reconciliation Logic

Reconciliation is the process of rebuilding correct local state after mismatch detection.

This phase should implement deterministic reconciliation logic.

The reconciliation layer should never prioritize stale local assumptions over exchange facts.

---

# Reconciliation Responsibilities

The reconciliation logic must be able to:

* update balances from exchange state
* update order states from exchange state
* mark unknown orders appropriately
* re-evaluate active position consistency
* close invalid local positions if no exchange-backed basis remains
* pause automation if state becomes ambiguous

---

# Reconciliation Outcomes

Suggested outcomes:

```text
no_change
reconciled
reconciled_with_warning
manual_intervention_required
```

Examples:

## no_change

Exchange and local state match.

## reconciled

Mismatch found and resolved safely.

## reconciled_with_warning

Mismatch resolved, but active automation should be cautious.

## manual_intervention_required

System cannot safely determine the correct operational state.

---

# Reconciliation Examples

## Example 1 – Manual Sell Outside Bot

Local state:

```text
open position: SOLUSDT, quantity 1.742
```

Exchange state:

```text
SOL balance no longer present
```

Action:

* mark position inconsistent
* close local position record or mark externally closed
* trigger warning
* pause related automation

---

## Example 2 – Unknown Open Order

Exchange state contains an order not in local DB.

Action:

* store order
* mark as external or untracked origin
* do not assume it belongs to the current bot workflow
* warn or isolate it from active automation logic

---

## Example 3 – Uncertain Execution Recovery

A previous execution attempt timed out locally, but exchange later shows the order as filled.

Action:

* update local order state to filled
* create/update position from exchange-backed result
* clear uncertain execution state

---

# Reconciliation Safety Policy

The reconciliation phase must prefer safety over aggressiveness.

If the system cannot confidently rebuild a valid active state, it should:

* pause automation
* log the condition
* surface a warning
* wait for a safe restart or user action

It must not continue making autonomous trading decisions on ambiguous state.

---

# Crash Recovery

Crash recovery ensures the system can resume safely after unexpected interruption.

Examples:

* process crash
* forced shutdown
* machine restart
* runtime exception during execution or monitoring

The goal is not merely to restart the app, but to restart it **without corrupting trading state**.

---

# Crash Recovery Requirements

On restart, the system must be able to recover:

* latest known balances
* latest known open orders
* latest known active position
* pending execution candidate if relevant
* uncertain execution states
* monitoring status
* last successful sync metadata

This recovery must happen before automation resumes.

---

# Startup Recovery Sequence

Recommended recovery flow:

```text
Start application
   ↓
Load configuration
   ↓
Initialize database and state manager
   ↓
Load local runtime state
   ↓
Detect incomplete or uncertain prior state
   ↓
Run startup synchronization with exchange
   ↓
Run reconciliation
   ↓
Decide whether automation may resume
```

The system must not resume trade monitoring or execution blindly from local state only.

---

# Crash Recovery States

Suggested recovery status values:

```text
clean_restart
recovering
reconciled_after_restart
paused_due_to_uncertainty
```

Example:

* if local state is complete and exchange sync confirms it → `reconciled_after_restart`
* if previous execution was uncertain and state is ambiguous → `paused_due_to_uncertainty`

---

# Safe Resume Rules

The system should resume monitoring or automation only if:

* latest synchronization succeeded
* reconciliation found no critical ambiguity
* active position is consistent
* no uncertain execution remains unresolved
* no external manual override invalidates current workflow

If any of these fail, the system should enter a paused or warning state.

---

# Restart Tests

This phase must include practical restart and recovery tests.

Recommended scenarios:

## Test 1 – Clean Restart

* no active position
* known balances only
* app restarts
* state loads successfully

## Test 2 – Restart with Active Position

* active position exists
* app restarts
* state reloads
* exchange sync confirms position context

## Test 3 – Manual Account Change During Downtime

* app stops
* user changes Binance account manually
* app restarts
* reconciliation detects mismatch

## Test 4 – Uncertain Execution Recovery

* simulate timeout during order placement
* restart app
* sync exchange state
* reconcile final order truth

## Test 5 – Missing Order Recovery

* local state expects an order
* exchange no longer reports it
* reconciliation updates local state correctly

These tests should be implemented at least as structured manual/integration scenarios even if not fully automated at first.

---

# Persistence for Reliability Events

This phase should persist reconciliation and recovery events.

Suggested table:

## `reconciliation_events`

Fields:

```text
id
event_type
status
summary
details
created_at
updated_at
```

Possible `event_type` values:

```text
balance_mismatch
unknown_order
missing_order
position_mismatch
startup_recovery
uncertain_execution_recovery
```

Possible `status` values:

```text
detected
reconciled
warning
manual_intervention_required
```

This is useful for:

* audit trail
* debugging
* restart diagnostics
* user visibility

---

# Automation Pause Behavior

This phase should define how the system pauses itself safely.

Examples of pause-worthy situations:

* unresolved external changes
* uncertain execution not reconciled
* active position mismatch
* missing critical exchange sync
* startup recovery failure

A paused state should:

* stop automated execution
* stop automated exits if state is too ambiguous
* continue allowing diagnostics or safe sync attempts
* clearly inform the user through logs and CLI

---

# CLI Behavior

The CLI should expose reliability visibility.

Suggested outputs:

## Reconciliation Summary

```text
Reconciliation Summary
- Status: reconciled_with_warning
- Issue: External balance change detected
- Action: Local state updated, automation paused
```

## Recovery Summary

```text
Startup Recovery
- Status: reconciled_after_restart
- Active Position: SOLUSDT
- Order State: consistent
- Monitoring: resumed
```

## Ambiguous State Warning

```text
Recovery Warning
- Status: paused_due_to_uncertainty
- Reason: Previous execution result could not be confirmed safely
- Action: Review account state and re-run synchronization
```

---

# Error Handling

The reliability phase must handle:

* failed startup sync
* corrupted local state
* missing required recovery records
* unresolved uncertain execution
* impossible position reconstruction
* repeated reconciliation failures

Errors must:

* be logged clearly
* avoid unsafe automatic continuation
* trigger pause when necessary
* preserve as much diagnostic context as possible

---

# Logging Requirements

Log all critical reliability events.

Minimum logs:

* external mismatch detected
* reconciliation started
* reconciliation completed
* startup recovery started
* startup recovery completed
* automation paused
* uncertain execution resolved
* manual intervention required

Example logs:

```text
[WARN] Reconciliation: Balance mismatch detected for USDT
[INFO] Reconciliation: Local order state updated from exchange truth
[INFO] Recovery: Startup synchronization completed
[WARN] Recovery: Automation paused due to unresolved position mismatch
```

---

# Suggested Modules

Suggested files for this phase:

```text
reconciliation/
  detector.py
  reconciler.py
  policies.py
  startup_recovery.py

reliability/
  pause_state.py
  recovery_status.py

models/
  reconciliation_result.py
  recovery_result.py
  external_change_event.py
```

Possible responsibilities:

## `detector.py`

* compare exchange state with local state
* generate mismatch events

## `reconciler.py`

* apply deterministic reconciliation logic

## `policies.py`

* define safe pause / resume rules

## `startup_recovery.py`

* orchestrate startup recovery flow

## `pause_state.py`

* manage paused automation state

## `recovery_status.py`

* standardize recovery outcome states

---

# Deliverables

Phase 9 is complete when:

* external account changes can be detected
* mismatches can be reconciled safely
* startup recovery works
* uncertain prior state can be handled safely
* the system can pause itself when state is ambiguous
* reconciliation and recovery events are persisted

---

# Success Criteria

Phase 9 is successful when the system can:

* detect local/exchange state drift
* rebuild correct local state from exchange truth
* recover safely after restart or crash
* avoid duplicate or unsafe post-restart actions
* pause automation whenever reliability is uncertain

```
```
