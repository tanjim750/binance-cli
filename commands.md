# CryptoGent CLI Commands

Most commands accept:

- `--config <path>`: config TOML path (default: `./cryptogent.toml` or `$CRYPTOGENT_CONFIG`)
- `--db <path>`: SQLite DB path (default from config or `./cryptogent.sqlite3`)

## Setup

- `cryptogent init`  
  Create a default `cryptogent.toml` (if missing) and initialize the SQLite schema.

- `cryptogent menu`  
  Interactive menu wrapper over subcommands (includes creating + validating trade requests).

- `cryptogent status`  
  Show local paths plus cached-state counts (balances / open orders) and last sync status.

## Config

- `cryptogent config show`  
  Show effective config values (including whether testnet is enabled).

- `cryptogent config set-binance --api-key "…" --api-secret-stdin`  
  Store **mainnet** Binance API key/secret in `cryptogent.toml` (plaintext).

- `cryptogent config set-binance-testnet --api-key "…" --api-secret-stdin`  
  Store **testnet** Binance API key/secret in `cryptogent.toml` (plaintext, under `[binance_testnet]`).

- `cryptogent config use-testnet`  
  Toggle config to use Binance Spot Test Network (`binance.testnet = true`).

- `cryptogent config use-mainnet`  
  Toggle config back to real Binance Spot API (`binance.testnet = false`).

- `cryptogent config sync-bnb-burn`  
  Fetch and persist Binance “pay Spot fees with BNB” flag (`spotBNBBurn`) into config (mainnet/testnet section depending on network).

- `cryptogent config set-bnb-burn --enabled`  
  Enable paying Spot fees with BNB on Binance (requires API key+secret; may not be supported on testnet).

- `cryptogent config set-bnb-burn --disabled`  
  Disable paying Spot fees with BNB.

Notes:

- Recommended: use environment variables instead of storing secrets:
  - Mainnet: `BINANCE_API_KEY`, `BINANCE_API_SECRET`
  - Testnet: `BINANCE_TESTNET_API_KEY`, `BINANCE_TESTNET_API_SECRET`

## Exchange (no trading)

These are connectivity/read-only utilities. They support TLS/network flags:

- `--ca-bundle <pem>`: trust a custom CA bundle (for TLS-intercepting proxies)
- `--insecure`: disable TLS verification (debug only)
- `--testnet`: force Spot testnet for this command
- `--base-url <url>`: override base URL (escape hatch)

Commands:

- `cryptogent exchange ping`  
  Calls `GET /api/v3/ping` (quick connectivity check).

- `cryptogent exchange time`  
  Calls `GET /api/v3/time` (server timestamp in ms).

- `cryptogent exchange info [--symbol BTCUSDT]`  
  Calls `GET /api/v3/exchangeInfo` (optionally for a single symbol).

- `cryptogent exchange balances [--all]`  
  Calls `GET /api/v3/account` and prints balances (requires API key+secret).

## Sync (writes to SQLite; no trading)

Also supports: `--ca-bundle`, `--insecure`, `--testnet`, `--base-url`.

- `cryptogent sync startup`  
  Snapshot account + sync balances + sync open orders into SQLite.

- `cryptogent sync balances`  
  Sync balances into SQLite.

- `cryptogent sync open-orders [--symbol BTCUSDT]`  
  Sync open orders into SQLite (optionally filter by symbol).

- `cryptogent sync fear-greed`  
  Fetch and store the latest Fear & Greed Index reading (Alternative.me) into SQLite.

## Show (reads from SQLite; no network)

- `cryptogent show balances [--all] [--limit N]`  
  Print cached balances from SQLite.

- `cryptogent show balances --filter USDT`  
  Filter cached balances by asset substring.

- `cryptogent show open-orders [--symbol BTCUSDT] [--limit N]`  
  Print cached open orders from SQLite (includes `src=execution|manual|external`).

- `cryptogent show fear-greed [--limit N]`  
  Print cached Fear & Greed Index readings from SQLite.

