````markdown
# Phase 5 – Market and Planning

This phase introduces the first real **planning intelligence** of CryptoGent.

By this stage, the system should already be able to:

- collect a structured trade request from CLI
- load configuration and local state
- connect to Binance
- retrieve exchange metadata
- persist synchronized account context

Phase 5 uses that foundation to decide whether a trade request is worth pursuing and how it should be planned.

This phase should **not execute any order yet**.  
It should only produce a structured and well-reasoned **trade plan**.

---

# Phase Scope

This phase implements the following steps from the implementation roadmap:

20. market data retrieval  
21. feasibility evaluation  
22. asset selection  
23. capital allocation  
24. strategy signals  

---

# Core Objective

After completing Phase 5, CryptoGent should be able to:

- retrieve relevant market data
- evaluate whether the requested trade goal is realistic
- select a suitable asset if the user did not specify one
- determine how much capital should be allocated
- generate an initial strategy signal
- produce a structured trade planning output for later validation and execution

---

# Layers Covered in This Phase

This phase activates the following layers:

7. Market Data Layer  
8. Feasibility Evaluation Layer  
9. Asset Selection Layer  
10. Capital Allocation Layer  
11. Strategy and Signal Layer  

Supporting layers involved:

4. Exchange Connection Layer  
5. Account State Synchronization Layer  
6. Local State, Persistence, and Recovery Layer  
3. User Preferences and Configuration Layer  
20. Audit, Logging, and Reporting Layer  

---

# Planning Philosophy

The system should not attempt to trade merely because the user requested a profit target.

Instead, it should first determine:

- whether the goal is realistic
- whether market conditions support the request
- whether enough balance exists
- whether the selected or candidate assets are suitable
- whether a safe trade plan can be constructed

If the answer is no, the phase should stop with a clear planning result and warning.

---

# Market Data Retrieval

This phase must retrieve the market data required for planning.

The data should come from Binance Spot public endpoints.

Suggested sources:

## Current Price

```text
GET /api/v3/ticker/price
````

Used for:

* latest symbol price
* rough allocation calculations

---

## 24hr Statistics

```text
GET /api/v3/ticker/24hr
```

Used for:

* volume
* recent price change
* high / low range
* basic momentum

---

## Candlestick Data

```text
GET /api/v3/klines
```

Used for:

* recent price movement
* short trend checks
* volatility approximation

For MVP, use a small and practical candle window.

Example:

```text
interval = 1h
limit = 24
```

or

```text
interval = 15m
limit = 48
```

Keep it simple.

---

# Market Data Scope

At this stage, market data retrieval should focus only on:

* the user-specified asset, if given
* a small candidate universe, if no asset is given

Do not fetch the entire exchange unnecessarily.

Suggested initial candidate universe:

```text
BTCUSDT
ETHUSDT
SOLUSDT
BNBUSDT
XRPUSDT
```

This keeps the MVP manageable.

---

# Feasibility Evaluation

The feasibility layer determines whether the requested trade goal is realistic enough to continue planning.

It should evaluate the relationship between:

* target profit percent
* deadline
* recent volatility
* recent market movement
* basic liquidity

---

# Feasibility Inputs

Inputs include:

* trade request
* target profit percent
* stop-loss percent
* deadline
* preferred asset if provided
* market stats
* recent candles

---

# Feasibility Categories

The result should be one of:

```text
feasible
feasible_with_warning
high_risk
not_feasible
```

These categories should be used consistently throughout the project.

---

# Feasibility Logic

The MVP logic should remain deterministic and simple.

Examples of useful checks:

## Profit vs Volatility

If requested profit is much larger than recent normal movement for the selected timeframe, mark it as risky or not feasible.

## Deadline Pressure

If the deadline is too short relative to the target, increase risk severity.

## Liquidity Support

If the asset is illiquid or spread is poor, mark the plan as risky or reject it.

## Budget Viability

If the budget is too small for the symbol’s minimum notional or practical execution, reject the plan.

---

# Example Feasibility Outcomes

Example 1:

```text
Request:
target profit = 3%
deadline = 3 days
symbol = BTCUSDT
```

Possible result:

```text
feasible
```

Example 2:

```text
Request:
target profit = 25%
deadline = 1 day
symbol = BTCUSDT
```

Possible result:

```text
not_feasible
```

Example 3:

```text
Request:
target profit = 8%
deadline = 24 hours
symbol = SOLUSDT
```

Possible result:

```text
feasible_with_warning
```

---

# Feasibility Output

The phase should produce a structured feasibility result.

Suggested fields:

```text
status
warning_flags
summary
reason_codes
```

Example:

```text
status: feasible_with_warning
warning_flags:
  - short_deadline
  - elevated_target
