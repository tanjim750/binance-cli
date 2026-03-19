````markdown
# Phase 4 – Trade Input

This phase introduces the **user-facing trade input workflow** of CryptoGent.

The goal of this phase is to allow a user to create a structured trade request through the CLI without using natural language processing.

This phase should convert manual CLI selections and input values into a normalized internal trade request object that later phases can use for planning, validation, and execution.

No market analysis or order execution should happen in this phase.  
This phase is only responsible for **collecting, validating, normalizing, and storing user trade input**.

---

# Phase Scope

This phase implements the following steps from the implementation roadmap:

16. CLI trade workflow  
17. user configuration  
18. command validation  
19. trade request object  

---

# Core Objective

After completing Phase 4, CryptoGent should be able to:

- guide the user through a structured CLI trade setup flow
- collect trade parameters
- apply configured defaults where appropriate
- validate input values
- build a normalized trade request object
- optionally persist pending trade requests for later phases

This phase should become the main bridge between:

- CLI Interaction Layer
- Command and Input Processing Layer
- User Preferences and Configuration Layer

---

# Input Model Philosophy

CryptoGent should start with **structured manual input**, not natural language.

Reasons:

- easier to implement
- lower ambiguity
- safer for trading systems
- simpler validation
- easier debugging
- better Codex implementation path

The CLI should behave like a controlled wizard or menu flow.

---

# CLI Trade Workflow

The trade workflow should begin from the main menu.

Example menu:

```text
1. Start trade
2. Show balances
3. Show open position
4. View logs
5. Configuration
6. Exit
````

When the user selects:

```text
Start trade
```

the CLI should enter a guided input flow.

---

# Trade Input Fields

The CLI trade flow should collect the following fields.

## Required fields

* target profit percent
* stop-loss percent
* deadline
* budget mode

## Optional fields

* preferred asset
* exit asset
* notes or label

Depending on configuration, some fields may have defaults.

---

# Recommended Trade Input Sequence

Suggested order:

1. select action type
2. choose autonomy mode if applicable
3. enter target profit percent
4. enter stop-loss percent
5. enter deadline
6. choose budget mode
7. enter budget if manual
8. optionally enter preferred asset
9. confirm request

For MVP, autonomy mode can remain simple if it is not yet used by later phases.

---

# User Configuration Support

This phase must integrate with user configuration so defaults can be applied.

Examples of defaults:

* default exit asset = USDT
* default max position percent
* default monitoring interval
* default risk preferences
* default environment = testnet

Trade input should read these defaults and apply them when the user does not override them.

---

# Configuration-Driven Input

Example behavior:

If the config contains:

```yaml
trading:
  default_exit_asset: USDT
  default_budget_mode: manual
  default_stop_loss_percent: 2
```

then the CLI may display:

```text
Exit asset [default: USDT]:
Stop-loss percent [default: 2]:
```

The user can accept or override these values.

---

# Budget Mode

Budget input should support at least two modes.

## Manual Budget

User explicitly enters the amount to use.

Example:

```text
Use 100 USDT
```

## Suggestion / Auto Budget

The user allows the system to determine a suitable budget later based on:

* account balance
* configuration
* risk limits

At this phase, the CLI only stores the selected mode.
Budget calculation itself belongs to later phases.

---

# Deadline Input

Deadline must be collected in a structured and validated way.

Recommended supported formats:

* number of hours
* number of days
* absolute datetime (optional later)

For MVP, simpler is better.

Recommended first version:

```text
deadline in hours
```

or

```text
deadline in days
```

Example:

```text
Enter deadline in hours: 24
```

This avoids timezone and parsing complexity early on.

---

# Asset Input

Preferred asset should be optional.

Example:

```text
Preferred asset (optional, e.g. BTCUSDT):
```

If empty:

* the system will later choose the asset automatically

If provided:

* the symbol should be validated for format only in this phase
* actual exchange validity can be checked in later phases

---

# Input Validation

This phase must implement **command validation** for all user-provided fields.

Validation should happen before a trade request object is created.

---

# Required Validation Rules

## Profit Target Validation

* must be numeric
* must be greater than zero
* should remain within a reasonable upper bound

Example valid values:

```text
1
2.5
5
```

---

## Stop-Loss Validation

* must be numeric
* must be greater than zero
* should not exceed a reasonable cap
* should generally be lower than target profit for normal cases

This phase may warn instead of rejecting in some edge cases, depending on policy.

---

## Deadline Validation

* must be numeric if using hours/days mode
* must be greater than zero
* must not be absurdly large for MVP

---

## Budget Validation

If manual budget is selected:

* must be numeric
* must be greater than zero

Do not check actual exchange balance in this phase.
That belongs to later phases.

---

## Asset Validation

If asset is provided:

* normalize to uppercase
* validate basic symbol format
* reject obviously malformed input

Do not fully validate against Binance exchange info yet unless already available and intentionally reused.

---

# Command Validation Responsibility

The command validation logic should be separated from raw CLI input.

Recommended flow:

```text
CLI Input
   ↓
