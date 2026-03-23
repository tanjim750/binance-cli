PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS app_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS system_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  last_start_time_utc TEXT,
  last_shutdown_time_utc TEXT,
  last_successful_sync_time_utc TEXT,
  current_mode TEXT,
  automation_paused INTEGER NOT NULL DEFAULT 0,
  pause_reason TEXT,
  paused_at_utc TEXT,
  last_reconciliation_status TEXT,
  created_at_utc TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS account_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at_utc TEXT NOT NULL,
  payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS balances (
  asset TEXT PRIMARY KEY,
  free TEXT NOT NULL,
  locked TEXT NOT NULL,
  snapshot_time_utc TEXT,
  updated_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  exchange_order_id TEXT,
  order_source TEXT NOT NULL DEFAULT 'external',
  symbol TEXT NOT NULL,
  side TEXT NOT NULL,
  type TEXT NOT NULL,
  status TEXT NOT NULL,
  time_in_force TEXT,
  price TEXT,
  quantity TEXT NOT NULL,
  filled_quantity TEXT NOT NULL,
  executed_quantity TEXT,
  created_at_utc TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_exchange_order_id ON orders(exchange_order_id);

CREATE TABLE IF NOT EXISTS sync_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  started_at_utc TEXT NOT NULL,
  finished_at_utc TEXT,
  status TEXT NOT NULL,
  error_msg TEXT
);

CREATE TABLE IF NOT EXISTS trade_requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id TEXT,
  status TEXT NOT NULL,
  preferred_symbol TEXT,
  exit_asset TEXT,
  label TEXT,
  notes TEXT,
  budget_mode TEXT NOT NULL,
  budget_asset TEXT NOT NULL,
  budget_amount TEXT,
  profit_target_pct TEXT NOT NULL,
  stop_loss_pct TEXT NOT NULL,
  deadline_hours INTEGER,
  deadline_utc TEXT NOT NULL,
  validation_status TEXT,
  validation_error TEXT,
  validated_at_utc TEXT,
  last_price TEXT,
  estimated_qty TEXT,
  symbol_base_asset TEXT,
  symbol_quote_asset TEXT,
  created_at_utc TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT NOT NULL,
  base_asset TEXT,
  quote_asset TEXT,
  market_data_environment TEXT NOT NULL,
  execution_environment TEXT NOT NULL,
  entry_price TEXT NOT NULL,
  quantity TEXT NOT NULL,
  locked_qty TEXT NOT NULL DEFAULT '0',
  source_execution_id INTEGER,
  gross_quantity TEXT,
  fee_amount TEXT,
  fee_asset TEXT,
  stop_loss_price TEXT NOT NULL,
  profit_target_price TEXT NOT NULL,
  deadline_utc TEXT NOT NULL,
  status TEXT NOT NULL,
  opened_at_utc TEXT,
  closed_at_utc TEXT,
  last_monitored_at_utc TEXT,
  created_at_utc TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dust_ledger (
  dust_id INTEGER PRIMARY KEY AUTOINCREMENT,
  asset TEXT NOT NULL UNIQUE,
  dust_qty TEXT NOT NULL,
  avg_cost_price TEXT NOT NULL,
  needs_reconcile INTEGER NOT NULL DEFAULT 1,
  created_at_utc TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT NOT NULL,
  timeframe TEXT NOT NULL,
  captured_at_utc TEXT NOT NULL,
  last_price TEXT NOT NULL,
  bid TEXT,
  ask TEXT,
  spread_pct TEXT,
  change_percent TEXT,
  volume_quote TEXT,
  indicators_json TEXT,
  condition_summary TEXT,
  enabled_flags TEXT,
  config_hash TEXT
);