summary: Requested target may be achievable but current volatility makes execution risky.
```

---

# Asset Selection

The asset selection layer determines which asset should be used for planning and later execution.

If the user specified an asset:

* treat it as the primary candidate
* still validate whether it is suitable

If the user did not specify an asset:

* choose one from the allowed candidate universe

---

# Asset Selection Inputs

Inputs include:

* user preferred asset
* feasibility context
* current prices
* 24hr volume
* 24hr change
* candle-derived momentum
* basic spread or liquidity indicators
* available balance context if relevant

---

# Asset Selection Criteria

Use a simple scoring model.

Suggested factors:

* liquidity
* volatility
* recent momentum
* market activity
* compatibility with the exit asset
* quote asset support

Initial MVP should strongly prefer:

```text
USDT quote pairs
```

---

# Asset Selection Constraints

The selected asset must satisfy:

* Binance Spot symbol exists
* symbol is trading
* quote asset is supported
* symbol is not obviously illiquid
* trade budget can realistically be used on it

---

# Asset Selection Output

Suggested output structure:

```text
selected_symbol
candidate_symbols
selection_score
selection_summary
```

Example:

```text
selected_symbol: SOLUSDT
candidate_symbols:
  - SOLUSDT
  - ETHUSDT
  - BTCUSDT