- `cryptogent show audit [--limit N]`  
  Print recent audit log entries from SQLite (latest first).

## Trade (requests only; no execution)

- `cryptogent trade start --profit-target-pct 2.0 --deadline-hours 24 --budget-mode manual --budget 50 --budget-asset USDT --symbol BTCUSDT --exit-asset USDT`  
  Create a structured trade request in SQLite (no order placed). Prompts for confirmation unless `--yes` is provided.

- `cryptogent trade start --profit-target-pct 2.0 --deadline-hours 24 --budget-mode auto --budget-asset USDT --symbol BTCUSDT --exit-asset USDT --yes`  
  Create a request with auto budget selection (budget amount decided in later phases).

- `cryptogent trade list [--limit N]`  
  List stored trade requests (shows current validation status if present).

- `cryptogent trade show <id>`  
  Show one trade request (including last validation result).

- `cryptogent trade cancel <id>`  
  Cancel a `NEW` trade request.

- `cryptogent trade validate <id>`  
  Validates a trade request as a **gate**:
  - Rules/sizing check: Binance symbol rules (`exchangeInfo`) + current price (`ticker/price`)
  - Feasibility check: public market data (mainnet candles/stats/spread)
  Persists `VALID/INVALID/ERROR` + estimated quantity back into SQLite (no order placed).

Notes:
- `trade validate` currently requires `deadline_hours > 0` on the request for feasibility. If you create a request using `--deadline-minutes` or an ISO `--deadline`, validation will return `missing_trade_request_fields_for_feasibility`. Use `--deadline-hours` to avoid this.

- `cryptogent trade plan build <trade_request_id>`  
  Builds and persists a deterministic Phase 5 trade plan (public market data + rules snapshot + sizing; no order placed).
  Requires the trade request to be `VALID` (run `trade validate` first).

- `cryptogent trade plan list [--limit N]`  
  Lists stored trade plans.

- `cryptogent trade plan show <plan_id>`  
  Shows one stored trade plan (including rules snapshot and candidate list).

- `cryptogent trade safety <plan_id>`  
  Phase 6 safety validation (plan-based). Persists an `execution_candidates` row (no order placed).
  Use `--order-type LIMIT_BUY --limit-price <price>` to generate a LIMIT_BUY candidate.
  For SELL candidates, use:
  - `--order-type MARKET_SELL|LIMIT_SELL`
  - `--position-id <id>` (optional; default: active position for symbol)
  - `--close-mode amount|percent|all` plus `--close-amount` / `--close-percent` when required
  - `--limit-price <price>` when `LIMIT_SELL`

Notes:
- Backwards-compatible alias: `cryptogent trade plan-build <trade_request_id>` (same as `trade plan build`).

## Execution (Phase 7)

- `cryptogent trade execute <candidate_id> [--yes]`  
  Phase 7 execution: submits the order described by the execution candidate (idempotent `newClientOrderId` + reconciliation on timeout). Persists an `executions` row.
  Supported candidate order types:
  - `MARKET_BUY` (uses `quoteOrderQty = approved_budget_amount`)
  - `LIMIT_BUY` (GTC; uses `price=limit_price` and a base `quantity` computed from approved quote budget)
  - `MARKET_SELL` (uses base `quantity`)
  - `LIMIT_SELL` (GTC; uses `price=limit_price` + base `quantity`)

- `cryptogent trade execution list [--limit N]`  
  List stored execution attempts.

- `cryptogent trade execution show <execution_id>`  
  Show one stored execution attempt.

- `cryptogent trade execution cancel <execution_id>`  
  Cancels an open LIMIT_BUY / LIMIT_SELL execution on Binance using the stored `client_order_id`, reconciles locally, refreshes cached open orders, and recomputes position `locked_qty`.