Normalize raw values
   ↓
Validate fields
   ↓
Build trade request object
```

Validation should not be mixed directly into menu rendering code.

---

# Trade Request Object

The main output of this phase is a normalized **Trade Request Object**.

Suggested structure:

```text
request_id
target_profit_percent
stop_loss_percent
deadline_hours
budget_mode
budget_amount
preferred_asset
exit_asset
environment
created_at
status
```

Possible status values:

```text
draft
validated
cancelled
```

This object becomes the input to later planning phases.

---

# Example Trade Request

Example normalized object:

```text
request_id: tr_001
target_profit_percent: 4
stop_loss_percent: 2
deadline_hours: 24
budget_mode: manual
budget_amount: 100
preferred_asset: BTCUSDT
exit_asset: USDT
environment: testnet
status: validated
```

If no preferred asset is given:

```text
preferred_asset: null
```

---

# Persistence of Trade Requests

This phase may optionally persist trade requests to the local database.

Recommended table:

## `trade_requests`

Fields:

```text
id
request_id
target_profit_percent
stop_loss_percent
deadline_hours
budget_mode
budget_amount
preferred_asset
exit_asset
status
created_at
updated_at
```

This is useful for:

* auditability
* recovery
* debugging
* future workflow continuity

If persistence is added, it should remain simple.

---

# CLI Confirmation Step

Before finalizing a request, the CLI should show a confirmation summary.

Example:

```text
Trade Request Summary
- Target Profit: 4%
- Stop-Loss: 2%
- Deadline: 24 hours
- Budget Mode: manual
- Budget Amount: 100 USDT
- Preferred Asset: BTCUSDT
- Exit Asset: USDT

Confirm? [y/n]
```

If the user rejects the summary:

* return to edit flow
* or cancel request creation

---

# Error Handling

This phase must handle:

* invalid numeric input
* missing required input
* malformed asset symbol
* unsupported budget mode
* invalid menu selection
* cancelled user input

Errors should be:

* shown clearly in CLI
* logged
* non-destructive
* recoverable without restarting the app

---

# Logging Requirements

Important events to log:

* trade workflow started
* input validation failed
* trade request created
* trade request cancelled
* trade request persisted

Example logs:

```text
[INFO] TradeInput: New trade workflow started
[WARN] TradeInput: Invalid stop-loss value entered
[INFO] TradeInput: Trade request tr_001 created
```

---

# Suggested Modules

Suggested files for this phase:

```text
cli/
  trade_flow.py
  prompts.py
  validators.py

config/
  preferences.py

state/
  trade_request_store.py

models/
  trade_request.py
```

Possible responsibilities:

## `trade_flow.py`

* main CLI trade setup flow

## `prompts.py`

* reusable input prompts

## `validators.py`

* field validation helpers

## `preferences.py`

* user defaults resolution

## `trade_request_store.py`

* save/load pending trade requests

## `trade_request.py`

* trade request model

---

# Deliverables

Phase 4 is complete when:

* the CLI can guide the user through a trade setup flow
* input values are validated
* defaults are applied from configuration
* a normalized trade request object is created
* optional persistence of trade requests works
* clear confirmation and cancellation behavior exists

---

# Success Criteria

Phase 4 is successful when the system can:

* collect structured trade input through CLI
* validate all required fields
* generate a valid trade request object
* handle invalid input gracefully
* store or pass the request cleanly to later planning phases

```
```
