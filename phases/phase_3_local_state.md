````markdown
# Phase 3 – Local State

This phase introduces the **local state management foundation** of CryptoGent.

The goal of this phase is to create a reliable local persistence layer using SQLite so the system can:

- store synchronized account state
- maintain operational state
- persist balances, orders, and positions
- recover safely after restart
- rebuild context before future execution phases

This phase should not introduce strategy, validation, or execution logic yet.  
It should focus only on **state, persistence, synchronization storage, and recovery readiness**.

---

# Phase Scope

This phase implements the following steps from the implementation roadmap:

11. database schema  
12. state manager  
13. balance synchronization  
14. order synchronization  
15. startup recovery  

---

# Core Objective

After completing Phase 3, CryptoGent should be able to:

- persist local account snapshots
- store balance state
- store order state
- store position state
- update local state after synchronization
- detect previously known runtime state
- recover safely on restart
- rebuild local context before later phases

The exchange remains the **source of truth**.  
The local database acts as a **cached operational state**.

---

# Source of Truth Rule

CryptoGent must follow this rule strictly:

```text
Binance account state = source of truth
Local SQLite state = synchronized operational cache
````

This means:

* local state may be outdated
* exchange data has priority
* local state must be revalidated after restart
* local state should never be blindly trusted for execution

---

# Database Responsibilities

The SQLite database should now become a real operational storage layer.

It must support storing:

* account snapshots
* asset balances
* open orders
* known order history
* active positions
* system state metadata
* synchronization metadata
* recovery metadata

---

# Database Schema

Phase 3 should expand the initial schema into a more useful operational schema.

Suggested tables:

## `system_state`

Stores high-level runtime metadata.

Fields:

```text
id
last_start_time
last_shutdown_time
last_successful_sync_time
current_mode
created_at
updated_at
```

---

## `account_snapshots`

Stores account-level state summary at specific sync times.

Fields:

```text
id
snapshot_time
total_asset_value
valuation_asset
sync_source
created_at
```

---

## `balances`

Stores per-asset balances.

Fields:

```text
id
asset
free
locked
snapshot_time
updated_at
```

---

## `orders`

Stores known order state.

Fields:

```text
id
exchange_order_id
symbol
side
type
price
quantity
executed_quantity
status
time_in_force
created_at
updated_at
```

---

## `positions`

Stores bot-known active or historical positions.

Fields:

```text
id
symbol
entry_price
quantity
target_profit_percent
stop_loss_percent
deadline
status
opened_at
closed_at
updated_at
```

---

## `sync_events`

Stores synchronization activity.

Fields:

```text
id
sync_type
started_at
completed_at
status
message
created_at
```

Examples of `sync_type`:

```text
startup
manual
scheduled
immediate
```

---

# Database Design Considerations

The schema should be designed with the following principles:

* simple enough for local use
* easy to query
* easy to update after exchange sync
* suitable for recovery
* safe for future extension

Avoid overengineering in this phase.

Do not add unnecessary tables for future features yet unless they are clearly useful now.

---

# State Manager

Create a dedicated state manager that becomes the interface between application logic and SQLite.

Suggested module:

```text
state/state_manager.py
```

Responsibilities:

* load latest balances
* save balances
* save orders
* save positions
* load active position
* update system metadata
* record synchronization activity
* provide recovery-ready state

The rest of the system should not directly write raw SQL everywhere.
State access should go through this manager or a clearly defined persistence layer.

---

# State Manager Responsibilities

The state manager must support at least the following operations:

## Balance State

* upsert asset balances
* fetch all latest balances
* fetch single asset balance

## Order State

* upsert orders
* fetch open orders
* fetch known order by exchange order ID

## Position State

* create position
* update position
* fetch active position
* close position

## System State

* update last start time
* update last sync time
* fetch runtime metadata

## Sync State

* create sync event
* mark sync as success or failure

---

# Balance Synchronization

Phase 3 must implement logic to store synchronized balances locally.

This phase does not need to call Binance directly if Phase 2 already has account retrieval methods.
Instead, it should consume synchronized exchange responses and persist them properly.

Input source:

```text
GET /api/v3/account
```

Relevant fields:

```text
asset
free
locked
```

Balance synchronization should:

* overwrite stale known balance state
* update existing assets
* keep timestamped snapshot context
* support future reconciliation

---

# Balance Sync Flow

Recommended flow:

```text
Exchange Account Response
    ↓
