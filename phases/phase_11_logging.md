````markdown
# Phase 11 – Logging and Reporting

This phase introduces the **auditability and observability layer** of CryptoGent.

By this stage, the system should already be able to:

- create trade requests
- build trade plans
- validate trades
- enforce risk policies
- execute orders
- manage positions
- monitor trades
- reconcile exchange state
- integrate LLM advisory decisions

Phase 11 ensures that all critical system behavior becomes **traceable, reviewable, and analyzable**.

This phase focuses on building a structured logging and reporting system that allows developers and users to understand:

- what the system did
- why it made decisions
- how trades performed
- what errors occurred

---

# Phase Scope

This phase implements the following steps from the implementation roadmap:

40. audit logs  
41. trade history  
42. system reports  

---

# Core Objective

After completing Phase 11, CryptoGent should be able to:

- record all major system actions
- maintain a complete trade history
- generate performance summaries
- provide clear diagnostic logs
- support debugging and analysis
- expose structured reports through CLI

This phase is essential for:

- transparency
- debugging
- reliability verification
- performance evaluation

---

# Layers Covered in This Phase

This phase activates the following layer:

20. Audit, Logging, and Reporting Layer  

Supporting layers involved:

All operational layers interact with this phase because all system events must be recorded.

---

# Logging Philosophy

CryptoGent must treat logging as a **first-class feature**, not an afterthought.

Every meaningful action should produce a log event.

Logs must help answer questions such as:

- why a trade was executed
- why a trade was rejected
- why a position closed
- why reconciliation occurred
- why automation paused
- what the system observed during monitoring

Logs should be structured and readable.

---

# Log Categories

Logs should be organized into several categories.

---

## System Logs

These describe general application events.

Examples:

- system startup
- configuration loaded
- monitoring started
- recovery executed
- automation paused

---

## Trading Logs

These describe trade-related decisions.

Examples:

- trade request created
- trade plan generated
- feasibility evaluation result
- asset selection decision
- capital allocation result
- signal generated
- validation result
- risk approval

---

## Execution Logs

These describe order interactions with Binance.

Examples:

- order submitted
- order filled
- order partially filled
- order rejected
- execution uncertain
- execution reconciled

---

## Monitoring Logs

These describe monitoring behavior.

Examples:

- monitoring cycle executed
- current price observed
- PnL calculation
- stop-loss proximity
- target progress
- exit trigger detected

---

## Reliability Logs

These describe system integrity and recovery events.

Examples:

- external change detected
- reconciliation started
- reconciliation completed
- restart recovery executed
- automation paused

---

## AI Advisory Logs

These describe LLM interactions.

Examples:

- advisory request sent
- advisory response received
- recommendation accepted
- recommendation rejected

---

# Log Format

Logs should follow a consistent format.

Example format:

```text
[timestamp] [level] [component] message
````

Example:

```text
[2026-03-15 10:22:11] [INFO] [Execution] Order submitted for SOLUSDT
```

Recommended levels:

```text
DEBUG
INFO
WARN
ERROR
CRITICAL
```

---

# Structured Logging

Logs should also support structured fields where possible.

Example:

```text
symbol=SOLUSDT side=BUY quantity=1.742
```

Example structured log:

```text
[INFO] Execution: Order filled symbol=SOLUSDT price=103.28 quantity=1.742
```

Structured logs make debugging and analysis easier.

---

# Log Storage

Logs should be stored in two forms:

---

## File Logs

Runtime logs written to local log files.

Example directory:

```text
logs/
  cryptogent.log
