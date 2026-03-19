# Phase 1 – Foundation

This phase establishes the **core infrastructure** of CryptoGent.

The goal is to create a stable base architecture before implementing any exchange logic or trading features.

After Phase 1 the system should be able to:

- start from CLI
- load configuration
- initialize database
- create project structure
- initialize logging
- prepare module boundaries

No exchange interaction or trading logic should exist yet.

---

# Phase Scope

This phase implements the following steps from the implementation roadmap:

1. Project structure  
2. Layer modules  
3. SQLite schema  
4. Configuration system  
5. CLI entrypoint  

---

# Project Structure

The project must follow a **layer-based architecture** to ensure clear separation of concerns.

Suggested structure:

```

cryptogent/
│
├── cli/
│   ├── menu.py
│   └── commands.py
│
├── config/
│   ├── loader.py
│   └── settings.py
│
├── database/
│   ├── db.py
│   └── schema.py
│
├── state/
│   └── state_manager.py
│
├── exchange/
│
├── market/
│
├── strategy/
│
├── execution/
│
├── monitoring/
│
├── risk/
│
├── reconciliation/
│
├── llm/
│
├── logging/
│   └── logger.py
│
├── utils/
│   └── helpers.py
│
└── **main**.py

```

Only the following modules must be functional in Phase 1:

- CLI
- Config
- Database
- Logging

Other modules should exist as placeholders.

---

# Layer Module Initialization

Each layer should exist as a separate module directory.

Modules should not contain business logic yet.

Purpose of this step:

- ensure clear boundaries
- prevent circular dependencies
- prepare for future phases

Example empty modules:

```

exchange/
market/
strategy/
execution/
monitoring/
risk/
reconciliation/
llm/

```

---

# SQLite Database Setup

CryptoGent uses **SQLite** as the local persistence engine.

Reasons:

- lightweight
- embedded
- zero external dependency
- suitable for local autonomous agents

Database file example:

```

cryptogent.db

```

Location configurable through config.

---

# Initial Database Schema

Only minimal tables should be created in Phase 1.

### system_state

Stores basic system metadata.

```

id
created_at
last_start_time

```

---

### balances

Stores account balances snapshot.

```

asset
free
locked
timestamp

```

---

### orders

Stores order history.

```

order_id
symbol
side
price
quantity
status
timestamp

```

---

### positions

Stores active positions.

```

symbol
entry_price
quantity
target_profit
stop_loss
deadline
status

```

---

### logs

Stores system logs.

```

timestamp
level
component
message

```

---

# Configuration System

The configuration system must support loading runtime settings.

Configuration should be stored in:

```

config.yaml

````

Example configuration:

```yaml
mode: testnet

exchange:
  name: binance
  base_urls:
    mainnet: https://api.binance.com
    testnet: https://testnet.binance.vision

database:
  path: ./cryptogent.db

trading:
  default_quote_asset: USDT
  monitoring_interval: 60
  max_position_percent: 25
````

---

# Environment Modes

CryptoGent must support two execution environments.

```
testnet
mainnet
```

Default environment should be:

```
testnet
```

This mode will determine which Binance endpoint is used.

---

# Configuration Loader

A configuration loader should:

* read YAML configuration
* validate required fields
* provide default values
* expose configuration globally

Example interface:

```
ConfigLoader.load()
```

Returns:

```
Config object
```

---

# Logging System

A centralized logging system must be initialized during startup.

Logs should be written to:

* console
* log file
* database

Example log format:

```
[2026-03-14 12:00:00] INFO System: CryptoGent started
```

Logs must include:

```
timestamp
log level
component
message
```

---

# CLI Entrypoint

CryptoGent should start using:

```
python -m cryptogent
```

The CLI should initialize the system and show a command menu.

Example CLI menu:

```
1. Start trade
2. Show balances
3. Show open position
4. View logs
5. Configuration
6. Exit
```

During Phase 1 these commands can return placeholder responses.

---

# System Startup Flow

When the system starts:

```
Start CLI
   ↓
Load configuration
   ↓
Initialize logging
   ↓
Initialize database
   ↓
Initialize state manager
   ↓
Display CLI menu
```

---

# Dependency Recommendations

Minimal dependencies should be used.

Recommended packages:

```
requests
pydantic
pyyaml
rich
```

Avoid unnecessary dependencies.

---

# Security Considerations

API credentials must **never be hardcoded**.

Use environment variables.

Example `.env` file:

```
BINANCE_API_KEY=
BINANCE_API_SECRET=
```

These should be loaded during startup.

---

# Deliverables

Phase 1 is complete when:

* project structure is created
* configuration loader works
* database initializes
* CLI launches successfully
* logging system works

No exchange communication or trading logic should exist yet.

---

# Success Criteria

Phase 1 is considered successful when the system can:

* start via CLI
* load configuration
* initialize database
* create logs
* display command menu

```