Normalize balance data
    ↓
Upsert balances into SQLite
    ↓
Create account snapshot
    ↓
Update system last sync time
```

---

# Order Synchronization

Phase 3 must implement local persistence for known exchange orders.

Input sources may include:

```text
GET /api/v3/openOrders
GET /api/v3/myTrades
```

Order synchronization should store:

* exchange order ID
* symbol
* side
* type
* status
* quantities
* timestamps

This local order state will be required later for:

* monitoring
* reconciliation
* recovery
* execution result tracking

---

# Order Sync Rules

Order synchronization should:

* upsert by exchange order ID
* update existing order status
* preserve known order history
* distinguish between open and completed states

Do not create duplicate order rows for the same exchange order unless historical versioning is intentionally designed.

For MVP, simple upsert is enough.

---

# Startup Recovery

Startup recovery is a major responsibility of this phase.

When CryptoGent starts, it must be able to recover local context and prepare for re-synchronization.

Startup recovery should load:

* system state
* latest balances
* known open orders
* active positions
* latest sync metadata

This recovery is **local only** in this phase.

It prepares the system for full exchange revalidation.

---

# Startup Recovery Flow

Recommended startup recovery sequence:

```text
System starts
    ↓
Load configuration
    ↓
Initialize database
    ↓
Initialize state manager
    ↓
Load latest balances
    ↓
Load open orders
    ↓
Load active positions
    ↓
Load latest sync metadata
    ↓
Mark system ready for exchange re-sync
```

Important note:

Phase 3 recovery does **not** replace future exchange synchronization.
It only restores the local operational context.

---

# Local State Boundaries

This phase must not assume the local DB is enough for final trading decisions.

Local state is useful for:

* continuity
* recovery
* tracking
* monitoring preparation

Local state is not enough for:

* final balance truth
* order truth
* execution truth

Those always require exchange verification.

---

# Error Handling

Phase 3 must handle:

* database connection failure
* schema initialization failure
* write errors
* invalid state records
* corrupted local state

Errors must:

* be logged
* fail clearly
* not silently corrupt state

If local state cannot be trusted, the system should still be able to restart in a safe mode and wait for fresh exchange synchronization.

---

# Logging Requirements

Log all important persistence actions.

Minimum logs:

* database initialized
* balances updated
* orders updated
* startup recovery loaded
* sync event stored
* database error

Example log messages:

```text
[INFO] StateManager: Loaded 4 balances from local state
[INFO] StateManager: Upserted 2 open orders
[INFO] Recovery: Active position restored for BTCUSDT
```

---

# Suggested Modules

Suggested files for this phase:

```text
database/
  db.py
  schema.py
  migrations.py

state/
  state_manager.py
  recovery.py
  sync_store.py
```

Possible responsibilities:

## `db.py`

* SQLite connection
* session or connection management

## `schema.py`

* table definitions
* schema initialization

## `migrations.py`

* schema updates if needed

## `state_manager.py`

* high-level read/write state operations

## `recovery.py`

* startup recovery helpers

## `sync_store.py`

* sync event recording helpers

---

# Deliverables

Phase 3 is complete when:

* operational SQLite schema exists
* balances can be persisted locally
* orders can be persisted locally
* positions can be stored and retrieved
* state manager works
* startup recovery loads local context successfully
* sync metadata is stored

---

# Success Criteria

Phase 3 is successful when the system can:

* initialize the full local schema
* persist balances from synchronized account data
* persist orders from synchronized exchange data
* restore known local state after restart
* expose recovery-ready operational context for future phases

```
```