- `cryptogent trade reconcile`  
  Reconcile in-flight/uncertain/open executions with Binance using `GET /api/v3/order` by `origClientOrderId`.
  Also marks open `LIMIT_BUY` executions as locally `expired` after `--limit-order-timeout-minutes` (default: 30).
  Use `--auto-cancel-expired` to also cancel the order on Binance when it times out. If not provided, CLI prompts (default: No).

- `cryptogent trade reconcile-all`  
  Convenience reconcile loop that covers:
  - executions + manual orders (tracked by `clientOrderId`)
  - and progressively reconciles all cached open orders (orderId) each tick

## Manual Direct Order Mode (human-only; bypasses planning/safety)

All manual order submissions require `--i-am-human`. Add `--dry-run` to preview + run live checks without submitting.

- `cryptogent trade manual buy-market --i-am-human --symbol BTCUSDT --quote-qty 50 [--dry-run]`  
  MARKET BUY using `quoteOrderQty` (spends quote asset from the symbol, e.g. USDT for `BTCUSDT`).

- `cryptogent trade manual buy-limit --i-am-human --symbol SOLUSDT --quote-qty 500 --limit-price 91 [--dry-run]`  
  LIMIT BUY (GTC). Base quantity is computed from the quote budget and limit price (tick/step/min-notional enforced).

- `cryptogent trade manual sell-market --i-am-human --symbol SOLUSDT --base-qty 1.0 [--dry-run]`  
  MARKET SELL by base quantity.

- `cryptogent trade manual sell-limit --i-am-human --symbol SOLUSDT --base-qty 1.0 --limit-price 100 [--dry-run]`  
  LIMIT SELL (GTC) by base quantity.

- `cryptogent trade manual cancel --i-am-human <manual_order_id>`  
  Cancel an open LIMIT manual order on Binance (by stored `client_order_id`).

- `cryptogent trade manual reconcile [--loop --interval-seconds 60 --duration-seconds N]`  
  Reconcile manual orders with Binance by `origClientOrderId` and refresh cached balances/open-orders.

- `cryptogent trade manual list [--limit N]` / `cryptogent trade manual show <manual_order_id>`  
  Inspect manual order history.

## Manual Loop Trading Mode (human-only; bypasses planning/safety)

All loop commands that can submit/cancel require `--i-am-human`. Use `--dry-run` on `start` to preview using live reads only (no POST/DELETE).

- `cryptogent trade manual loop create --symbol SOLUSDT --quote-qty 1000 --entry-type BUY_MARKET --take-profit-pct 1.0 --rebuy-pct -1`  
  Stores a reusable loop preset (no exchange side effects). Output includes `preset_id`. Presets do **not** store `max_cycles`.
  Optional stop-loss exit behavior:
  - `--stop-loss-action stop_only` (default)
  - `--stop-loss-action stop_and_exit` (protective MARKET SELL when stop-loss hits)
  Optional order cleanup policy (applies when the loop stops/completes):
  - `--cleanup-policy cancel-open` (default; cancel loop-created open LIMIT orders)
  - `--cleanup-policy none` (leave open orders on the exchange)
  - `--cleanup-policy cancel-open-and-exit` (cancel open orders + MARKET SELL remaining base balance)
  Optional stop-loss threshold:
  - `--stop-loss-pct <pct>` (e.g. `--stop-loss-pct 2`)
  - `--stop-loss-abs <quote>` (e.g. `--stop-loss-abs 0.50`)

- `cryptogent trade manual loop start --i-am-human --id <preset_id> --max-cycles 3`  
  Starts a loop from a stored preset id using the provided `--max-cycles` at runtime. By default it also runs the loop runner (no separate reconcile needed).
  Optional runtime override:
  - `--stop-loss-action stop_only|stop_and_exit`
  - `--cleanup-policy cancel-open|none|cancel-open-and-exit`

- `cryptogent trade manual loop start --i-am-human --symbol SOLUSDT --quote-qty 1000 --entry-type BUY_MARKET --take-profit-pct 1.0 --rebuy-pct -1 --max-cycles 3`  
  Starts a loop session and submits the entry BUY. Also auto-creates a preset internally so the session is linked to a reusable strategy. By default it runs the loop runner (no separate reconcile needed).