selection_score: 0.81
selection_summary: Strong recent momentum and sufficient liquidity for the requested target horizon.
```

---

# Capital Allocation

The capital allocation layer determines how much balance should be committed to the trade.

This phase should remain conservative.

---

# Capital Allocation Inputs

Inputs include:

* user budget mode
* user budget amount if manual
* synchronized account balances
* selected symbol
* configuration limits
* exchange minimum notional
* feasibility result

---

# Allocation Modes

At minimum, support:

## Manual Budget

Use the user-specified amount, but do not exceed:

* available balance
* configured maximum position size
* exchange minimum and valid order requirements

## Suggested Budget

If no budget is provided, the system should recommend a conservative amount based on:

* available balance
* config max position size
* symbol suitability
* feasibility status

---

# Capital Allocation Rules

The allocation logic should enforce:

* never exceed available balance
* never exceed configured max position percent
* respect minimum notional
* respect step size implications later
* reduce allocation if feasibility result is risky

If the feasibility result is:

```text
high_risk
```

the system may either:

* reduce the allocation
* or stop the plan entirely depending on policy

---

# Example Allocation

Example:

```text
Available USDT balance = 1000
Config max position size = 25%
Manual budget = 400
```

Final allowed budget:

```text
250 USDT
```

because config limit is stricter.

---

# Allocation Output

Suggested output structure:

```text
requested_budget
approved_budget
allocation_reason
valuation_asset
```

Example:

```text
requested_budget: 400
approved_budget: 250
allocation_reason: Reduced to configured max position size.
valuation_asset: USDT
```

---

# Strategy Signals

The strategy and signal layer in this phase should generate the initial directional trade signal.

This should be simple and deterministic.

The goal is not to build a sophisticated trading strategy yet, but to create a clean planning output.

---

# Strategy Inputs

Inputs include:

* selected symbol
* market data
* feasibility result
* allocated capital
* target profit
* stop-loss
* deadline

---

# Signal Types

Allowed signals:

```text
buy
sell
hold
exit
```

For the MVP initial planning phase, the most common outputs will likely be:

```text
buy
hold
```

because the user is generally initiating a fresh trade setup.

---

# Simple MVP Signal Logic

Examples:

## Buy Signal

Generate buy when:

* momentum is acceptable
* liquidity is acceptable
* feasibility is not rejected
* price action is not obviously weak by current simple rules

## Hold Signal

Generate hold when:

* conditions are unclear
* momentum is weak
* feasibility is borderline
* no strong entry case exists

## Exit Signal

This can remain minimal in this phase because this phase is about planning a new trade, not managing an active one.

---

# Strategy Output

Suggested structure:

```text
signal
confidence
signal_reason
```

Example:

```text
signal: buy
confidence: 0.73
signal_reason: Positive recent momentum with acceptable volatility and sufficient liquidity.
```

---

# Final Trade Plan Output

At the end of Phase 5, the system should produce a structured **Trade Plan**.

Suggested structure:

```text
request_id
selected_symbol
feasibility_status
feasibility_summary
approved_budget
signal
signal_confidence
target_profit_percent
stop_loss_percent
deadline_hours
planning_status
```

Example:

```text
request_id: tr_001
selected_symbol: SOLUSDT
feasibility_status: feasible_with_warning
feasibility_summary: Target is possible but timeline is aggressive.
approved_budget: 180
signal: buy
signal_confidence: 0.73
target_profit_percent: 4
stop_loss_percent: 2
deadline_hours: 24
planning_status: ready_for_validation
```

---

# Persistence

This phase may persist trade planning results locally.

Suggested table:

## `trade_plans`

Fields:

```text
id
request_id
selected_symbol
feasibility_status
feasibility_summary
approved_budget
signal
signal_confidence
planning_status
created_at
updated_at
```

This is useful for:

* audit trail
* recovery
* debugging
* later execution phases

---

# CLI Behavior

After planning completes, the CLI should show a planning summary.

Example:

```text
Trade Planning Summary
- Symbol: SOLUSDT
- Feasibility: feasible_with_warning
- Budget Approved: 180 USDT
- Signal: BUY
- Confidence: 0.73
- Note: Timeline is aggressive.

Continue to validation and safety phase? [y/n]
```

This phase should stop before actual execution.

---

# Error Handling

The phase must handle:

* missing market data
* invalid or unavailable symbol
* insufficient balance for requested budget
* no suitable asset found
* not feasible trade request
* exchange metadata missing
* market data retrieval failure

Errors must:

* be logged
* be shown clearly to CLI
* stop the planning flow safely

# Phase 5.X – Additional Considerations and Deterministic Rules

> This section **extends Phase 5** and must be considered part of the specification.  
> It **does not modify any previous content** of Phase 5.  
> These rules clarify behavior, ensure deterministic planning, and eliminate ambiguous implementation decisions.

---

## 5.X Planning Boundary (No Execution Yet)

Phase 5 is **strictly planning-only**.

During this phase the system **must NOT**:

- place orders
- cancel orders
- amend orders
- submit test orders
- assume order fills
- lock or reserve balances
- create any exchange side effects

Phase 5 only produces a **trade plan**.

### Allowed Outputs

- feasibility decision  
  - `feasible`
  - `feasible_with_warning`
  - `high_risk`
  - `not_feasible`
- candidate symbol ranking
- selected asset
- approved budget
- estimated quantity
- rules snapshot
- signal summary
- warnings and rejection reasons

### Explicitly Not Allowed

- sending orders to exchange
- cancelling orders
- modifying orders
- simulating committed balances
- triggering execution logic

Execution happens only in **later phases** after additional validation.

---

## 5.X Deterministic Planning Pipeline

Phase 5 must always follow this **fixed pipeline**.

```