```

These logs help with debugging and operational visibility.

---

## Database Logs

Critical events should also be stored in SQLite.

This allows historical querying and reporting.

Example tables may include:

```text
audit_logs
trade_history
system_events
```

---

# Audit Logs

Audit logs record **important system actions**.

Examples of audit events:

* trade request creation
* trade validation result
* trade approval
* trade rejection
* order execution
* position opening
* position closing
* reconciliation event

Audit logs should include:

```text
event_type
component
summary
details
timestamp
```

Example:

```text
event_type: trade_execution
component: execution_layer
summary: BUY order executed for SOLUSDT
details: quantity=1.742 price=103.28
```

---

# Trade History

Trade history provides a persistent record of all completed trades.

It allows users to review trading performance.

---

# Trade History Fields

Suggested table structure:

```text
trade_id
symbol
side
entry_price
exit_price
quantity
target_profit_percent
stop_loss_percent
entry_time
exit_time
profit_loss
profit_loss_percent
reason_closed
```

Possible `reason_closed` values:

```text
target_reached
stop_loss_hit
deadline_exit
manual_close
reconciliation_close
```

---

# Trade History Example

Example record:

```text
symbol: SOLUSDT
entry_price: 103.28
exit_price: 107.45
quantity: 1.742
profit_loss_percent: 4.04
reason_closed: target_reached
```

This information helps evaluate system performance.

---

# System Reports

System reports provide summarized insights about trading behavior.

Reports should be generated from stored logs and trade history.

Reports should be accessible via CLI.

---

# Types of Reports

Examples of useful reports include:

---

## Trading Performance Report

Metrics may include:

* total trades
* successful trades
* losing trades
* average profit
* average loss
* win rate

Example:

```text
Total Trades: 12
Winning Trades: 8
Losing Trades: 4
Win Rate: 66%
Average Profit: 3.8%
Average Loss: -2.1%
```

---

## Risk Report

Shows how the system behaved relative to risk policies.

Examples:

* average position size
* stop-loss hit frequency
* risk rejections

---

## Monitoring Report

Shows monitoring behavior.

Examples:

* monitoring cycles executed
* exit triggers detected
* re-evaluation events

---

## Reliability Report

Shows system stability events.

Examples:

* reconciliation events
* restart recoveries
* automation pauses

---

# CLI Report Commands

Example commands:

```text
show trade history
show system logs
show performance report
show risk report
show reconciliation events
```

Example output:

```text
Trade History
-------------
SOLUSDT BUY  entry=103.28 exit=107.45  pnl=+4.04%
ETHUSDT BUY  entry=2810.15 exit=2760.32 pnl=-1.77%
```

---

# Reporting Calculations

Reports should compute metrics from stored trade history.

Example calculations:

---

## Profit/Loss

```text
pnl = (exit_price - entry_price) * quantity
```

---

## Profit Percent

```text
profit_percent = ((exit_price - entry_price) / entry_price) * 100
```

---

## Win Rate

```text
win_rate = winning_trades / total_trades
```

---

# Log Rotation

The system should prevent log files from growing indefinitely.

Basic log rotation should be implemented.

Example policy:

```text
rotate log file after 10 MB
keep last 5 log files
```

This keeps disk usage controlled.

---

# Error Logging

All exceptions must be logged clearly.

Example:

```text
[ERROR] Execution: Binance order failed error=INSUFFICIENT_BALANCE
```

Critical errors should include stack traces when possible.

---

# Suggested Modules

Suggested files for this phase:

```text
logging/
  logger.py
  audit_logger.py
  event_recorder.py

reports/
  performance_report.py
  trade_report.py
  risk_report.py
  monitoring_report.py

models/
  audit_event.py
  trade_record.py
```

---

# Module Responsibilities

---

## `logger.py`

Central logging configuration.

---

## `audit_logger.py`

Records important system events.

---

## `event_recorder.py`

Writes events to database tables.

---

## `performance_report.py`

Calculates trading performance statistics.

---

## `trade_report.py`

Displays trade history.

---

## `risk_report.py`

Analyzes risk policy outcomes.

---

# Deliverables

Phase 11 is complete when:

* structured logging exists across all modules
* audit events are stored in the database
* trade history is recorded
* system reports can be generated
* CLI commands expose reports
* logs support debugging and performance analysis

---

# Success Criteria

Phase 11 is successful when the system can:

* record all critical actions
* maintain full trade history
* generate meaningful performance reports
* expose logs and diagnostics through CLI
* provide transparency into system behavior

```
```
