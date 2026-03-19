```markdown
# CryptoGent – System Plan

## Overview

CryptoGent is an open-source autonomous crypto trading agent designed to operate locally on a user's machine. The system interacts with the Binance **Spot trading API only** to execute trades based on user-defined goals such as profit targets, deadlines, and budgets.

The system is designed to:

- accept structured user instructions through a CLI
- evaluate feasibility of trading goals
- select suitable assets
- allocate capital safely
- generate trading signals
- validate and execute orders
- monitor positions
- exit trades based on profit targets, stop-loss, or deadlines
- detect external account changes
- maintain synchronized local state

CryptoGent prioritizes **deterministic safety controls** and treats LLM reasoning as **advisory only**.

---

# Core Design Principles

## 1. Spot Trading Only

CryptoGent must interact only with Binance **Spot API endpoints**.

Not supported:

- Futures
- Margin
- Options
- Lending / borrowing
- Withdrawals

---

## 2. Local Execution

CryptoGent runs entirely on the user’s machine.

- No central server
- API credentials stay local
- SQLite used for persistence

If the machine stops, the system stops.

---

## 3. Exchange as Source of Truth

The Binance account state is always authoritative.

The local database is only a **cached operational state**.

Any mismatch must be resolved by re-synchronizing with the exchange.

---

## 4. LLM Advisory Only

LLM outputs cannot execute trades directly.

All trading actions must pass through:

```

Deterministic Validation Layer
Risk Management Layer

```

before execution.

---

## 5. Safety First

Every trade must include:

- stop-loss
- validated order parameters
- balance verification
- exchange rule validation

---

# System Architecture

CryptoGent is structured as a **layered architecture**.

## Layers

1. CLI Interaction Layer  
2. Command and Input Processing Layer  
3. User Preferences and Configuration Layer  
4. Exchange Connection Layer  
5. Account State Synchronization Layer  
6. Local State, Persistence, and Recovery Layer  
7. Market Data Layer  
8. Feasibility Evaluation Layer  
9. Asset Selection Layer  
10. Capital Allocation Layer  
11. Strategy and Signal Layer  
12. LLM Advisory and Decision Support Layer  
13. Deterministic Validation Layer  
14. Risk Management Layer  
15. Order Execution Layer  
16. Position Management Layer  
17. Deadline and Exit Control Layer  
18. Monitoring and Re-evaluation Layer  
19. External Change Detection and Reconciliation Layer  
20. Audit, Logging, and Reporting Layer  

---

# Feature Set (MVP)

## Trading

- Goal-based trading
- Profit target
- Stop-loss
- Deadline-based exit
- Budget allocation
- Asset selection

---

## Safety

- deterministic validation
- exchange rule validation
- stop-loss requirement
- exposure limits
- minimum notional checks

---

## Monitoring

- continuous market monitoring
- position tracking
- exit triggers
- deadline control

---

## Synchronization

- balance synchronization
- order synchronization
- trade synchronization
- external change detection

---

## Persistence

- SQLite database
- account snapshots
- position state
- order history
- configuration

---

## CLI

Initial version uses **structured CLI input**, not natural language.

Example CLI options:

```

1. Start new trade
2. Show account balance
3. Show active trade
4. View order history
5. Change configuration
6. Pause monitoring
7. Exit

```

---

# Trading Flow

## Step 1 – User Input

CLI → Command Processing

User provides:

- profit target
- stop-loss
- deadline
- budget
- preferred asset (optional)

---

## Step 2 – Account Context

System retrieves:

- balances
- open orders
- asset holdings

---

## Step 3 – Market Context

System retrieves:

- current price
- recent candles
- volatility indicators
- liquidity metrics

---

## Step 4 – Feasibility Evaluation

System checks whether the requested target is realistic.

Possible outcomes:

- feasible
- feasible with warning
- high risk
- not feasible

---

## Step 5 – Asset Selection

System selects asset based on:

- volatility
- liquidity
- spread
- momentum
- market activity

---

## Step 6 – Capital Allocation

System determines trade capital:

- respect user budget
- respect account balance
- enforce minimum order rules

---

## Step 7 – Strategy Signal

Strategy layer determines:

- buy
- sell
- hold
- exit

---

## Step 8 – Optional LLM Advisory

LLM may provide:

- reasoning
- scenario interpretation
- alternative suggestions

Execution must not depend solely on LLM output.

---

## Step 9 – Deterministic Validation

Checks:

- balance availability
- symbol rules
- lot size
- minimum notional
- order validity

---

## Step 10 – Risk Management

Checks:

- stop-loss presence
- maximum exposure
- cooldown rules
- deadline constraints

---

## Step 11 – Order Execution

Orders sent through Binance Spot API.

---

## Step 12 – Position Management

System tracks:

- entry price
- quantity
- unrealized PnL

---

## Step 13 – Monitoring

System continuously monitors:

- price movement
- deadline
- stop-loss
- profit target

---

## Step 14 – Exit

Trade exits when:

- profit target reached
- stop-loss triggered
- deadline reached
- strategy invalidated

---

# Synchronization Model

Three synchronization types exist.

## Immediate Sync

Triggered after:

- order placement
- order cancellation
- trade completion

---

## Scheduled Sync

Runs periodically.

Example:

```