1. Load trade_request
2. Refresh exchange rules (exchangeInfo)
3. Refresh required market data
4. Run freshness checks
5. Run consistency checks
6. Run feasibility logic
7. Run asset selection (if symbol not fixed)
8. Run capital allocation
9. Run signal generation
10. Produce final trade plan
11. Persist trade plan
12. Write audit logs

```

Pipeline order **must not change** unless explicitly documented.

If an earlier stage fails with a **hard stop**, later stages must not run except to generate a diagnostic summary.

---

## 5.X Market Data Retrieval — Mainnet vs Testnet Policy

Market data must follow an explicit environment policy.

### Recommended Policy

Planning may use **mainnet public market data** even if eventual execution will occur on **testnet**.

Reason:

- testnet market data is often incomplete
- testnet prices may be unrealistic
- planning quality improves with real liquidity data

### Required Metadata

If planning uses mainnet data but execution targets testnet, the plan must store:

```

market_data_environment = mainnet_public
execution_environment = testnet

```

### Alternative Strict Policy

A stricter implementation may forbid mixing environments, but this is **not recommended** for planning accuracy.

---

## 5.X Minimum Market Data Scope

Phase 5 requires the following datasets at minimum:

- `exchangeInfo` (symbol rules)
- current price / ticker price
- 24h statistics
- candle data (klines)

Optional future sources:

- best bid/ask (recommended for spread checks)
- order book snapshot
- recent trades

### MVP Recommendation (Deterministic Baseline)

For MVP planning, strongly prefer the following deterministic baseline so feasibility/scoring is reproducible and does not depend on ad-hoc heuristics:

* **Candles:** `interval = 5m`, `count = 288` (approx. 24 hours)
* **Spread input (optional but recommended):** use best bid/ask when available  
  - If best bid/ask is not available, spread-based checks must be skipped and the plan must record:
    - `spread_check = skipped_missing_bid_ask`
    - `feasibility_downgrade = feasible_with_warning` (or `high_risk`, depending on policy)

### Minimum Required Fields

Each plan must record:

- symbol
- reference price
- 24h quote volume
- 24h high
- 24h low
- candle interval
- candle count
- latest candle close
- market data timestamp
- rules timestamp
- whether bid/ask was available (for spread checks)

---

## 5.X Freshness Requirements

Market data must satisfy freshness thresholds.

| Dataset | Maximum Age |
|------|------|
| ticker price | 15 seconds |
| 24h stats | 60 seconds |
| candles | latest closed candle available |
| exchange rules | 24 hours |
| bid/ask (if used) | 15 seconds |

### Candle Freshness Rule

Example:

- If interval = `1m`, last closed candle must be within **2 minutes**
- If interval = `5m`, last closed candle must be within **10 minutes**

### Failure Behavior

- severely stale data → **hard stop**
- slightly stale but usable → **warning**

---

## 5.X Market Data Consistency Checks

Before planning, market data must pass consistency validation.

### Default Checks

- ticker price vs latest candle close difference ≤ **1%**
- 24h high ≥ 24h low
- current price must be positive
- candle timestamps strictly increasing
- required candle count satisfied

### Consistency Failure Handling

| Condition | Action |
|------|------|
missing rules | hard stop |
missing price | hard stop |
corrupt candles | hard stop |
ticker vs candle difference >1% | warning |
difference >3% | hard stop |

---

## 5.X Feasibility Logic — Deterministic Threshold Table

To avoid subjective decisions, the following **default thresholds** apply.

| Metric | Threshold | Behavior |
|------|------|------|
Max spread (requires bid/ask) | 0.50% | warning |
Extreme spread (requires bid/ask) | 1.00% | not_feasible |
Min 24h volume | 5,000,000 quote | warning |
Reject liquidity | 1,000,000 quote | not_feasible |
Volatility warning | 3% | feasible_with_warning |
Volatility high risk | 5% | high_risk |
Volatility reject | 8% | not_feasible |
Deadline warning | > 24 hours | warning |
Deadline high risk | > 168 hours | high_risk |
Fee buffer | 0.25% | reserved |
Safety buffer | 1% | optional reserve |

---

## 5.X Feasibility Categories

### feasible

All checks pass with healthy liquidity and stable volatility.

### feasible_with_warning

Tradable but concerns exist.

Examples:

- moderate volatility
- slightly elevated spread
- borderline liquidity

### high_risk

Technically tradable but risk elevated.

Examples:

- strong volatility
- tight rounding margin
- aggressive deadline

### not_feasible

Hard constraint failed.

Examples:

- missing rules
- symbol not trading
- min-notional failure
- severe liquidity issues
- corrupted data

---

## 5.X Hard Stop vs Warning Policy

### Hard Stop

Must return `not_feasible`.

Examples:

- missing exchange filters
- symbol not TRADING
- missing market data
- corrupted candle sequence
- wrong quote asset
- min-notional failure after rounding
- extreme spread
- severe liquidity shortage
- severe data staleness

### Warning Only

Plan still allowed.

Examples:

- volatility slightly high
- spread slightly elevated
- small data staleness
- minor price inconsistency

### High Risk Escalation

Plan allowed but flagged.

Examples:

- high volatility
- borderline rounding viability
- unstable market conditions

---

## 5.X Budget Mode Interaction (`budget_mode=auto`)

Phase 4 may specify:

```

