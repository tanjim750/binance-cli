````markdown
# 📊 CryptoGent Market status – Full Design Specification

## 1. Introduction

The Market module provides a structured and extensible system for analyzing cryptocurrency markets within CryptoGent. It allows users to:

- View real-time market conditions
- Perform technical analysis using multiple factors
- Compare historical and multi-timeframe data
- Store analysis snapshots for later use
- Manually manage stored data

The system is designed to be **lightweight, modular, deterministic, and scalable**.

---

## 2. Core Principles

### 2.1 Lightweight by Default
The default command returns only essential market data. Advanced analysis is enabled explicitly via flags.

### 2.2 Modular Analysis
Each analysis category is independently controlled via CLI flags.

### 2.3 Deterministic Output
Given the same inputs (symbol, timeframe, flags), the system produces consistent results.

### 2.4 Snapshot-Based Storage
Only processed analysis results are stored. Raw market data is never stored.

### 2.5 Explicit Control
- No automatic storage
- No automatic cleanup
- All actions must be explicitly triggered

---

## 3. Key Concepts

### 3.1 Market Snapshot
A snapshot represents a computed market state at a specific time, including:

- Price data
- Indicators
- Condition summaries
- Metadata (symbol, timeframe, flags)

---

### 3.2 Timeframe Rule

- `market status` → **exactly one timeframe**
- `market compare` → supports multiple timeframes

---

## 4. CLI Commands

---

## 4.1 Market Status

### Purpose
Provides real-time market analysis for a single timeframe.

### Command

```bash
python3 -m cryptogent market status --symbol SOLUSDT --timeframe 1h
````

### Required Parameters

* `--symbol`
* `--timeframe`

### Supported Timeframes

```
1m, 3m, 5m, 15m, 30m
1h, 2h, 4h, 6h, 8h, 12h
1d
1w
1M
```

---

### Default Output

* Symbol
* Timeframe
* Last price
* Best bid / ask
* Spread (value + %)
* 24h high / low
* 24h change %
* 24h volume
* Condition summary

---

## 5. Analysis Modules (Flags)

---

### Price Action

```
--price-action
```

Includes:

* Support / Resistance
* Trend structure (HH, HL, LH, LL)
* Breakout / Breakdown
* Candlestick patterns

Outputs (key fields):

* `pa_support_level`, `pa_support_strength`, `pa_support_distance_pct`
* `pa_resistance_level`, `pa_resistance_strength`, `pa_resistance_distance_pct`
* `pa_structure_type`, `pa_last_swing_high`, `pa_last_swing_low`
* `pa_breakout`, `pa_breakdown`, `pa_breakout_strength`
* `pa_patterns_json`, `pa_last_pattern`, `pa_dominant_bias`, `pa_signal_count`, `pa_confluence`

---

### Trend

```
--trend
```

Includes:

* EMA (20, 50, 200)
* SMA
* Crossovers
* Crossover events (bullish_cross / bearish_cross)
* Crossover strength (% distance between EMA20 and EMA50)
* EMA50/EMA200 cross (golden/death cross)
* SMA20/50 and SMA50/200 cross equivalents
* Trend bias tiers (strong_bull/bull/neutral/bear/strong_bear)
* EMA50/EMA200 strength (% gap)
* ADX (+DI / -DI) + trend strength tier
* Ichimoku (Tenkan / Kijun / Senkou A/B) + cloud bias
* Price vs EMA % distance (20/50/200)
* Trendlines

---

### Momentum

```
--momentum
```

Includes:

* RSI
* MACD
* Stochastic RSI (%K / %D)
* RSI prev + RSI zone
* MACD bias
* Stoch RSI bias
* Williams %R + zone
* CCI + zone
* ROC + bias
* Composite momentum signal
* RSI single‑bar divergence hints (bullish/bearish)

---

### Volume & Liquidity

```
--volume
```

Includes:

* Volume trends
* Volume spikes
* Order book summary
* Liquidity zones
* Buy/Sell walls
* Volume MA20/MA50 + Z‑score
* Taker buy ratio + buy/sell pressure
* Taker buy ratio MA20
* OBV + OBV trend
* VWAP (rolling) + price vs VWAP %
* Vol‑price confirmation (confirmed/diverging/neutral)
* Order book imbalance (bid vs ask)
* Optional order‑book depth for walls/zones (controlled by CLI/config)

---

### Volatility

```
--volatility
```

Includes:

* Bollinger Bands
* ATR
* %B (price position within bands)
* Keltner Channel (upper/lower)
* Squeeze (BB inside KC)
* Historical volatility (annualised)
* Chandelier Exit (long/short)
* Volatility regime (low/normal/high/extreme)

---

### Market Structure

```
--structure
```

Includes:

* Accumulation / Distribution
* Range vs trend
* BOS
* CHOCH
* Last swing high/low (for context)

---

### Crypto-Specific

```
--crypto
```

Includes:

* Whale activity (later implementation)
* Exchange inflow/outflow  (later implementation)
* Funding rate
* Open interest
* Auto futures market: USDT‑M when symbol ends with `USDT`, otherwise COIN‑M

---

### Quant

```
--quant
```

Includes:

* Correlation
* Statistical signals
* ML features

---

### Execution

```
--execution
```

Includes:

* Spread quality
* Slippage estimate
* Market depth

---

### Risk

```
--risk
```

Includes:

* Stop-loss suggestion
* Risk/reward ratio
* Position sizing

---

## 6. Utility Flags

---

### Profiles

```
--profile quick
--profile trend
--profile full
```

---

### Indicators

```
--indicators
```

---

### Candle Limit

```
--limit 100
```

---

### Output Modes

```
--compact
--json
--table
```

---

### Signal Mode

```
--signal
```

Output:

```
Signal: BUY / SELL / HOLD
Confidence: low / medium / high
Reason: ...
```

---

### Cache

```
--cache 5s
```

Cache uses recent saved snapshots (when available) within the TTL to avoid refetching.

---

### Strict Mode

```
--strict
```

---

### Debug Mode

```
--debug
```

---

### Save Snapshot

```
--save-snapshot
```

---

## 7. Market Compare

---

### Purpose

Provides comparison across timeframes or historical snapshots.

---

### Command

```bash
python3 -m cryptogent market compare --symbol SOLUSDT --timeframes 1h,4h
```

OR

```bash
--since 24h
```

---

### Behavior

* Uses stored snapshots or fresh fetch
* Returns summarized comparison
* Does not output full analysis blocks

---

### Example Output

```
Timeframe Comparison
- 15m: bullish, RSI 61
- 1h: bullish, EMA aligned
- 4h: neutral