- `cryptogent trade manual loop start --i-am-human --symbol SOLUSDT --quote-qty 1000 --entry-type BUY_LIMIT --entry-limit-price 93.00 --take-profit-abs 0.50 --rebuy-abs 0.20 --max-cycles 0`  
  Starts an infinite loop with a LIMIT entry. Rebuy offsets are **signed**; `0.20` defaults to a dip (below last sell), `+0.20` is momentum (above last sell).

- `cryptogent trade manual loop status [--loop-id <id>]` / `cryptogent trade manual loop list [--limit N]`  
  Inspect stored loop sessions.

- `cryptogent trade manual loop preset list [--limit N]` / `cryptogent trade manual loop preset show <preset_id>`  
  Inspect stored loop presets (reusable configs for `loop start --id ...`).

- `cryptogent trade manual loop reconcile --i-am-human [--loop-id <id>]`  
  Advanced: reconciles the loop and advances **only after FULL fills**:
  - BUY fill → submits next SELL LIMIT leg
  - SELL fill → records realized PnL for the cycle and submits next BUY LIMIT leg (if more cycles remain)

- `cryptogent trade manual loop reconcile --i-am-human --loop --interval-seconds 6 [--duration-seconds 60]`  
  Runs loop reconciliation repeatedly:
  - Ctrl‑B: force-stop the loop session and apply cleanup policy
  - Ctrl‑C: stop the local runner only (loop session remains `running`)

- `cryptogent trade manual loop stop --i-am-human [--loop-id <id>]`  
  Stops the loop (requires interactive confirmation) and applies the configured cleanup policy (default `cancel-open-and-exit`), then refreshes cached balances/open orders.

## Positions (Phase 8; no execution)

- `cryptogent position list [--limit N]`  
  List stored positions (includes `LOCKED` reserved quantity from open SELL orders).

- `cryptogent position show <position_id> [--live]`  
  Show one position. With `--live`, fetch current price using the position’s market-data environment and compute unrealized PnL (Decimal-safe).

## Monitoring (Phase 8; decisions only; no execution)

- `cryptogent monitor once [--position-id <id>] [--verbose]`  
  Run one monitoring tick: fetch price, compute unrealized PnL, persist a monitoring event, and print decision summary (`hold|exit_recommended|reevaluate|data_unavailable`).

- `cryptogent monitor loop [--interval-seconds N] [--duration-seconds N] [--position-id <id>] [--verbose]`  
  Run monitoring ticks repeatedly (Ctrl-C to stop). If `--interval-seconds` is omitted, it uses `trading.monitoring_interval_seconds` from `cryptogent.toml`, otherwise a safe fallback default.
  Monitoring backoff is applied on repeated fetch failures (×2 then ×5), with messages like:
  `monitoring_fetch_failed ... backoff_multiplier=2 next_retry=20s`.

- `cryptogent monitor events list [--limit N]`  
  List monitoring event history (includes decision + reason code).

## Market (Phase 13; basic)