budget_mode = auto

```

This means the system must compute the **approved budget in Phase 5**.

### Auto Budget Calculation

```

approved_budget =
min(
available_balance × risk_cap_pct,
configured_max_trade_budget,
request_max_budget_if_any
)

```

Then apply:

- fee buffer
- safety buffer

### Balance Source of Truth (Required)

For `budget_mode=auto`, the planner must determine `available_balance` from a **fresh signed account fetch** at planning time.

Fallback (only if exchange fetch fails):

* use cached balances from local state
* mark the plan as `feasible_with_warning` or `high_risk`
* add warning reason code:
  - `balance_source_cache_only`

### Default Limits

| Parameter | Default |
|------|------|
risk_cap_pct | 5% |
fee buffer | 0.25% |
safety buffer | 1% |

### Auto Budget Failure

If computed budget cannot meet **min-notional after rounding**, planning must return:

```

not_feasible
reason = auto_budget_insufficient_after_rounding

```

---

## 5.X Lot Size and Min-Notional Handling

Budget validation must simulate order sizing.

### Procedure

```

raw_quantity = usable_budget / price
rounded_quantity = round_down_to_step_size(raw_quantity)

```

Then validate:

- quantity ≥ minQty
- notional ≥ minNotional
- quantity ≤ maxQty

### Important Rule

If rounding causes violation of min-notional:

```

category = not_feasible
reason = lot_size_rounding_failure

```

---

## 5.X Asset Selection — Universe and Exclusions

### Universe Strategy

Prefer **allowlist-first**.

Example config:

```

candidate_symbols = ["BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT"]

```

If empty → fallback to **top liquid USDT pairs**.

### Hard Exclusions

Immediately reject symbols with:

- status ≠ TRADING
- wrong quote asset
- missing LOT_SIZE
- missing MIN_NOTIONAL
- leveraged tokens
- extremely low liquidity
- stale data
- symbols outside configured universe

---

## 5.X Deterministic Asset Scoring

Tie-break order:

1. highest liquidity
2. lowest spread
3. lowest volatility
4. best momentum
5. stable alphabetical fallback

### Deadline Preference

Short deadlines prioritize:

- higher liquidity
- lower volatility

---

## 5.X Asset Selection Transparency

The system must report:

- selected symbol
- reason for selection
- top candidate list
- excluded candidates with reasons

Example metrics:

- 24h volume
- volatility
- spread
- momentum score
- deadline compatibility

---

## 5.X Capital Allocation Rules

Capital allocation must be rule-aware.

### Allocation Steps

```