Alignment
- Short-term: bullish
- Mid-term: bullish
- Higher-term: neutral
- Overall: bullish with caution
```

---

## 8. Storage Design

---

### Storage Model

* Snapshot-based
* Stores computed output only

---

### Table: market_snapshots

Fields:

* id
* symbol
* timeframe
* captured_at_utc
* last_price
* bid
* ask
* spread
* change_percent
* volume
* indicators_json
* condition_summary
* enabled_flags
* config_hash

---

### Storage Rules

Store only when:

* `--save-snapshot` is used
* compare requires history
* signal mode is enabled

---

## 9. Cleanup System

---

### Command

```bash
python3 -m cryptogent market cleanup --older-than 7d --i-am-human
```

---

### Dry Run

```
--dry-run
```

---

### Behavior

* Deletes stored snapshot data only
* No automatic cleanup
* Requires explicit confirmation

---

### Options

```
--older-than 7d
--symbol SOLUSDT
--timeframe 1h
```

---

## 10. Retention Guidelines

| Data Type         | Retention |
| ----------------- | --------- |
| Snapshots         | 7 days    |
| Compare summaries | 30 days   |
| Signals           | 90 days   |

---

## 11. Safety Rules

* Missing data → marked as `unavailable`
* Strict mode → fails execution
* High API usage → warning shown
* System state (paused, etc.) → displayed if available

---

## 12. Output Structure

```
Market Snapshot
Condition Summary
Price Action
Trend
Momentum
Volume & Liquidity
Volatility
Market Structure
Crypto-Specific
Quant
Execution
Risk
Summary
```

Only enabled sections are shown.

---

## 13. Final Summary

This design provides:

* Modular CLI-based analysis
* Controlled and lightweight storage
* Explicit cleanup policy
* Clear separation between analysis and comparison
* Scalable architecture for future expansion

It ensures CryptoGent remains efficient while evolving into a professional-grade trading analysis system.

# Compute engine Implementation Plan (Custom Logic + Library-Based Indicators)

## 1. Goal
- **library-based technical indicators**
- **custom rule-based market structure logic**

The purpose of this design is to keep the system:

- accurate
- modular
- maintainable
- easy to extend later

---

## 2. High-Level Approach

The implementation should be split into **two layers**:

### 2.1 Library-Based Layer
This layer is responsible for computing standard technical indicators that already have reliable formulas and mature Python implementations.

Examples:

- EMA
- SMA
- RSI
- MACD
- Stochastic RSI
- Bollinger Bands
- ATR

### 2.2 Custom Logic Layer
This layer is responsible for computing market interpretation features that are not consistently available from indicator libraries, or that require project-specific rules.

Examples:

- support / resistance
- trend structure (HH, HL, LH, LL)
- breakout / breakdown
- trendlines
- liquidity zones
- accumulation / distribution
- BOS / CHOCH
- condition summaries
- signal explanations

---

## 3. Recommended Library Strategy

## 3.1 Primary Library

Use **`pandas-ta`** as the primary indicator library.

Reason:

- easy to integrate with pandas DataFrame
- pure Python
- large indicator coverage
- easier installation than TA-Lib
- suitable for CLI and snapshot analysis workflows

Note: `pandas-ta` currently depends on `numba`, which does not support Python 3.14.
Momentum indicators therefore require Python 3.10–3.13, or they must be disabled on Python 3.14+.

---

## 3.2 Optional Future Upgrade

Support **TA-Lib** later as an optional high-performance backend.

Reason:

- industry standard
- very fast
- useful if indicator computation becomes heavy

However, the initial implementation should not depend on TA-Lib because installation complexity is higher.

---

## 3.3 Core Principle

Libraries should be used only for **formula-based indicators**.

Custom logic must still be used for:

- structural interpretation
- market state classification
- pattern rules
- project-specific decision labels

---

## 4. Input Data Model

All analysis begins with Binance kline data.

The system should first normalize raw kline data into a structured DataFrame with at least these fields:

- `open_time`
- `open`
- `high`
- `low`
- `close`
- `volume`
- `close_time`

Optional fields if available:

- `quote_volume`
- `number_of_trades`
- `taker_buy_base_volume`
- `taker_buy_quote_volume`

---

## 5. Processing Pipeline

The implementation should follow this order:

### Step 1 – Fetch raw market data
Fetch klines and other required market data from Binance.

### Step 2 – Normalize data
Convert the raw response into a clean DataFrame with typed numeric columns.

### Step 3 – Compute library-based indicators
Use `pandas-ta` to calculate standard indicators.

### Step 4 – Compute custom market features
Run custom functions on the normalized candle data and indicator output.

### Step 5 – Generate condition summaries
Translate numeric outputs into human-readable labels.

### Step 6 – Build final result object
Return a structured analysis result that can be printed, serialized, or stored.

---

## 6. Library-Based Indicator Implementation

The following items should be implemented through the indicator library.

## 6.1 Trend Indicators

### EMA
Use close price to compute:

- EMA 20
- EMA 50
- EMA 200

### SMA
Use close price to compute:

- SMA 20
- SMA 50
- SMA 200

### ADX
Compute:

- ADX value
- +DI / -DI
- trend-strength tier from ADX

### Ichimoku
Compute:

- Tenkan-sen (conversion line)
- Kijun-sen (base line)
- Senkou Span A / B
- cloud bias (price vs cloud)

### Price vs EMA distance
Compute % distance:

- price vs EMA-20
- price vs EMA-50
- price vs EMA-200

### Crossovers
Do not rely only on raw library output.
The moving averages can be calculated by the library, but the crossover interpretation should be handled in custom logic.

Examples:

- EMA20 crosses above EMA50 → bullish crossover
- EMA20 crosses below EMA50 → bearish crossover

---

## 6.2 Momentum Indicators

### RSI
Use standard period, usually 14.

### MACD
Compute:

- MACD line
- signal line
- histogram

### Stochastic RSI
Use a standard period unless configurable later.

### Williams %R
Compute standard 14‑bar Williams %R.

### CCI
Compute 20‑bar CCI.

### ROC
Compute 10‑bar rate of change (%).

### Composite momentum signal
Optional: combine available indicators into a directional bias label.

### Momentum
If a simple momentum indicator is needed, it may be taken from the library directly.

---

## 6.3 Volatility Indicators

### Bollinger Bands
Compute:

- upper band
- middle band
- lower band

### ATR
Compute ATR using a standard period such as 14.

### Keltner Channel
Compute:

- EMA(20) ± 1.5 × ATR(14)

### Squeeze
TTM‑style: Bollinger Bands fully inside Keltner Channel.

### Historical Volatility
20‑bar log‑return standard deviation, annualised.

### Chandelier Exit
Compute long/short trailing stop levels:

- highest_high − (3 × ATR)
- lowest_low + (3 × ATR)

---

## 6.4 Optional Additional Indicators

If needed later, the same layer may support:

- ADX
- CCI
- ROC
- VWAP
- OBV

These should remain optional and not be part of the first mandatory implementation.

---

## 7. Custom Logic Implementation

The following items should be implemented using project-defined rules.

---

## 7.1 Support and Resistance

### Goal
Detect important repeated price zones where price tends to reverse or react.

### Method
Use recent candle highs and lows to identify local extrema.

### Basic logic
- local minima suggest support
- local maxima suggest resistance
- repeated touches increase confidence

### Output
Return:

- nearest support
- nearest resistance
- support strength
- resistance strength

---

## 7.2 Trend Structure (HH, HL, LH, LL)

### Goal
Detect whether the market is structurally trending or ranging.

### Method
Use swing highs and swing lows.

### Rules
- higher high + higher low → bullish structure
- lower high + lower low → bearish structure
- mixed structure → neutral or ranging

### Output
Return:

- latest swing high
- latest swing low
- structure label
- directional bias

---

## 7.3 Breakout / Breakdown

### Goal
Detect whether price has moved beyond a significant support or resistance level.

### Method
Compare latest close with computed support/resistance zones.

### Confirmation
A breakout or breakdown should not be declared only on wick movement.
Prefer:

- candle close beyond level
- optional volume confirmation

### Output
Return:

- breakout detected or not
- breakdown detected or not
- broken level
- confidence label

---

## 7.4 Candlestick Patterns

### Goal
Identify important candle formations.

### Initial pattern set
Implement a small reliable set first:

- Doji
- Hammer
- Shooting Star
- Bullish Engulfing
- Bearish Engulfing
- Morning Star
- Evening Star

### Method
Compare:
- open
- high
- low
- close
- body size
- wick sizes

### Output
Return:

- pattern name
- bullish / bearish / neutral bias
- confidence

---

## 7.5 Trendlines

### Goal
Provide a simplified structural interpretation of the current directional line.

### Method
Use recent swing points.

- uptrend line → connect higher lows
- downtrend line → connect lower highs

This does not need to be chart-perfect.
A simplified slope-based interpretation is enough for CLI output.

### Output
Return:

- trendline direction
- slope classification
- whether price is above or below the line

---

## 7.6 Volume Trend and Volume Spike

### Goal
Understand whether the latest movement is supported by participation.

### Method
Use rolling average volume.

### Rules
- current volume significantly above average → spike
- current volume below average → weak participation

### Output
Return:

- volume trend
- volume spike status
- relative volume ratio

---

## 7.7 Liquidity Zones

### Goal
Identify price areas where strong reactions are likely.

### Method
Can be approximated from:

- repeated highs/lows
- dense order activity areas
- clustered candle reactions

Initial implementation can remain heuristic.

### Output
Return:

- upper liquidity zone
- lower liquidity zone
- nearest zone to current price

---

## 7.8 Buy/Sell Walls

### Goal
Identify notable pressure in the order book.

### Method
Analyze order book depth and look for unusually large bid/ask quantities.

### Output
Return:

- strongest buy wall
- strongest sell wall
- wall imbalance summary

---

## 7.9 Accumulation / Distribution

### Goal
Estimate whether the asset is being quietly accumulated or distributed.

### Method
Use a combination of:

- range behavior
- repeated rejection zones
- price compression
- volume behavior

This should remain heuristic and descriptive, not absolute.

### Output
Return:

- accumulation
- distribution
- unclear

---

## 7.10 Range vs Trending Market

### Goal
Classify the market as either directional or sideways.

### Method
Use combined evidence from:

- structure
- ATR
- EMA alignment
- repeated support/resistance containment

### Output
Return:

- trending
- range-bound
- transitional

---

## 7.11 BOS / CHOCH

### Goal
Provide higher-level structure interpretation.

### Definitions
- BOS = Break of Structure
- CHOCH = Change of Character

### Method
Use swing structure changes:

- BOS when prior structure continuation breaks key swing
- CHOCH when directional character changes

### Output
Return:

- BOS detected or not
- CHOCH detected or not
- direction of event

---

## 7.12 Spread and Slippage Estimate

### Spread
Can be computed from bid and ask:

- absolute spread
- percentage spread

### Slippage estimate
Use order book depth to estimate likely movement for a hypothetical size.

Initial version can remain simple and approximate.

---

## 7.13 Risk Suggestions

### Stop-loss suggestion
Can be derived from:

- nearest support
- ATR buffer
- structure low

### Risk/reward ratio
Can be computed if both stop-loss and target assumptions are available.

### Position sizing
Should remain optional and based on user-defined capital/risk settings.

---

## 8. Condition Summary Layer

After both library-based and custom features are computed, the system should generate human-readable summaries.

Examples:

- trend: bullish
- momentum: weakening bullish
- volatility: moderate
- structure: higher highs and higher lows
- breakout state: approaching resistance
- volume confirmation: weak

This layer is important because CLI users should not need to manually interpret all raw values.

---

## 9. Result Object Design

The final result should be stored in a structured object with separate sections.

Suggested sections:

- market_snapshot
- trend_indicators
- momentum_indicators
- volatility_indicators
- price_action
- volume_liquidity
- market_structure
- execution_factors
- risk_metrics
- condition_summary
- metadata

This object should be the single source used for:

- CLI rendering
- JSON output
- snapshot storage
- comparison logic

---

## 10. Storage Policy

Only the **measured output** should be stored.

Do not store:

- raw tick streams
- full raw order book snapshots continuously
- unlimited repeated candle dumps

Store only:

- selected computed metrics
- summaries
- metadata
- chosen flags
- capture time

This keeps SQLite lightweight and useful for comparison.

---

## 11. Error Handling Rules

### Library computation failure
If a library-based indicator cannot be computed:

- mark it as unavailable
- include the reason in debug output
- do not fail the entire command unless strict mode is enabled

### Custom computation failure
If a custom rule cannot determine a value:

- return `unknown` or `unavailable`
- avoid forcing misleading labels

---

## 12. Recommended Implementation Order

### Phase 1 – Core foundation
- data normalization
- result object structure
- market snapshot output

### Phase 2 – Library-based indicators
- EMA
- SMA
- RSI
- MACD
- Bollinger Bands
- ATR

### Phase 3 – Basic custom logic
- support / resistance
- trend structure
- breakout / breakdown
- spread calculation
- volume spike

### Phase 4 – Intermediate custom logic
- candlestick patterns
- trendlines
- range vs trend
- basic BOS / CHOCH

### Phase 5 – Advanced custom logic
- liquidity zones
- accumulation / distribution
- slippage estimation
- risk suggestions

---

## 13. Final Recommendation

Use the following strategy:

- **Library for formula-based indicators**
- **Custom logic for interpretation, structure, and project-specific rules**

This hybrid design gives the best balance between:

- development speed
- reliability
- flexibility
- future expansion

---

## 14. Summary

### Use Library For
- EMA
- SMA
- RSI
- MACD
- Stochastic RSI
- Bollinger Bands
- ATR

### Use Custom Logic For
- support / resistance
- trend structure
- breakout / breakdown
- candlestick patterns
- trendlines
- crossovers interpretation
- volume spike
- liquidity zones
- accumulation / distribution
- BOS / CHOCH
- spread / slippage
- risk suggestions
- final condition summaries

This separation should be followed consistently throughout the CryptoGent Market module.

```
```