every 60 seconds

```

Used to detect:

- manual trades
- balance changes
- order updates

---

## Startup Sync

When the system starts:

1. fetch balances  
2. fetch open orders  
3. fetch recent trades  
4. rebuild local state  

---

# Data Persistence

SQLite database stores:

## Configuration

- user preferences
- default trading rules

---

## Account State

- asset balances
- locked funds
- valuation snapshot

---

## Trading State

- active positions
- goals
- stop-loss
- deadlines

---

## Orders

- order ID
- symbol
- price
- quantity
- status

---

## Logs

- decision summaries
- errors
- warnings
- execution history

---

# Implementation Steps

## Phase 1 – Foundation

1. Project structure
2. Layer modules
3. SQLite schema
4. configuration system
5. CLI entrypoint

---

## Phase 2 – Exchange Integration

6. Binance Spot API client
7. request signing
8. exchange rule retrieval
9. API error handling
10. connection testing

---

## Phase 3 – Local State

11. database schema
12. state manager
13. balance synchronization
14. order synchronization
15. startup recovery

---

## Phase 4 – Trade Input

16. CLI trade workflow
17. user configuration
18. command validation
19. trade request object

---

## Phase 5 – Market & Planning

20. market data retrieval
21. feasibility evaluation
22. asset selection
23. capital allocation
24. strategy signals

---

## Phase 6 – Safety

25. deterministic validation
26. risk management

---

## Phase 7 – Execution

27. order execution
28. position management
29. persistence updates

---

## Phase 8 – Monitoring

30. monitoring loop
31. exit control
32. re-evaluation triggers

---

## Phase 9 – Reliability

33. external change detection
34. reconciliation logic
35. crash recovery
36. restart tests

---

## Phase 10 – LLM Integration

37. context builder
38. advisory prompt
39. recommendation parser

---

## Phase 11 – Logging

40. audit logs
41. trade history
42. system reports

---

# Risk Factors

## State Drift

Local state may differ from exchange state.

Solution:

- continuous synchronization
- reconciliation logic

---

## Partial Order Fills

Orders may fill partially.

Solution:

- track filled quantities
- update positions correctly

---

## Manual User Actions

Users may trade outside the bot.

Solution:

- detect external changes
- reconcile state

---

## Machine Shutdown

System stops when machine stops.

Solution:

- startup synchronization
- state recovery

---

## Exchange Rules

Binance rules may reject invalid orders.

Solution:

- deterministic validation layer

---

## LLM Overconfidence

LLM may suggest unrealistic trades.

Solution:

- LLM advisory only
- deterministic validation
- risk controls

---

# Project Structure Guidance

Codex should structure the project around **layer boundaries**, not feature grouping.

Suggested modules:

```

cli/
config/
exchange/
sync/
state/
market/
feasibility/
asset_selection/
allocation/
strategy/
llm_advisory/
validation/
risk/
execution/
position/
exit_control/
monitoring/
reconciliation/
logging/

```

Each layer should expose a **clear interface**.

Layers must avoid circular dependencies.

---

# Future Enhancements

Possible future features:

- natural language input
- multi-asset portfolio trading
- advanced strategy plugins
- volatility-based stop-loss
- VPS deployment support
- performance analytics
- backtesting engine

---

# Final Note

CryptoGent should evolve as a **safe, deterministic trading engine first**, and only later incorporate advanced AI reasoning capabilities.

The priority is:

```

correctness
safety
state consistency
deterministic execution

```

before intelligence and automation.
```