1. determine approved budget
2. subtract fee buffer
3. subtract safety buffer
4. compute raw quantity
5. round to step size
6. validate filters
7. compute residual balance

```

### Allocation Output

- approved budget
- usable budget
- price used
- raw quantity
- rounded quantity
- expected notional
- residual funds

---

## 5.X Signal Generation

Signals in Phase 5 are **advisory only**.

Possible outputs:

- `favorable`
- `neutral`
- `weak`
- `avoid`

Signals must be derived from deterministic inputs.

Examples:

- momentum
- volatility regime
- liquidity strength
- deadline compatibility

Preferred signal reason codes:

```

momentum_positive
volatility_elevated
liquidity_strong
deadline_mismatch

```

---

## 5.X Final Trade Plan Output — Rules Snapshot

Each trade plan must include a **rules snapshot**.

### Required Fields

- symbol
- step size
- minQty
- maxQty
- minNotional
- tick size
- price source
- price timestamp
- candle interval
- candle count
- candle window timestamps
- planning timestamp
- market data environment
- execution environment

This ensures deterministic validation in later phases.

---

## 5.X Persistence — `trade_plans` Schema

Trade plans must link directly to trade requests.

### Required Fields

```

trade_plan_id
trade_request_id
request_id

```

### Required Metadata

- request snapshot
- selected symbol
- candidate list
- feasibility category
- warning reasons
- approved budget
- quantity details
- rules snapshot
- derived metrics
- signal summary
- market data environment
- creation timestamp

---

## 5.X Derived Metrics

Example metrics stored in plan:

- volatility %
- 24h volume
- spread %
- momentum score
- deadline remaining
- data freshness status

---

## 5.X Error Handling Labels

Worst-case scenarios must be labeled.

| Scenario | Label |
|------|------|
missing rules | hard_stop |
corrupt candles | hard_stop |
stale 24h stats | warning_only |
high volatility | high_risk |
price inconsistency | hard_stop |

This removes ambiguity during implementation.

---

## 5.X CLI Planning Entry Point

Example CLI commands:

```

trade plan <id>
plan create <id>

```

### MVP Command Recommendation

Use a single canonical entry point:

```text
cryptogent trade plan <id>
```

This command should:

1. run the deterministic planning pipeline
2. print a summary
3. persist the plan
4. stop before execution

Behavior:

1. load request
2. build plan
3. print summary
4. show decision + warnings
5. optionally ask:

```

continue to validation?

```

Important:

CLI planning **must not place orders**.

---

## 5.X Planning Output Transparency

Plan summary must include:

- request reference
- decision category
- selected symbol
- approved budget
- estimated quantity
- rules snapshot
- market data summary
- signal summary
- warnings
- rejection reasons
- candidate ranking

---

## 5.X Initial Universe Recommendation

To simplify early implementation:

```

[trading]
candidate_symbols = ["BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT"]