CREATE TABLE IF NOT EXISTS fear_greed_index (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  value TEXT NOT NULL,
  value_classification TEXT NOT NULL,
  timestamp_utc TEXT NOT NULL,
  time_until_update_s INTEGER,
  source TEXT NOT NULL DEFAULT 'alternative.me',
  raw_json TEXT NOT NULL,
  created_at_utc TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_fear_greed_source_ts ON fear_greed_index(source, timestamp_utc);
CREATE INDEX IF NOT EXISTS idx_fear_greed_created ON fear_greed_index(created_at_utc);

CREATE TABLE IF NOT EXISTS news_articles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  provider TEXT NOT NULL,
  provider_article_id TEXT NOT NULL,
  request_kind TEXT NOT NULL,
  request_params_json TEXT,
  title TEXT NOT NULL,
  description TEXT,
  content TEXT,
  url TEXT NOT NULL,
  image_url TEXT,
  published_at_utc TEXT NOT NULL,
  lang TEXT,
  source_id TEXT,
  source_name TEXT,
  source_url TEXT,
  source_country TEXT,
  fetched_at_utc TEXT NOT NULL,
  raw_json TEXT NOT NULL,
  created_at_utc TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_news_provider_article ON news_articles(provider, provider_article_id);
CREATE INDEX IF NOT EXISTS idx_news_published ON news_articles(published_at_utc);
CREATE INDEX IF NOT EXISTS idx_news_provider ON news_articles(provider);

CREATE TABLE IF NOT EXISTS telegram_channel_state (
  channel TEXT NOT NULL PRIMARY KEY,
  last_message_id INTEGER,
  last_synced_at_utc TEXT,
  created_at_utc TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS telegram_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel TEXT NOT NULL,
  message_id INTEGER NOT NULL,
  published_at_utc TEXT NOT NULL,
  text TEXT,
  views INTEGER,
  forwards INTEGER,
  has_media INTEGER NOT NULL DEFAULT 0,
  source_type TEXT,
  sentiment_score REAL,
  impact_score REAL,
  event_hash TEXT,
  raw_json TEXT NOT NULL,
  created_at_utc TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_telegram_channel_message ON telegram_messages(channel, message_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_telegram_event_hash ON telegram_messages(event_hash);
CREATE INDEX IF NOT EXISTS idx_telegram_published ON telegram_messages(published_at_utc);
CREATE INDEX IF NOT EXISTS idx_telegram_channel ON telegram_messages(channel);

CREATE TABLE IF NOT EXISTS youtube_channel_state (
  channel_id TEXT NOT NULL PRIMARY KEY,
  channel_name TEXT,
  last_video_published_at_utc TEXT,
  last_synced_at_utc TEXT,
  created_at_utc TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS youtube_discovery_state (
  discovery_key TEXT NOT NULL PRIMARY KEY,
  last_published_at_utc TEXT,
  last_synced_at_utc TEXT,
  created_at_utc TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS youtube_videos (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  video_id TEXT NOT NULL,
  channel_id TEXT NOT NULL,
  channel_title TEXT,
  title TEXT NOT NULL,
  description TEXT,
  published_at_utc TEXT NOT NULL,
  tags_json TEXT,
  view_count INTEGER,
  like_count INTEGER,
  comment_count INTEGER,
  topic_labels_json TEXT,
  sentiment_score REAL,
  impact_score REAL,
  source_type TEXT,
  raw_json TEXT NOT NULL,
  created_at_utc TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS youtube_comments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  video_id TEXT NOT NULL,
  comment_id TEXT NOT NULL,
  published_at_utc TEXT NOT NULL,
  text TEXT,
  like_count INTEGER,
  reply_count INTEGER,
  author_channel_id TEXT,
  source_type TEXT,
  topic_labels_json TEXT,
  sentiment_score REAL,
  impact_score REAL,
  raw_json TEXT NOT NULL,
  created_at_utc TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_youtube_video_id ON youtube_videos(video_id);
CREATE INDEX IF NOT EXISTS idx_youtube_channel_id ON youtube_videos(channel_id);
CREATE INDEX IF NOT EXISTS idx_youtube_published ON youtube_videos(published_at_utc);
CREATE UNIQUE INDEX IF NOT EXISTS idx_youtube_comment_id ON youtube_comments(comment_id);
CREATE INDEX IF NOT EXISTS idx_youtube_comment_video ON youtube_comments(video_id);

CREATE TABLE IF NOT EXISTS trade_plans (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  trade_request_id INTEGER NOT NULL,
  request_id TEXT,
  status TEXT NOT NULL,
  feasibility_category TEXT NOT NULL,
  warnings_json TEXT,
  rejection_reason TEXT,
  market_data_environment TEXT NOT NULL,
  execution_environment TEXT NOT NULL,
  symbol TEXT NOT NULL,
  price TEXT NOT NULL,
  bid TEXT,
  ask TEXT,
  spread_pct TEXT,
  volume_24h_quote TEXT,
  volatility_pct TEXT,
  momentum_pct TEXT,
  budget_mode TEXT NOT NULL,
  approved_budget_asset TEXT NOT NULL,
  approved_budget_amount TEXT,
  usable_budget_amount TEXT,
  raw_quantity TEXT,
  rounded_quantity TEXT,
  expected_notional TEXT,
  rules_snapshot_json TEXT NOT NULL,
  market_summary_json TEXT NOT NULL,
  candidate_list_json TEXT,
  signal TEXT NOT NULL,
  signal_reasons_json TEXT,
  created_at_utc TEXT NOT NULL,
  FOREIGN KEY(trade_request_id) REFERENCES trade_requests(id)
);

CREATE INDEX IF NOT EXISTS idx_trade_plans_trade_request_id ON trade_plans(trade_request_id);

CREATE TABLE IF NOT EXISTS execution_candidates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  trade_plan_id INTEGER NOT NULL,
  trade_request_id INTEGER NOT NULL,
  request_id TEXT,
  symbol TEXT NOT NULL,
  side TEXT NOT NULL,
  order_type TEXT NOT NULL,
  limit_price TEXT,
  execution_environment TEXT NOT NULL,
  position_id INTEGER,
  validation_status TEXT NOT NULL,
  risk_status TEXT NOT NULL,
  approved_budget_asset TEXT NOT NULL,
  approved_budget_amount TEXT NOT NULL,
  approved_quantity TEXT NOT NULL,
  execution_ready INTEGER NOT NULL,
  summary TEXT NOT NULL,
  details_json TEXT,
  created_at_utc TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL,
  FOREIGN KEY(trade_plan_id) REFERENCES trade_plans(id),
  FOREIGN KEY(trade_request_id) REFERENCES trade_requests(id)
);

CREATE INDEX IF NOT EXISTS idx_execution_candidates_trade_plan_id ON execution_candidates(trade_plan_id);

CREATE TABLE IF NOT EXISTS executions (
  execution_id INTEGER PRIMARY KEY AUTOINCREMENT,
  candidate_id INTEGER NOT NULL,
  plan_id INTEGER NOT NULL,
  trade_request_id INTEGER NOT NULL,
  symbol TEXT NOT NULL,
  side TEXT NOT NULL,
  order_type TEXT NOT NULL,
  execution_environment TEXT NOT NULL,
  position_id INTEGER,
  client_order_id TEXT NOT NULL,
  binance_order_id TEXT,
  quote_order_qty TEXT,
  limit_price TEXT,
  time_in_force TEXT,
  requested_quantity TEXT,
  executed_quantity TEXT,
  avg_fill_price TEXT,
  total_quote_spent TEXT,
  commission_total TEXT,
  commission_asset TEXT,
  fee_breakdown_json TEXT,
  realized_pnl_quote TEXT,
  realized_pnl_quote_asset TEXT,
  pnl_warnings_json TEXT,
  fills_count INTEGER,
  local_status TEXT NOT NULL,
  raw_status TEXT,
  retry_count INTEGER NOT NULL,
  submitted_at_utc TEXT,
  reconciled_at_utc TEXT,
  expired_at_utc TEXT,
  created_at_utc TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL,
  FOREIGN KEY(candidate_id) REFERENCES execution_candidates(id),
  FOREIGN KEY(plan_id) REFERENCES trade_plans(id),
  FOREIGN KEY(trade_request_id) REFERENCES trade_requests(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_executions_client_order_id ON executions(client_order_id);
CREATE INDEX IF NOT EXISTS idx_executions_candidate_id ON executions(candidate_id);

CREATE TABLE IF NOT EXISTS audit_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at_utc TEXT NOT NULL,
  level TEXT NOT NULL,
  event TEXT NOT NULL,
  details_json TEXT
);

CREATE TABLE IF NOT EXISTS monitoring_events (
  monitoring_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  position_id INTEGER NOT NULL,
  created_at_utc TEXT NOT NULL,
  symbol TEXT NOT NULL,
  entry_price TEXT,
  current_price TEXT,
  pnl_percent TEXT,
  decision TEXT NOT NULL,
  exit_reason TEXT,
  deadline_utc TEXT,
  position_status TEXT,
  error_code TEXT,
  error_message TEXT,
  FOREIGN KEY(position_id) REFERENCES positions(id)
);

CREATE INDEX IF NOT EXISTS idx_monitoring_events_position_id ON monitoring_events(position_id);

CREATE TABLE IF NOT EXISTS reconciliation_events (
  reconciliation_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_type TEXT NOT NULL,
  status TEXT NOT NULL,
  summary TEXT NOT NULL,
  details_json TEXT,
  created_at_utc TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reconciliation_events_created ON reconciliation_events(created_at_utc);

CREATE TABLE IF NOT EXISTS automation_pauses (
  pause_id INTEGER PRIMARY KEY AUTOINCREMENT,
  scope_type TEXT NOT NULL, -- loop|symbol
  scope_key TEXT NOT NULL,
  status TEXT NOT NULL, -- active|cleared
  reason TEXT,
  created_at_utc TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_automation_pauses_scope ON automation_pauses(scope_type, scope_key, status);

CREATE TABLE IF NOT EXISTS manual_orders (
  manual_order_id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at_utc TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL,
  dry_run INTEGER NOT NULL,
  execution_environment TEXT NOT NULL,
  base_url TEXT NOT NULL,
  symbol TEXT NOT NULL,
  side TEXT NOT NULL,
  order_type TEXT NOT NULL,
  time_in_force TEXT,
  limit_price TEXT,
  quote_order_qty TEXT,
  quantity TEXT,
  client_order_id TEXT NOT NULL,
  binance_order_id TEXT,
  local_status TEXT NOT NULL,
  raw_status TEXT,
  retry_count INTEGER NOT NULL,
  executed_quantity TEXT,
  avg_fill_price TEXT,
  total_quote_value TEXT,
  fee_breakdown_json TEXT,
  message TEXT,
  details_json TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_manual_orders_client_order_id ON manual_orders(client_order_id);
CREATE INDEX IF NOT EXISTS idx_manual_orders_created_at ON manual_orders(created_at_utc);

-- Phase 12 — Manual Loop Trading Mode (human-only)
CREATE TABLE IF NOT EXISTS loop_sessions (
  loop_id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at_utc TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL,
  dry_run INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL,
  execution_environment TEXT NOT NULL,
  base_url TEXT NOT NULL,
  preset_id INTEGER,
  symbol TEXT NOT NULL,
  quote_qty TEXT NOT NULL,
  entry_order_type TEXT NOT NULL,
  entry_limit_price TEXT,
  take_profit_kind TEXT NOT NULL,
  take_profit_value TEXT NOT NULL,
  rebuy_kind TEXT,
  rebuy_value TEXT,
  stop_loss_kind TEXT,
  stop_loss_value TEXT,
  stop_loss_action TEXT NOT NULL DEFAULT 'stop_only',
  cleanup_policy TEXT NOT NULL DEFAULT 'cancel-open',
  max_cycles INTEGER NOT NULL,
  cycles_completed INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL,
  last_buy_leg_id INTEGER,
  last_sell_leg_id INTEGER,
  last_buy_avg_price TEXT,
  last_sell_avg_price TEXT,
  last_buy_executed_qty TEXT,
  last_sell_executed_qty TEXT,
  cumulative_realized_pnl_quote TEXT,
  pnl_quote_asset TEXT,
  stopped_at_utc TEXT,
  last_error TEXT,
  last_warning TEXT
);

CREATE INDEX IF NOT EXISTS idx_loop_sessions_status ON loop_sessions(status);
CREATE INDEX IF NOT EXISTS idx_loop_sessions_symbol ON loop_sessions(symbol);
CREATE INDEX IF NOT EXISTS idx_loop_sessions_preset_id ON loop_sessions(preset_id);

CREATE TABLE IF NOT EXISTS loop_legs (
  leg_id INTEGER PRIMARY KEY AUTOINCREMENT,
  loop_id INTEGER NOT NULL,
  created_at_utc TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL,
  cycle_index INTEGER NOT NULL,
  leg_role TEXT NOT NULL,
  side TEXT NOT NULL,
  order_type TEXT NOT NULL,
  time_in_force TEXT,
  limit_price TEXT,
  quote_order_qty TEXT,
  quantity TEXT,
  client_order_id TEXT NOT NULL,
  binance_order_id TEXT,
  local_status TEXT NOT NULL,
  raw_status TEXT,
  retry_count INTEGER NOT NULL DEFAULT 0,
  executed_quantity TEXT,
  avg_fill_price TEXT,
  total_quote_value TEXT,
  fee_breakdown_json TEXT,
  message TEXT,
  submitted_at_utc TEXT,
  reconciled_at_utc TEXT,
  filled_at_utc TEXT,
  FOREIGN KEY(loop_id) REFERENCES loop_sessions(loop_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_loop_legs_client_order_id ON loop_legs(client_order_id);
CREATE INDEX IF NOT EXISTS idx_loop_legs_loop_id ON loop_legs(loop_id);
CREATE INDEX IF NOT EXISTS idx_loop_legs_status ON loop_legs(local_status);
CREATE INDEX IF NOT EXISTS idx_loop_legs_binance_order_id ON loop_legs(binance_order_id);

CREATE TABLE IF NOT EXISTS loop_events (
  loop_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  loop_id INTEGER NOT NULL,
  created_at_utc TEXT NOT NULL,
  event_type TEXT NOT NULL,
  preset_id INTEGER,
  symbol TEXT,
  side TEXT,
  cycle_number INTEGER,
  client_order_id TEXT,
  binance_order_id TEXT,
  price TEXT,
  quantity TEXT,
  message TEXT,
  details_json TEXT,
  FOREIGN KEY(loop_id) REFERENCES loop_sessions(loop_id)
);

CREATE INDEX IF NOT EXISTS idx_loop_events_loop_id ON loop_events(loop_id);
CREATE INDEX IF NOT EXISTS idx_loop_events_event_type ON loop_events(event_type);
CREATE INDEX IF NOT EXISTS idx_loop_events_preset_id ON loop_events(preset_id);

-- Phase 12 — Manual Loop Trading presets (saved configurations)
CREATE TABLE IF NOT EXISTS loop_presets (
  preset_id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at_utc TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL,
  name TEXT,
  notes TEXT,
  symbol TEXT NOT NULL,
  quote_qty TEXT NOT NULL,
  entry_order_type TEXT NOT NULL,
  entry_limit_price TEXT,
  take_profit_kind TEXT NOT NULL,
  take_profit_value TEXT NOT NULL,
  rebuy_kind TEXT,
  rebuy_value TEXT,
  stop_loss_kind TEXT,
  stop_loss_value TEXT,
  stop_loss_action TEXT NOT NULL DEFAULT 'stop_only',
  cleanup_policy TEXT NOT NULL DEFAULT 'cancel-open'
);

CREATE INDEX IF NOT EXISTS idx_loop_presets_symbol ON loop_presets(symbol);