- `cryptogent market status --symbol <SYM> --timeframe <TF> [--limit N] [--market-env mainnet_public|testnet]`  
  Basic market status for a single timeframe (price, bid/ask, spread, 24h stats, and condition summary).
  Timeframes include: 1m, 3m, 5m, 15m, 30m, 1h, 2h, 4h, 6h, 8h, 12h, 1d, 1w, 1M.
  Options:
  - `--json` output JSON only
  - `--compact` single-line output
  - `--table` key/value table
  - `--cache 5s|60|1m|1h` cache TTL (uses recent saved snapshot when available)
  - `--save-snapshot` persist snapshot
  - `--profile quick|trend|full` apply a preset analysis bundle
  - `--momentum` include RSI/MACD/Stoch RSI
  - `--trend` include EMA/SMA + crossovers
  - `--volatility` include ATR + Bollinger
  - `--volume` include volume + order book summary
  - `--structure` include BOS/CHOCH + range/accumulation
  - `--price-action` include support/resistance, breakout, and candlestick patterns
  - `--execution` include execution-quality metrics (spread/slippage/depth)
  - `--risk` include risk sizing (stop/TP/position size/leverage)
  - `--quant` include quant metrics (correlation/stat signals/ML features)
  - `--crypto` include funding rate + open interest (auto USDT‑M vs COIN‑M)
  - `--risk-side long|short` side for risk sizing (default long)
  - `--risk-entry P` override entry price for risk sizing
  - `--risk-pct X` risk % of account (default 1)
  - `--risk-account-balance Q` account balance to use for sizing (quote)
  - `--risk-max-position-pct X` max position % cap (default 20)
  - `--volume-depth N` order book depth for liquidity metrics (default from config)
  - `--volume-window-fast N` fast volume MA window
  - `--volume-window-slow N` slow volume MA window
  - `--volume-spike-ratio X` spike ratio threshold
  - `--volume-zscore X` z-score spike threshold
  - `--volume-buy-ratio X` taker buy ratio for buy pressure
  - `--volume-sell-ratio X` taker buy ratio for sell pressure
  - `--volume-wall-ratio X` wall size multiple vs median
  - `--volume-imbalance X` book imbalance threshold
  - `--strict` fail if requested indicators are unavailable
  - `--debug` print indicator debug values

- `cryptogent market snapshot list [--limit N] [--symbol SYM] [--timeframe TF]`  
  List stored market snapshots (most recent first).

- `cryptogent market snapshot show <id>`  
  Show full snapshot details and stored indicators.

## Orders (Open Orders Management)

- `cryptogent orders cancel <order_id> [--i-am-human]`  
  Cancel a cached **open non‑MARKET** order by Binance `order_id` for sources `manual` or `execution`.
  External orders (`src=external`) cannot be cancelled.
  Manual orders require `--i-am-human`.
  After cancel, refreshes cached open orders/balances and recomputes `locked_qty`.

## Reliability (Phase 9)

- `cryptogent reliability status`  
  Show pause state, last reconciliation status, and last successful sync.

- `cryptogent reliability reconcile`  
  Sync balances + open orders, detect mismatches (balances, unknown orders, missing orders, position mismatch, uncertain executions), and record reconciliation events.  
  If critical ambiguity is detected, **global automation** is paused.  
  Single‑symbol mismatches pause only that symbol (and related loops).

- `cryptogent reliability resume --global --i-am-human`  
  Resume global automation after a healthy reconciliation.

- `cryptogent reliability resume --symbol SOLUSDT --i-am-human`  
  Resume automation for a paused symbol after a healthy reconciliation.

- `cryptogent reliability resume --loop-id 12 --i-am-human`  
  Resume automation for a paused loop after a healthy reconciliation.

- `cryptogent reliability events list [--limit N]`  
  List recent reconciliation events.

## PnL Helpers

- `cryptogent pnl realized [--limit N]`  
  List realized PnL from SELL executions (uses stored `realized_pnl_quote` on `executions`).

- `cryptogent pnl realized show <execution_id>`  
  Show realized PnL details for one execution (includes fee breakdown + warnings).

- `cryptogent pnl unrealized [--position-id <id>] [--limit N]`  
  Compute unrealized PnL for open positions using live price from each position’s `market_data_environment` (Decimal-safe).

- `cryptogent pnl unrealized --no-live`  
  Show entry/qty only (no network calls).

## Dust Ledger (accounting-only; not auto-traded)

- `cryptogent dust list [--limit N]`  
  List dust ledger rows (per asset), including “effective dust” (clamped against cached Binance free balance minus open position qty).

- `cryptogent dust show <asset>`  
  Show one dust ledger row plus the cached free balance and effective dust.