```

If empty:

fallback to **top liquid USDT pairs**.

The system must always state whether a symbol came from:

- allowlist
- fallback discovery

---

## 5.X Candidate Reporting

Asset selection must report:

- top N candidates
- each candidate metrics
- why the winner was selected
- why others were excluded

Example metrics:

- 24h volume
- volatility
- spread
- momentum
- deadline fit
- data freshness

## Worst-Case Scenarios to Design For

The planning layer should explicitly handle these worst cases by **failing safe** (no plan / not feasible), or by producing a plan with a clearly flagged risk state:

### Market Data Integrity Failures

* **Stale data** (cached price older than expected; candles not updated)
* **Inconsistent data** (ticker price disagrees materially with candle close)
* **Missing fields / partial responses** from exchange endpoints
* **Out-of-order candles** or duplicated candle entries
* **Clock skew** between local time and exchange time causing “latest” windows to be miscomputed

### Connectivity and Exchange Availability

* **TLS interception / certificate failures** on some networks
* **DNS failures** / captive portals / intermittent connectivity
* **Rate limiting** (HTTP 429) and temporary exchange unavailability
* **Regional blocks / ISP filtering** where public endpoints work but signed endpoints fail (or vice versa)

### Symbol and Market Structure Changes

* **Symbol not TRADING** (halted, paused, or in maintenance)
* **Delisting / trading rule changes** mid-run (filters change, min notional changes)
* **Quote asset mismatch** (user budget asset ≠ symbol quote asset)
* **Tick/step size constraints** that make the requested budget effectively non-actionable after rounding

### Extreme Market Conditions

* **Gap moves** (price jumps beyond stop-loss/profit levels between sampling intervals)
* **Volatility spike** (recent volatility far above typical; profit target may be trivial, stop-loss may be too tight)
* **Illiquidity / spread blowout** (thin books, large spread, severe slippage risk)
* **Sudden correlation breaks** (asset behaves differently than recent history suggests)

### Asset-Specific “Surprise” Risks

* **Stablecoin depeg** for the chosen quote/exit asset (e.g. USDT-like risk scenarios)
* **Low-liquidity meme tokens** where candles look active but real executable liquidity is poor
* **Manipulation / wash trading signals** producing misleading volume or momentum metrics

### Account / State Drift

* **Local cache drift** (balances/orders in SQLite differ from exchange; planning must not trust cache)
* **Funds locked by open orders** so “free balance” is lower than expected
* **External manual user actions** during or between planning steps

### Degradation Policy (MVP)

For MVP, prefer conservative outcomes:

* If required market data is missing/inconsistent → `not_feasible`
* If symbol rules cannot be retrieved → `not_feasible`
* If volatility/liquidity is abnormal beyond thresholds → `high_risk` (or `feasible_with_warning`)
* If budget cannot meet min notional after rounding → `not_feasible`

---

# Logging Requirements

Log the following key events:

* market data retrieval started and completed
* feasibility evaluation result
* selected asset
* allocation result
* generated signal
* planning completed or rejected

Example logs:

```text
[INFO] Planning: Retrieved market data for candidate assets
[INFO] Feasibility: Request tr_001 marked feasible_with_warning
[INFO] AssetSelection: Selected SOLUSDT
[INFO] Allocation: Approved 180 USDT
[INFO] Strategy: BUY signal generated
```

## Must Not Log

Do not log:

* API secrets
* request signatures
* full candle arrays
* full account payloads
* large raw API responses

## Preferred Summary Logging

Instead log:

* candle count
* candle time range
* price range
* volume summary
* balance summary
* rules timestamp

Rule:

```text
Never log full candle arrays or full account payloads.
Log summaries, counts, and timestamps only.
```

---

# Suggested Modules

Suggested files for this phase:

```text
market/
  market_data_service.py
  candles.py
  tickers.py

planning/
  feasibility.py
  asset_selector.py
  allocation.py
  strategy.py
  trade_planner.py

models/
  feasibility_result.py
  trade_plan.py
```

Possible responsibilities:

## `market_data_service.py`

* unified interface for market data retrieval

## `candles.py`

* candle fetch helpers

## `tickers.py`

* ticker and stats fetch helpers

## `feasibility.py`

* deterministic feasibility evaluation

## `asset_selector.py`

* asset scoring and ranking

## `allocation.py`

* budget approval logic

## `strategy.py`

* initial signal generation

## `trade_planner.py`

* orchestration of planning phase

---

# Deliverables

Phase 5 is complete when:

* market data can be retrieved reliably
* feasibility evaluation works
* a candidate asset can be selected
* capital allocation works conservatively
* an initial strategy signal is generated
* a structured trade plan is produced

No orders should be placed in this phase.

---

# Success Criteria

Phase 5 is successful when the system can:

* take a validated trade request
* retrieve relevant market context
* determine whether the request is feasible
* select a suitable asset
* approve a safe budget
* generate a planning signal
* produce a clean trade plan ready for the safety phase

```
```
