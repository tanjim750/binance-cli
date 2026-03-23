from __future__ import annotations

import argparse
import sys
import os
import time
import select
import shutil
import termios
import tty
import contextlib
from pathlib import Path
from datetime import UTC, datetime
from typing import Any

from cryptogent.config.io import BINANCE_SPOT_BASE_URL, ConfigPaths, ensure_default_config
from cryptogent.config.io import load_config
from cryptogent.config.model import AppConfig
from cryptogent.config.edit import BinanceCredentialUpdate, update_binance_config
from cryptogent.db.migrate import ensure_db_initialized
from cryptogent.db.connection import connect
from cryptogent.exchange.binance_errors import BinanceAPIError
from cryptogent.exchange.binance_spot import BinanceSpotClient
from cryptogent.market.analysis.crypto import compute_crypto_metrics
from cryptogent.market.market_data_service import MarketDataError, fetch_market_snapshot, fetch_market_snapshot_cached
from cryptogent.market.analysis.momentum import compute_momentum_metrics
from cryptogent.market.analysis.trend import compute_trend_metrics
from cryptogent.market.analysis.volatility import compute_volatility_metrics
from cryptogent.market.analysis.volume import compute_volume_metrics
from cryptogent.market.analysis.structure import compute_structure_metrics
from cryptogent.market.analysis.quant import compute_quant_metrics
from cryptogent.market.analysis.execution import compute_execution_metrics
from cryptogent.market.analysis.risk import compute_risk_metrics
from cryptogent.market.analysis.price_action import compute_price_action_metrics
from cryptogent.planning.trade_planner import PlanningError, build_trade_plan, persist_trade_plan
from cryptogent.safety.validator import SafetyError, evaluate_safety
from cryptogent.execution.executor import (
    ExecutionError,
    execute_limit_buy,
    execute_limit_sell,
    execute_market_buy_quote,
    execute_market_sell_qty,
)
from cryptogent.execution.result_parser import parse_fills
from cryptogent.state.manager import OrderRow, StateManager
from cryptogent.sync.binance_sync import startup_sync, sync_balances, sync_open_orders
from cryptogent.sync.fear_greed_sync import sync_fear_greed
from cryptogent.validation.trade_request import ValidationError, validate_trade_request
from cryptogent.validation.binance_rules import parse_symbol_rules, precheck_market_buy, quantize_down, RuleError
from cryptogent.planning.feasibility import FeasibilityError, evaluate_feasibility, freshness_and_consistency_checks
from cryptogent.util.time import ms_to_utc_iso, utcnow_iso
from decimal import Decimal, InvalidOperation
import secrets
import json as _json


def _safe_json(value: Any) -> str | None:
    try:
        return _json.dumps(value, separators=(",", ":"))
    except Exception:
        return None


def _add_common_paths(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to config TOML (default: ./cryptogent.toml or $CRYPTOGENT_CONFIG).",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Path to SQLite DB (default from config or ./cryptogent.sqlite3).",
    )

def _add_exchange_tls_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--ca-bundle",
        type=Path,
        default=None,
        help="Path to a PEM CA bundle to trust (useful behind TLS-intercepting proxies).",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS cert verification (debug only; not recommended).",
    )
    parser.add_argument(
        "--testnet",
        action="store_true",
        help='Use Binance Spot Test Network (base URL "https://testnet.binance.vision").',
    )
    parser.add_argument(
        "--base-url",
        type=str,
        default=None,
        help='Override Binance base URL (e.g. "https://api.binance.com").',
    )


def _add_exchange_tls_only_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--ca-bundle",
        type=Path,
        default=None,
        help="Path to a PEM CA bundle to trust (useful behind TLS-intercepting proxies).",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS cert verification (debug only; not recommended).",
    )


def cmd_init(args: argparse.Namespace) -> int:
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)
    print(f"Config: {config_path}")
    print(f"DB:     {db_path}")
    return 0


def cmd_config_show(args: argparse.Namespace) -> int:
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    cfg = load_config(config_path)
    print(f"Config: {config_path}")
    print(f"- db_path: {cfg.db_path}")
    print(f"- binance_base_url: {cfg.binance_base_url}")
    print(f"- binance_testnet: {cfg.binance_testnet}")
    print(f"- binance_timeout_s: {cfg.binance_timeout_s}")
    print(f"- binance_recv_window_ms: {cfg.binance_recv_window_ms}")
    print(f"- binance_tls_verify: {cfg.binance_tls_verify}")
    print(f"- binance_ca_bundle_path: {cfg.binance_ca_bundle_path}")
    print(f"- binance_api_key_set: {bool(cfg.binance_api_key)}")
    print(f"- binance_api_secret_set: {bool(cfg.binance_api_secret)}")
    print(f"- binance_spot_bnb_burn: {cfg.binance_spot_bnb_burn}")
    print(f"- trading_monitoring_interval_seconds: {cfg.trading_monitoring_interval_seconds}")
    return 0


def cmd_config_set_binance(args: argparse.Namespace) -> int:
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)

    api_secret = args.api_secret
    if args.api_secret_stdin:
        api_secret = sys.stdin.read().strip()

    if args.testnet or args.base_url:
        print("Use `cryptogent config use-testnet` / `cryptogent config use-mainnet` to toggle networks.")
        return 2

    if not args.api_key and api_secret is None:
        print("Nothing to update (provide --api-key and/or --api-secret/--api-secret-stdin).")
        return 2

    update_binance_config(
        config_path,
        BinanceCredentialUpdate(
            api_key=args.api_key if args.api_key else None,
            api_secret=api_secret if api_secret not in (None, "") else None,
        ),
    )
    # Best-effort: fetch + persist spotBNBBurn after credentials are set.
    try:
        cfg = load_config(config_path)
        client = BinanceSpotClient.from_config(cfg)
        burn = client.get_spot_bnb_burn()
        update_binance_config(config_path, BinanceCredentialUpdate(spot_bnb_burn=burn))
    except Exception:
        pass
    print(f"Updated: {config_path}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)
    with connect(db_path) as conn:
        state = StateManager(conn)
        state.ensure_system_state()
        mode = "TESTNET" if load_config(config_path).binance_testnet else "MAINNET"
        state.update_system_start(current_mode=mode)
        last_sync = state.get_last_sync()
        bal_n = state.get_balance_count()
        oo_n = state.get_open_order_count()
        system_state = state.get_system_state()
    print("CryptoGent status:")
    print(f"- config: {config_path}")
    print(f"- db:     {db_path}")
    print(f"- mode:   {mode}")
    print(f"- cached balances: {bal_n}")
    print(f"- cached open orders: {oo_n}")
    if last_sync:
        print(f"- last sync: {last_sync.get('kind')} {last_sync.get('status')} finished={last_sync.get('finished_at_utc')}")
    if system_state:
        print(f"- last start: {system_state.get('last_start_time_utc')}")
        print(f"- last shutdown: {system_state.get('last_shutdown_time_utc')}")
        print(f"- last successful sync: {system_state.get('last_successful_sync_time_utc')}")
    return 0


def _client_from_args(args: argparse.Namespace) -> BinanceSpotClient:
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    cfg = load_config(config_path)
    client = BinanceSpotClient.from_config(cfg)

    # Base URL overrides (CLI wins).
    # `--testnet` forces testnet regardless of config.
    if getattr(args, "testnet", False):
        client = BinanceSpotClient(**{**client.__dict__, "base_url": "https://testnet.binance.vision"})
        tkey = os.environ.get("BINANCE_TESTNET_API_KEY")
        tsecret = os.environ.get("BINANCE_TESTNET_API_SECRET")
        if tkey:
            client = BinanceSpotClient(**{**client.__dict__, "api_key": tkey})
        if tsecret:
            client = BinanceSpotClient(**{**client.__dict__, "api_secret": tsecret})
    # `--base-url` remains as an escape hatch.
    if getattr(args, "base_url", None):
        client = BinanceSpotClient(**{**client.__dict__, "base_url": str(args.base_url).strip()})

    if getattr(args, "ca_bundle", None):
        client = BinanceSpotClient(
            **{**client.__dict__, "ca_bundle_path": args.ca_bundle.expanduser(), "tls_verify": True}
        )
    if getattr(args, "insecure", False):
        client = BinanceSpotClient(**{**client.__dict__, "tls_verify": False})

    # If config selected testnet, loader already switched keys; but allow env to override either way.
    return client


def cmd_config_use_testnet(args: argparse.Namespace) -> int:
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    update_binance_config(config_path, BinanceCredentialUpdate(testnet=True))
    print(f"Updated: {config_path} (binance.testnet = true)")
    return 0


def cmd_config_use_mainnet(args: argparse.Namespace) -> int:
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    update_binance_config(config_path, BinanceCredentialUpdate(testnet=False))
    print(f"Updated: {config_path} (binance.testnet = false)")
    return 0


def cmd_config_set_binance_testnet(args: argparse.Namespace) -> int:
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)

    api_secret = args.api_secret
    if args.api_secret_stdin:
        api_secret = sys.stdin.read().strip()

    if not args.api_key and api_secret is None:
        print("Nothing to update (provide --api-key and/or --api-secret/--api-secret-stdin).")
        return 2

    update_binance_config(
        config_path,
        BinanceCredentialUpdate(
            testnet_api_key=args.api_key if args.api_key else None,
            testnet_api_secret=api_secret if api_secret not in (None, "") else None,
        ),
    )
    # Best-effort: fetch + persist spotBNBBurn after credentials are set (may not be supported on testnet).
    try:
        cfg = load_config(config_path)
        client = BinanceSpotClient.from_config(cfg)
        burn = client.get_spot_bnb_burn()
        update_binance_config(config_path, BinanceCredentialUpdate(testnet_spot_bnb_burn=burn))
    except Exception:
        pass
    print(f"Updated: {config_path} ([binance_testnet])")
    return 0


def cmd_config_sync_bnb_burn(args: argparse.Namespace) -> int:
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    cfg = load_config(config_path)
    client = BinanceSpotClient.from_config(cfg)
    try:
        burn = client.get_spot_bnb_burn()
    except BinanceAPIError as e:
        if e.status == 404:
            print("Not supported on this Binance base URL (likely Spot Testnet).")
            return 2
        print(f"ERROR: {e}")
        return 2

    if cfg.binance_testnet:
        update_binance_config(config_path, BinanceCredentialUpdate(testnet_spot_bnb_burn=burn))
    else:
        update_binance_config(config_path, BinanceCredentialUpdate(spot_bnb_burn=burn))
    print(f"spotBNBBurn={burn} (saved to {config_path})")
    return 0


def cmd_config_set_bnb_burn(args: argparse.Namespace) -> int:
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    cfg = load_config(config_path)
    client = BinanceSpotClient.from_config(cfg)
    try:
        burn = client.set_spot_bnb_burn(enabled=bool(args.enabled))
    except BinanceAPIError as e:
        if e.status == 404:
            print("Not supported on this Binance base URL (likely Spot Testnet).")
            return 2
        print(f"ERROR: {e}")
        return 2

    if cfg.binance_testnet:
        update_binance_config(config_path, BinanceCredentialUpdate(testnet_spot_bnb_burn=burn))
    else:
        update_binance_config(config_path, BinanceCredentialUpdate(spot_bnb_burn=burn))
    print(f"spotBNBBurn={burn} (saved to {config_path})")
    return 0


def cmd_exchange_ping(args: argparse.Namespace) -> int:
    client = _client_from_args(args)
    try:
        client.ping()
    except BinanceAPIError as e:
        print(str(e))
        return 2
    print("OK")
    return 0


def cmd_exchange_time(args: argparse.Namespace) -> int:
    client = _client_from_args(args)
    try:
        t = client.get_server_time_ms()
    except BinanceAPIError as e:
        print(str(e))
        return 2
    print(t)
    return 0


def cmd_exchange_info(args: argparse.Namespace) -> int:
    client = _client_from_args(args)
    try:
        info = client.get_exchange_info(symbol=args.symbol)
    except BinanceAPIError as e:
        print(str(e))
        return 2
    # Avoid dumping the entire response by default; keep it human-friendly.
    tz = info.get("timezone")
    server_time = info.get("serverTime")
    symbols = info.get("symbols")
    count = len(symbols) if isinstance(symbols, list) else None
    print(f"timezone={tz} serverTime={server_time} symbols={count}")
    return 0


def cmd_exchange_balances(args: argparse.Namespace) -> int:
    client = _client_from_args(args)
    try:
        balances = client.get_balances()
    except BinanceAPIError as e:
        print(str(e))
        return 2
    # Print only non-zero balances unless --all is specified.
    shown = 0
    for b in balances:
        if not args.all and b.free in ("0", "0.0", "0.00000000") and b.locked in ("0", "0.0", "0.00000000"):
            continue
        print(f"{b.asset}: free={b.free} locked={b.locked}")
        shown += 1
    if shown == 0:
        print("(no balances to show)")
    return 0


def cmd_sync_startup(args: argparse.Namespace) -> int:
    client = _client_from_args(args)
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)
    with connect(db_path) as conn:
        result = startup_sync(client=client, conn=conn)
    if result.status != "ok":
        print("ERROR (see `show audit` and `status`)")
        return 2
    print(f"OK kind={result.kind} balances={result.balances_upserted} open_orders={result.open_orders_seen}")
    return 0


def cmd_sync_balances(args: argparse.Namespace) -> int:
    client = _client_from_args(args)
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)
    with connect(db_path) as conn:
        result = sync_balances(client=client, conn=conn)
    if result.status != "ok":
        print("ERROR (see `show audit` and `status`)")
        return 2
    print(f"OK kind={result.kind} balances={result.balances_upserted}")
    return 0


def cmd_sync_open_orders(args: argparse.Namespace) -> int:
    client = _client_from_args(args)
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)
    with connect(db_path) as conn:
        result = sync_open_orders(client=client, conn=conn, symbol=args.symbol)
    if result.status != "ok":
        print("ERROR (see `show audit` and `status`)")
        return 2
    print(f"OK kind={result.kind} open_orders={result.open_orders_seen}")
    return 0


def cmd_sync_fear_greed(args: argparse.Namespace) -> int:
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    cfg = load_config(config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)
    with connect(db_path) as conn:
        result = sync_fear_greed(
            conn=conn,
            ca_bundle=getattr(args, "ca_bundle", None),
            insecure=bool(getattr(args, "insecure", False)),
            cache_ttl_s=cfg.fear_greed_cache_ttl_seconds,
        )
    if result.status != "ok":
        print("ERROR (see `show audit` and `status`)")
        return 2
    print(f"OK kind={result.kind} rows={result.rows_upserted}")
    return 0


def cmd_show_balances(args: argparse.Namespace) -> int:
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)
    with connect(db_path) as conn:
        state = StateManager(conn)
        # If filtering, pull full set first so the limit applies after filtering.
        want_filter = str(getattr(args, "filter", "") or "").strip()
        rows = state.list_balances(include_zero=args.all, limit=None if want_filter else args.limit)

    if want_filter:
        contains = bool(getattr(args, "contains", False))
        raw = want_filter.strip()
        if raw.startswith("*") and raw.endswith("*") and len(raw) > 2:
            contains = True
            raw = raw.strip("*")
        raw = raw.upper()
        if contains:
            rows = [r for r in rows if raw in str(r.get("asset", "")).upper()]
        else:
            # Exact asset match by default (supports comma/space lists like "SOL,AI").
            tokens = [t.strip().upper() for t in raw.replace(" ", ",").split(",") if t.strip()]
            wanted = set(tokens)
            rows = [r for r in rows if str(r.get("asset", "")).upper() in wanted]

    if getattr(args, "limit", None) is not None:
        try:
            rows = rows[: int(args.limit)]
        except Exception:
            pass
    if not rows:
        print("(no balances cached)")
        return 0

    def _d(v: object) -> Decimal:
        try:
            return Decimal(str(v))
        except Exception:
            return Decimal(0)

    updated = [r.get("updated_at_utc") for r in rows if r.get("updated_at_utc")]
    last_updated = max(updated) if updated else None

    print(f"Cached balances: {len(rows)}" + (f" (last updated: {last_updated})" if last_updated else ""))
    print(f"{'ASSET':<12} {'FREE':>18} {'LOCKED':>18} {'UPDATED (UTC)':>22}")
    for r in rows:
        asset = str(r.get("asset") or "")
        free = _d(r.get("free"))
        locked = _d(r.get("locked"))
        updated_at = str(r.get("updated_at_utc") or "")
        free_s = format(free, "f")
        locked_s = format(locked, "f")
        print(f"{asset:<12} {free_s:>18} {locked_s:>18} {updated_at:>22}")
    return 0


def cmd_show_fear_greed(args: argparse.Namespace) -> int:
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)
    with connect(db_path) as conn:
        state = StateManager(conn)
        rows = state.list_fear_greed(limit=args.limit)
    if not rows:
        print("(no fear & greed data cached)")
        return 0
    for r in rows:
        ts = r.get("timestamp_utc")
        value = r.get("value")
        cls = r.get("value_classification")
        source = r.get("source")
        print(f"{ts} value={value} class={cls} source={source}")
    return 0

def cmd_show_open_orders(args: argparse.Namespace) -> int:
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)
    with connect(db_path) as conn:
        state = StateManager(conn)
        rows = state.list_open_orders(symbol=args.symbol, limit=args.limit)
    if not rows:
        print("(no open orders cached)")
        return 0
    for r in rows:
        src = str(r.get("order_source") or "external")
        print(
            f"{r['symbol']} {r['side']} {r['type']} status={r['status']} "
            f"price={r['price']} qty={r['quantity']} filled={r['filled_quantity']} "
            f"updated={r['updated_at_utc']} id={r['exchange_order_id']} src={src}"
        )
    return 0


def cmd_show_audit(args: argparse.Namespace) -> int:
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)
    with connect(db_path) as conn:
        state = StateManager(conn)
        rows = state.list_audit_logs(limit=args.limit)
    if not rows:
        print("(no audit logs)")
        return 0
    for r in rows:
        details = r.get("details_json")
        details_s = f" details={details}" if details not in (None, "", "{}") else ""
        print(f"{r['created_at_utc']} {r['level']} {r['event']}{details_s}")
    return 0


def cmd_dust_list(args: argparse.Namespace) -> int:
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)
    limit = int(getattr(args, "limit", 200))
    with connect(db_path) as conn:
        state = StateManager(conn)
        rows = state.list_dust(limit=limit)
        balances = {str(r.get("asset") or "").upper(): Decimal(str(r.get("free") or "0")) for r in state.list_balances(include_zero=True, limit=None)}
        open_pos_qty = state.get_open_position_qty_by_asset()

    if not rows:
        print("(no dust ledger rows)")
        return 0

    def _d(v: object) -> Decimal:
        try:
            return Decimal(str(v))
        except Exception:
            return Decimal("0")

    print(f"Dust ledger: {len(rows)}")
    print(f"{'ASSET':<10} {'DUST_QTY':>16} {'AVG_COST':>16} {'EFFECTIVE':>16} {'NEEDS_REC':>10} {'UPDATED (UTC)':>22}")
    for r in rows:
        asset = str(r.get("asset") or "").strip().upper()
        dust_qty = _d(r.get("dust_qty"))
        avg_cost = _d(r.get("avg_cost_price"))
        needs = int(r.get("needs_reconcile") or 0)
        free = balances.get(asset, Decimal("0"))
        reserved = open_pos_qty.get(asset, Decimal("0"))
        allowed = free - reserved
        if allowed < 0:
            allowed = Decimal("0")
        effective = dust_qty if dust_qty <= allowed else allowed
        updated_at = str(r.get("updated_at_utc") or "")
        print(
            f"{asset:<10} {str(dust_qty):>16} {str(avg_cost):>16} {str(effective):>16} {needs:>10} {updated_at:>22}"
        )
    return 0


def cmd_dust_show(args: argparse.Namespace) -> int:
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)
    asset = str(getattr(args, "asset", "") or "").strip().upper()
    if not asset:
        print("ERROR: missing asset")
        return 2
    with connect(db_path) as conn:
        state = StateManager(conn)
        row = state.get_dust(asset=asset)
        free = state.get_cached_balance_free(asset=asset) or Decimal("0")
        reserved = state.get_open_position_qty_by_asset().get(asset, Decimal("0"))
    if not row:
        print("(not found)")
        return 2
    dust_qty = Decimal(str(row.get("dust_qty") or "0"))
    avg_cost = Decimal(str(row.get("avg_cost_price") or "0"))
    allowed = free - reserved
    if allowed < 0:
        allowed = Decimal("0")
    effective = dust_qty if dust_qty <= allowed else allowed
    print(f"asset={asset}")
    print(f"dust_qty={row.get('dust_qty')}")
    print(f"avg_cost_price={row.get('avg_cost_price')}")
    print(f"needs_reconcile={row.get('needs_reconcile')}")
    print(f"binance_free_cached={str(free)}")
    print(f"open_position_qty={str(reserved)}")
    print(f"effective_dust={str(effective)}")
    return 0


def cmd_pnl_realized_list(args: argparse.Namespace) -> int:
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)
    limit = int(getattr(args, "limit", 50))
    with connect(db_path) as conn:
        cur = conn.execute(
            """
            SELECT execution_id, symbol, order_type,
                   realized_pnl_quote, realized_pnl_quote_asset, pnl_warnings_json,
                   submitted_at_utc, reconciled_at_utc, updated_at_utc
            FROM executions
            WHERE realized_pnl_quote IS NOT NULL
              AND order_type IN ('MARKET_SELL','LIMIT_SELL')
            ORDER BY execution_id DESC
            LIMIT ?
            """,
            (int(limit),),
        )
        rows = [dict(r) for r in cur.fetchall()]
    if not rows:
        print("(no realized PnL rows)")
        return 0
    print(f"Realized PnL: {len(rows)}")
    print(f"{'EXEC_ID':>7} {'SYMBOL':<10} {'TYPE':<11} {'PNL':>18} {'ASSET':<6} {'AT_UTC':<22} {'WARN':<5}")
    for r in rows:
        warn = "yes" if (r.get("pnl_warnings_json") not in (None, "", "[]")) else "no"
        at = str(r.get("reconciled_at_utc") or r.get("submitted_at_utc") or r.get("updated_at_utc") or "")
        print(
            f"{int(r.get('execution_id') or 0):>7} "
            f"{str(r.get('symbol') or '-'): <10} "
            f"{str(r.get('order_type') or '-'): <11} "
            f"{str(r.get('realized_pnl_quote') or '-'): >18} "
            f"{str(r.get('realized_pnl_quote_asset') or '-'): <6} "
            f"{at: <22} "
            f"{warn:<5}"
        )
    return 0


def cmd_pnl_realized_show(args: argparse.Namespace) -> int:
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)
    exec_id = int(getattr(args, "execution_id"))
    with connect(db_path) as conn:
        cur = conn.execute("SELECT * FROM executions WHERE execution_id = ?", (exec_id,))
        row = cur.fetchone()
        if not row:
            print("(not found)")
            return 2
        r = dict(row)
    print(f"execution_id={r.get('execution_id')}")
    print(f"symbol={r.get('symbol')}")
    print(f"order_type={r.get('order_type')}")
    print(f"raw_status={r.get('raw_status')}")
    print(f"local_status={r.get('local_status')}")
    print(f"executed_quantity={r.get('executed_quantity')}")
    print(f"avg_fill_price={r.get('avg_fill_price')}")
    print(f"total_quote_spent={r.get('total_quote_spent')}")
    print(f"fee_breakdown_json={r.get('fee_breakdown_json')}")
    print(f"realized_pnl_quote={r.get('realized_pnl_quote')}")
    print(f"realized_pnl_quote_asset={r.get('realized_pnl_quote_asset')}")
    print(f"pnl_warnings_json={r.get('pnl_warnings_json')}")
    print(f"submitted_at_utc={r.get('submitted_at_utc')}")
    print(f"reconciled_at_utc={r.get('reconciled_at_utc')}")
    return 0


def cmd_pnl_unrealized(args: argparse.Namespace) -> int:
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    cfg = load_config(config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)
    limit = int(getattr(args, "limit", 50))
    position_id = getattr(args, "position_id", None)
    live = not bool(getattr(args, "no_live", False))

    with connect(db_path) as conn:
        state = StateManager(conn)
        if position_id not in (None, ""):
            pos = state.get_position(position_id=int(position_id))
            rows = [pos] if pos else []
        else:
            rows = state.list_positions(status="OPEN", limit=limit)

    if not rows:
        print("(no open positions)")
        return 0

    def _d(v: object, name: str) -> Decimal:
        try:
            return Decimal(str(v))
        except Exception as e:
            raise ValueError(f"Invalid decimal for {name}") from e

    print(f"Unrealized PnL (positions): {len(rows)}" + (" (live)" if live else " (no-live)"))
    print(f"{'POS_ID':>6} {'SYMBOL':<10} {'MD_ENV':<13} {'QTY':>14} {'ENTRY':>14} {'PRICE':>14} {'PNL':>14} {'PNL%':>14}")

    # Create per-env price clients lazily.
    clients: dict[str, BinanceSpotClient] = {}
    for r in rows:
        if not r:
            continue
        if str(r.get("status") or "").upper() != "OPEN":
            continue
        sym = str(r.get("symbol") or "").strip().upper()
        md_env = str(r.get("market_data_environment") or "mainnet_public")
        qty = _d(r.get("quantity") or "0", "quantity")
        entry = _d(r.get("entry_price") or "0", "entry_price")
        price = Decimal("0")
        if live:
            if md_env not in clients:
                clients[md_env] = _price_client_for_market_env(
                    cfg=cfg,
                    market_env=md_env,
                    ca_bundle=getattr(args, "ca_bundle", None),
                    insecure=bool(getattr(args, "insecure", False)),
                )
            try:
                price = _d(clients[md_env].get_ticker_price(symbol=sym), "price")
            except Exception:
                price = Decimal("0")

        market_value = price * qty if live else Decimal("0")
        cost_basis = entry * qty
        unrealized = (market_value - cost_basis) if live else Decimal("0")
        pnl_pct = (unrealized / cost_basis * Decimal("100")) if (live and cost_basis > 0) else Decimal("0")

        print(
            f"{int(r.get('id') or 0):>6} "
            f"{sym:<10} "
            f"{str(md_env):<13} "
            f"{str(qty):>14} "
            f"{str(entry):>14} "
            f"{(str(price) if live else '-'):>14} "
            f"{(str(unrealized) if live else '-'):>14} "
            f"{(str(pnl_pct) if live else '-'):>14}"
        )
    return 0


def _prompt(text: str, *, default: str | None = None) -> str:
    suffix = f" [{default}]" if default is not None else ""
    value = input(f"{text}{suffix}: ").strip()
    return value if value else (default or "")


def _prompt_yes_no(text: str, *, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    v = input(f"{text} ({hint}): ").strip().lower()
    if not v:
        return default
    return v in ("y", "yes", "1", "true", "on")

def _supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return bool(getattr(sys.stdout, "isatty", lambda: False)())


def _style(text: str, *, fg: str | None = None, bold: bool = False) -> str:
    if not _supports_color():
        return text
    codes: list[str] = []
    if bold:
        codes.append("1")
    if fg:
        codes.append(
            {
                "red": "31",
                "green": "32",
                "yellow": "33",
                "blue": "34",
                "magenta": "35",
                "cyan": "36",
                "gray": "90",
            }[fg]
        )
    if not codes:
        return text
    return f"\033[{';'.join(codes)}m{text}\033[0m"


def cmd_menu(args: argparse.Namespace) -> int:
    """
    Interactive wrapper over subcommands (menu UI).
    Business logic stays in the same command handlers used by non-interactive CLI.
    """
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    cfg = load_config(config_path)
    mode = "TESTNET" if cfg.binance_testnet else "MAINNET"

    print()
    print(_style(f"CryptoGent Menu  |  Network: {mode}  |  Base URL: {cfg.binance_base_url}", fg="cyan", bold=True))

    def _net_ns(**kwargs) -> argparse.Namespace:
        # For menu actions that touch the exchange. Defaults are safe.
        return argparse.Namespace(
            config=args.config,
            db=args.db,
            ca_bundle=None,
            insecure=False,
            testnet=False,
            base_url=None,
            **kwargs,
        )

    last_trade_request_id: int | None = None
    last_trade_plan_id: int | None = None
    last_candidate_id: int | None = None

    while True:
        print()
        print(_style(" 1) ", fg="yellow", bold=True) + "Setup")
        print(_style(" 2) ", fg="yellow", bold=True) + "Status")
        print(_style(" 3) ", fg="yellow", bold=True) + "Config")
        print(_style(" 4) ", fg="yellow", bold=True) + "Exchange")
        print(_style(" 5) ", fg="yellow", bold=True) + "Sync")
        print(_style(" 6) ", fg="yellow", bold=True) + "Show (cached)")
        print(_style(" 7) ", fg="yellow", bold=True) + "Trade")
        print(_style(" 8) ", fg="yellow", bold=True) + "Exit")

        choice = input("> ").strip()

        if choice == "1":
            print()
            print(_style("Setup", fg="cyan", bold=True))
            print(" 1) Init (create config + DB)")
            print(" 2) Back")
            sub = input("> ").strip()
            if sub == "1":
                cmd_init(argparse.Namespace(config=args.config, db=args.db))
            continue

        if choice == "2":
            cmd_status(argparse.Namespace(config=args.config, db=args.db))
            continue

        if choice == "3":
            while True:
                print()
                print(_style("Config", fg="cyan", bold=True))
                print(" 1) Show config")
                print(" 2) Use testnet")
                print(" 3) Use mainnet")
                print(" 4) Set mainnet API key/secret (plaintext)")
                print(" 5) Set testnet API key/secret (plaintext)")
                print(" 6) Back")
                sub = input("> ").strip()
                if sub == "1":
                    cmd_config_show(argparse.Namespace(config=args.config, db=args.db))
                elif sub == "2":
                    cmd_config_use_testnet(argparse.Namespace(config=args.config, db=args.db))
                    print(_style("Switched to TESTNET (restart menu header to refresh).", fg="green"))
                elif sub == "3":
                    cmd_config_use_mainnet(argparse.Namespace(config=args.config, db=args.db))
                    print(_style("Switched to MAINNET (restart menu header to refresh).", fg="green"))
                elif sub == "4":
                    api_key = _prompt("BINANCE_API_KEY", default="")
                    api_secret = _prompt("BINANCE_API_SECRET", default="")
                    if not _prompt_yes_no("Store in cryptogent.toml as plaintext?", default=False):
                        continue
                    cmd_config_set_binance(
                        argparse.Namespace(
                            config=args.config,
                            db=args.db,
                            api_key=api_key,
                            api_secret=api_secret,
                            api_secret_stdin=False,
                            testnet=False,
                            base_url=None,
                        )
                    )
                elif sub == "5":
                    api_key = _prompt("BINANCE_TESTNET_API_KEY", default="")
                    api_secret = _prompt("BINANCE_TESTNET_API_SECRET", default="")
                    if not _prompt_yes_no("Store in cryptogent.toml as plaintext?", default=False):
                        continue
                    cmd_config_set_binance_testnet(
                        argparse.Namespace(
                            config=args.config,
                            db=args.db,
                            api_key=api_key,
                            api_secret=api_secret,
                            api_secret_stdin=False,
                        )
                    )
                elif sub == "6":
                    break
                else:
                    print(_style("Invalid choice", fg="red"))
            continue

        if choice == "4":
            while True:
                print()
                print(_style("Exchange (no trading)", fg="cyan", bold=True))
                print(" 1) Ping")
                print(" 2) Time")
                print(" 3) Exchange info")
                print(" 4) Balances (auth)")
                print(" 5) Back")
                sub = input("> ").strip()
                if sub == "1":
                    cmd_exchange_ping(_net_ns())
                elif sub == "2":
                    cmd_exchange_time(_net_ns())
                elif sub == "3":
                    sym = _prompt("Symbol (optional)", default="").upper().strip() or None
                    cmd_exchange_info(_net_ns(symbol=sym))
                elif sub == "4":
                    show_all = _prompt_yes_no("Include zero balances?", default=False)
                    cmd_exchange_balances(_net_ns(all=show_all))
                elif sub == "5":
                    break
                else:
                    print(_style("Invalid choice", fg="red"))
            continue

        if choice == "5":
            while True:
                print()
                print(_style("Sync (writes to SQLite)", fg="cyan", bold=True))
                print(" 1) Startup sync")
                print(" 2) Sync balances")
                print(" 3) Sync open orders")
                print(" 4) Sync fear & greed index")
                print(" 5) Back")
                sub = input("> ").strip()
                if sub == "1":
                    cmd_sync_startup(_net_ns())
                elif sub == "2":
                    cmd_sync_balances(_net_ns())
                elif sub == "3":
                    sym = _prompt("Symbol (optional)", default="").upper().strip() or None
                    cmd_sync_open_orders(_net_ns(symbol=sym))
                elif sub == "4":
                    cmd_sync_fear_greed(_net_ns())
                elif sub == "5":
                    break
                else:
                    print(_style("Invalid choice", fg="red"))
            continue

        if choice == "6":
            while True:
                print()
                print(_style("Show (cached; no network)", fg="cyan", bold=True))
                print(" 1) Show balances")
                print(" 2) Show open orders")
                print(" 3) Show fear & greed index")
                print(" 4) Show audit logs")
                print(" 5) Back")
                sub = input("> ").strip()
                if sub == "1":
                    limit_s = _prompt("How many rows?", default="25")
                    flt = _prompt('Filter assets (optional, exact e.g. "SOL,AI"; substring use "*SOL*")', default="").strip() or None
                    include_zero = _prompt_yes_no("Include zero balances?", default=False)
                    contains = False
                    if flt and flt.startswith("*") and flt.endswith("*") and len(flt) > 2:
                        contains = True
                        flt = flt.strip("*")
                    cmd_show_balances(
                        argparse.Namespace(
                            config=args.config,
                            db=args.db,
                            all=include_zero,
                            limit=int(limit_s),
                            filter=flt,
                            contains=contains,
                        )
                    )
                elif sub == "2":
                    sym = _prompt("Symbol (optional)", default="").upper().strip() or None
                    limit_s = _prompt("How many rows?", default="50")
                    cmd_show_open_orders(argparse.Namespace(config=args.config, db=args.db, symbol=sym, limit=int(limit_s)))
                elif sub == "3":
                    limit_s = _prompt("How many rows?", default="20")
                    cmd_show_fear_greed(argparse.Namespace(config=args.config, db=args.db, limit=int(limit_s)))
                elif sub == "4":
                    limit_s = _prompt("How many entries?", default="50")
                    cmd_show_audit(argparse.Namespace(config=args.config, db=args.db, limit=int(limit_s)))
                elif sub == "5":
                    break
                else:
                    print(_style("Invalid choice", fg="red"))
            continue

        if choice == "7":
            while True:
                print()
                print(_style("Trade (requests; no execution)", fg="cyan", bold=True))
                print(" 1) Start trade (create request)")
                print(" 2) List trade requests")
                print(" 3) Show trade request")
                print(" 4) Cancel trade request")
                print(" 5) Validate trade request")
                print(" 6) Build trade plan (from request)")
                print(" 7) List trade plans")
                print(" 8) Show trade plan")
                print(" 9) Safety validate trade plan")
                print("10) Execute trade candidate (Phase 7)")
                print("11) List executions")
                print("12) Show execution")
                print("13) Cancel LIMIT execution")
                print("14) Back")
                sub = input("> ").strip()

                if sub == "1":
                    while True:
                        paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
                        config_path = ensure_default_config(paths.config_path)
                        cfg = load_config(config_path)

                        profit_target_pct = _prompt("Profit target (%)", default="2.0")
                        stop_loss_pct = _prompt("Stop-loss (%)", default=str(cfg.trading_default_stop_loss_pct))
                        deadline_hours = _prompt("Deadline (hours from now)", default="24")
                        budget_mode = _prompt("Budget mode (manual/auto)", default=str(cfg.trading_default_budget_mode)).lower()
                        budget = _prompt("Budget amount (e.g. 50)", default="50")
                        budget_asset = _prompt("Budget asset (e.g. USDT)", default="USDT").upper()
                        symbol = _prompt("Preferred symbol (e.g. BTCUSDT)", default="BTCUSDT").upper()
                        exit_asset = _prompt("Exit asset", default=str(cfg.trading_default_exit_asset)).upper()
                        label = _prompt("Label (optional)", default="").strip() or None
                        notes = _prompt("Notes (optional)", default="").strip() or None

                        try:
                            validate_trade_request(
                                profit_target_pct=profit_target_pct,
                                stop_loss_pct=stop_loss_pct,
                                deadline=None,
                                deadline_minutes=None,
                                deadline_hours=int(deadline_hours),
                                budget_mode=budget_mode,
                                budget_asset=budget_asset,
                                budget_amount=budget if budget_mode == "manual" else None,
                                preferred_symbol=symbol,
                                exit_asset=exit_asset,
                                label=label,
                                notes=notes,
                            )
                        except ValidationError as e:
                            print(f"Invalid input: {e}")
                            if not _prompt_yes_no("Try again?", default=True):
                                break
                            continue

                        cmd_trade_start(
                            argparse.Namespace(
                                config=args.config,
                                db=args.db,
                                profit_target_pct=profit_target_pct,
                                stop_loss_pct=stop_loss_pct,
                                deadline=None,
                                deadline_minutes=None,
                                deadline_hours=int(deadline_hours),
                                budget_mode=budget_mode,
                                budget=budget if budget_mode == "manual" else None,
                                budget_asset=budget_asset,
                                symbol=symbol,
                                exit_asset=exit_asset,
                                label=label,
                                notes=notes,
                                yes=True,
                            )
                        )
                        try:
                            db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)
                            with connect(db_path) as conn:
                                row = conn.execute("SELECT id FROM trade_requests ORDER BY id DESC LIMIT 1").fetchone()
                                last_trade_request_id = int(row[0]) if row else None
                        except Exception:
                            last_trade_request_id = None

                        if last_trade_request_id is not None and _prompt_yes_no("Validate this request now?", default=False):
                            cmd_trade_validate(_net_ns(id=last_trade_request_id))
                        break
                    continue

                if sub == "2":
                    limit_s = _prompt("Limit rows", default="20")
                    cmd_trade_list(argparse.Namespace(config=args.config, db=args.db, limit=int(limit_s)))
                    continue

                if sub == "3":
                    tid = _prompt("Trade request id", default=str(last_trade_request_id or "")).strip()
                    if not tid:
                        print(_style("No id provided", fg="red"))
                        continue
                    try:
                        cmd_trade_show(argparse.Namespace(config=args.config, db=args.db, id=int(tid)))
                    except ValueError:
                        print(_style("Invalid id", fg="red"))
                    continue

                if sub == "4":
                    tid = _prompt("Trade request id", default=str(last_trade_request_id or "")).strip()
                    if not tid:
                        print(_style("No id provided", fg="red"))
                        continue
                    try:
                        cmd_trade_cancel(argparse.Namespace(config=args.config, db=args.db, id=int(tid)))
                    except ValueError:
                        print(_style("Invalid id", fg="red"))
                    continue

                if sub == "5":
                    tid = _prompt("Trade request id", default=str(last_trade_request_id or "")).strip()
                    if not tid:
                        print(_style("No id provided", fg="red"))
                        continue
                    try:
                        cmd_trade_validate(_net_ns(id=int(tid)))
                    except ValueError:
                        print(_style("Invalid id", fg="red"))
                    continue

                if sub == "6":
                    tid = _prompt("Trade request id", default=str(last_trade_request_id or "")).strip()
                    if not tid:
                        print(_style("No id provided", fg="red"))
                        continue
                    try:
                        cmd_trade_plan(_net_ns(id=int(tid), candle_interval="5m", candle_count=288))
                        try:
                            db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)
                            with connect(db_path) as conn:
                                row = conn.execute("SELECT id FROM trade_plans ORDER BY id DESC LIMIT 1").fetchone()
                                last_trade_plan_id = int(row[0]) if row else None
                        except Exception:
                            last_trade_plan_id = None
                    except ValueError:
                        print(_style("Invalid id", fg="red"))
                    continue

                if sub == "7":
                    limit_s = _prompt("Limit rows", default="20")
                    cmd_trade_plan_list(argparse.Namespace(config=args.config, db=args.db, limit=int(limit_s)))
                    continue

                if sub == "8":
                    pid = _prompt("Trade plan id", default=str(last_trade_plan_id or "")).strip()
                    if not pid:
                        print(_style("No id provided", fg="red"))
                        continue
                    try:
                        cmd_trade_plan_show(argparse.Namespace(config=args.config, db=args.db, plan_id=int(pid)))
                    except ValueError:
                        print(_style("Invalid id", fg="red"))
                    continue

                if sub == "9":
                    pid = _prompt("Trade plan id", default=str(last_trade_plan_id or "")).strip()
                    if not pid:
                        print(_style("No id provided", fg="red"))
                        continue
                    order_type = _prompt(
                        "Order type (MARKET_BUY/LIMIT_BUY/MARKET_SELL/LIMIT_SELL)",
                        default="MARKET_BUY",
                    ).strip().upper()
                    limit_price = None
                    if order_type in ("LIMIT_BUY", "LIMIT_SELL"):
                        limit_price = _prompt("Limit price", default="").strip() or None
                    close_mode = "all"
                    close_amount = None
                    close_percent = None
                    if order_type.endswith("_SELL"):
                        close_mode = _prompt("Close mode (amount/percent/all)", default="all").strip().lower() or "all"
                        if close_mode == "amount":
                            close_amount = _prompt("Close amount (base qty)", default="").strip() or None
                        elif close_mode == "percent":
                            close_percent = _prompt("Close percent (0-100)", default="").strip() or None
                    try:
                        cmd_trade_safety(
                            _net_ns(
                                plan_id=int(pid),
                                max_age_minutes=60,
                                price_drift_warn_pct="1.0",
                                price_drift_unsafe_pct="3.0",
                                max_position_pct="25",
                                max_stop_loss_pct="10",
                                order_type=order_type,
                                limit_price=limit_price,
                                close_mode=close_mode,
                                close_amount=close_amount,
                                close_percent=close_percent,
                            )
                        )
                        try:
                            db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)
                            with connect(db_path) as conn:
                                row = conn.execute(
                                    "SELECT id FROM execution_candidates ORDER BY id DESC LIMIT 1"
                                ).fetchone()
                                last_candidate_id = int(row[0]) if row else None
                        except Exception:
                            last_candidate_id = None
                    except ValueError:
                        print(_style("Invalid id", fg="red"))
                    continue

                if sub == "10":
                    cid = _prompt("Candidate id", default=str(last_candidate_id or "")).strip()
                    if not cid:
                        print(_style("No id provided", fg="red"))
                        continue
                    try:
                        cmd_trade_execute(_net_ns(candidate_id=int(cid), yes=False))
                    except ValueError:
                        print(_style("Invalid id", fg="red"))
                    continue

                if sub == "11":
                    limit_s = _prompt("Limit rows", default="20")
                    cmd_trade_executions_list(argparse.Namespace(config=args.config, db=args.db, limit=int(limit_s)))
                    continue

                if sub == "12":
                    eid = _prompt("Execution id", default="").strip()
                    if not eid:
                        print(_style("No id provided", fg="red"))
                        continue
                    try:
                        cmd_trade_executions_show(argparse.Namespace(config=args.config, db=args.db, execution_id=int(eid)))
                    except ValueError:
                        print(_style("Invalid id", fg="red"))
                    continue

                if sub == "13":
                    eid = _prompt("Execution id", default="").strip()
                    if not eid:
                        print(_style("No id provided", fg="red"))
                        continue
                    try:
                        cmd_trade_execution_cancel(_net_ns(execution_id=int(eid)))
                    except ValueError:
                        print(_style("Invalid id", fg="red"))
                    continue

                if sub == "14":
                    break
                print(_style("Invalid choice", fg="red"))
            continue

        if choice == "8":
            return 0

        print(_style("Invalid choice", fg="red"))


def cmd_trade_start(args: argparse.Namespace) -> int:
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    cfg = load_config(config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)

    budget_mode = args.budget_mode or cfg.trading_default_budget_mode
    exit_asset = args.exit_asset or cfg.trading_default_exit_asset
    stop_loss_default = cfg.trading_default_stop_loss_pct

    try:
        req = validate_trade_request(
            profit_target_pct=args.profit_target_pct,
            stop_loss_pct=args.stop_loss_pct or stop_loss_default,
            deadline=args.deadline,
            deadline_minutes=args.deadline_minutes,
            deadline_hours=args.deadline_hours,
            budget_mode=budget_mode,
            budget_asset=args.budget_asset,
            budget_amount=args.budget,
            preferred_symbol=args.symbol,
            exit_asset=exit_asset,
            label=args.label,
            notes=args.notes,
        )
    except ValidationError as e:
        print(f"Invalid trade request: {e}")
        return 2

    # Confirmation step (interactive unless --yes).
    if not getattr(args, "yes", False):
        print("Trade Request Summary")
        print(f"- Target Profit: {req.profit_target_pct}%")
        print(f"- Stop-Loss: {req.stop_loss_pct}%")
        print(f"- Deadline: {req.deadline_utc.isoformat()}")
        print(f"- Budget Mode: {req.budget_mode}")
        if req.budget_mode == "manual":
            print(f"- Budget: {req.budget_amount} {req.budget_asset}")
        else:
            print(f"- Budget Asset: {req.budget_asset}")
        print(f"- Preferred Symbol: {req.preferred_symbol}")
        print(f"- Exit Asset: {req.exit_asset}")
        if req.label:
            print(f"- Label: {req.label}")
        if req.notes:
            print(f"- Notes: {req.notes}")
        if not _prompt_yes_no("Confirm?", default=False):
            print("Cancelled")
            return 2

    with connect(db_path) as conn:
        state = StateManager(conn)
        trade_id = state.create_trade_request(req)
    print(f"Created trade request id={trade_id} status=DRAFT")
    return 0


def cmd_trade_list(args: argparse.Namespace) -> int:
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)
    with connect(db_path) as conn:
        state = StateManager(conn)
        rows = state.list_trade_requests(limit=args.limit)
    if not rows:
        print("(no trade requests)")
        return 0

    def _cell(v: object, width: int) -> str:
        s = "" if v is None else str(v)
        if len(s) > width:
            return s[: max(0, width - 1)] + "…"
        return s

    print(f"Trade requests: {len(rows)}")
    print(
        f"{'ID':>4} {'REQUEST_ID':<10} {'STATUS':<9} {'SYMBOL':<10} {'BUDGET':<16} "
        f"{'PT%':>6} {'SL%':>6} {'DEADLINE (UTC)':<22} {'VALID':<7}"
    )
    for r in rows:
        status = r.get("status")
        status_display = "DRAFT" if status == "NEW" else status
        budget_mode = (r.get("budget_mode") or "").upper()
        budget_amt = r.get("budget_amount")
        budget_asset = r.get("budget_asset")
        budget_display = f"{budget_mode}:{budget_amt} {budget_asset}" if budget_amt is not None else f"{budget_mode}:{budget_asset}"

        print(
            f"{int(r['id']):>4} "
            f"{_cell(r.get('request_id') or '-', 10):<10} "
            f"{_cell(status_display or '-', 9):<9} "
            f"{_cell(r.get('preferred_symbol') or '-', 10):<10} "
            f"{_cell(budget_display, 16):<16} "
            f"{_cell(str(r.get('profit_target_pct') or ''), 6):>6} "
            f"{_cell(str(r.get('stop_loss_pct') or ''), 6):>6} "
            f"{_cell(r.get('deadline_utc') or '-', 22):<22} "
            f"{_cell(r.get('validation_status') or '-', 7):<7}"
        )
    return 0


def cmd_trade_show(args: argparse.Namespace) -> int:
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)
    with connect(db_path) as conn:
        state = StateManager(conn)
        row = state.get_trade_request(args.id)
    if not row:
        print("(not found)")
        return 2
    print(f"id={row['id']}")
    print(f"request_id={row.get('request_id')}")
    status = row.get("status")
    print(f"status={'DRAFT' if status == 'NEW' else status}")
    print(f"preferred_symbol={row['preferred_symbol']}")
    if "budget_mode" in row:
        print(f"budget_mode={row.get('budget_mode')}")
    print(f"budget={row['budget_amount']} {row['budget_asset']}")
    if "exit_asset" in row:
        print(f"exit_asset={row.get('exit_asset')}")
    if "label" in row:
        print(f"label={row.get('label')}")
    if "notes" in row:
        print(f"notes={row.get('notes')}")
    print(f"profit_target_pct={row['profit_target_pct']}")
    print(f"stop_loss_pct={row['stop_loss_pct']}")
    if "deadline_hours" in row:
        print(f"deadline_hours={row.get('deadline_hours')}")
    print(f"deadline_utc={row['deadline_utc']}")
    if "validation_status" in row:
        print(f"validation_status={row.get('validation_status')}")
        print(f"validation_error={row.get('validation_error')}")
        print(f"validated_at_utc={row.get('validated_at_utc')}")
        print(f"last_price={row.get('last_price')}")
        print(f"estimated_qty={row.get('estimated_qty')}")
        print(f"symbol_base_asset={row.get('symbol_base_asset')}")
        print(f"symbol_quote_asset={row.get('symbol_quote_asset')}")
    print(f"created_at_utc={row['created_at_utc']}")
    print(f"updated_at_utc={row['updated_at_utc']}")
    return 0


def cmd_trade_cancel(args: argparse.Namespace) -> int:
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)
    with connect(db_path) as conn:
        state = StateManager(conn)
        ok = state.cancel_trade_request(args.id)
    if not ok:
        print("Not cancelled (not found or not NEW)")
        return 2
    print("Cancelled")
    return 0


def cmd_trade_validate(args: argparse.Namespace) -> int:
    client = _client_from_args(args)
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)

    with connect(db_path) as conn:
        state = StateManager(conn)
        row = state.get_trade_request(args.id)
        if not row:
            print("(not found)")
            return 2
        if row.get("status") in ("CANCELLED",):
            print(f"Trade request is CANCELLED; not validating.")
            return 2
        symbol = row.get("preferred_symbol")
        if not symbol:
            print("Trade request has no preferred_symbol; set one when creating the request.")
            return 2
        deadline_s = str(row.get("deadline_utc") or "")
        try:
            deadline = datetime.fromisoformat(deadline_s.replace("Z", "+00:00")).astimezone(UTC)
        except ValueError:
            print("Invalid stored deadline_utc")
            return 2
        if deadline <= datetime.now(UTC):
            err = "deadline already passed"
            with connect(db_path) as conn2:
                StateManager(conn2).set_trade_request_validation(
                    trade_request_id=args.id,
                    validation_status="INVALID",
                    validation_error=err,
                    last_price=None,
                    estimated_qty=None,
                    symbol_base_asset=None,
                    symbol_quote_asset=None,
                )
            print(f"INVALID: {err}")
            return 2
        budget_asset = str(row.get("budget_asset") or "")
        try:
            budget_amount = Decimal(str(row.get("budget_amount")))
        except (InvalidOperation, ValueError):
            print("Invalid stored budget_amount")
            return 2
        try:
            profit_target_pct = Decimal(str(row.get("profit_target_pct")))
            stop_loss_pct = Decimal(str(row.get("stop_loss_pct")))
        except (InvalidOperation, ValueError):
            profit_target_pct = None
            stop_loss_pct = None
        deadline_hours = int(row.get("deadline_hours") or 0)

    try:
        info = client.get_symbol_info(symbol=str(symbol))
        if not info:
            err = "symbol not found in exchangeInfo"
            with connect(db_path) as conn:
                StateManager(conn).set_trade_request_validation(
                    trade_request_id=args.id,
                    validation_status="INVALID",
                    validation_error=err,
                    last_price=None,
                    estimated_qty=None,
                    symbol_base_asset=None,
                    symbol_quote_asset=None,
                )
            print(f"INVALID: {err}")
            return 2

        rules = parse_symbol_rules(info)
        price_s = client.get_ticker_price(symbol=rules.symbol)
        last_price = Decimal(price_s)
        res = precheck_market_buy(
            rules=rules,
            budget_asset=budget_asset,
            budget_amount=budget_amount,
            last_price=last_price,
        )
    except (BinanceAPIError, RuleError, InvalidOperation, ValueError) as e:
        err = str(e)
        with connect(db_path) as conn:
            StateManager(conn).set_trade_request_validation(
                trade_request_id=args.id,
                validation_status="ERROR",
                validation_error=err,
                last_price=None,
                estimated_qty=None,
                symbol_base_asset=None,
                symbol_quote_asset=None,
            )
        print(f"ERROR: {err}")
        return 2

    with connect(db_path) as conn:
        ok = StateManager(conn).set_trade_request_validation(
            trade_request_id=args.id,
            validation_status="VALID" if res.ok else "INVALID",
            validation_error=res.error,
            last_price=str(last_price),
            estimated_qty=str(res.estimated_qty) if res.estimated_qty is not None else None,
            symbol_base_asset=rules.base_asset,
            symbol_quote_asset=rules.quote_asset,
        )
    if not ok:
        print("Not updated (trade request not found or not NEW)")
        return 2

    if res.ok:
        # Feasibility gate (planning-oriented). Validation is only VALID if feasibility can be computed and is not_feasible.
        if profit_target_pct is None or stop_loss_pct is None or deadline_hours <= 0:
            err = "missing_trade_request_fields_for_feasibility"
            with connect(db_path) as conn:
                StateManager(conn).set_trade_request_validation(
                    trade_request_id=args.id,
                    validation_status="ERROR",
                    validation_error=err,
                    last_price=str(last_price),
                    estimated_qty=str(res.estimated_qty) if res.estimated_qty is not None else None,
                    symbol_base_asset=rules.base_asset,
                    symbol_quote_asset=rules.quote_asset,
                )
            print(f"ERROR: {err}")
            return 2

        try:
            market_client = BinanceSpotClient(
                base_url=BINANCE_SPOT_BASE_URL,
                api_key=None,
                api_secret=None,
                recv_window_ms=client.recv_window_ms,
                timeout_s=client.timeout_s,
                tls_verify=client.tls_verify,
                ca_bundle_path=client.ca_bundle_path,
            )
            snapshot = fetch_market_snapshot(
                client=market_client,
                symbol=rules.symbol,
                candle_interval="5m",
                candle_count=288,
                fetch_book_ticker=True,
            )
            md_warnings, hard = freshness_and_consistency_checks(snapshot=snapshot, candle_interval="5m", candle_count=288)
            if hard:
                feas_category = "not_feasible"
                feas_reason = hard
                feas_warnings = md_warnings
            else:
                spread_available = snapshot.bid is not None and snapshot.ask is not None and snapshot.spread_pct is not None
                feas = evaluate_feasibility(
                    profit_target_pct=profit_target_pct,
                    stop_loss_pct=stop_loss_pct,
                    deadline_hours=deadline_hours,
                    volume_24h_quote=snapshot.volume_24h_quote,
                    volatility_pct=snapshot.candles.volatility_pct,
                    spread_pct=snapshot.spread_pct,
                    spread_available=spread_available,
                    warnings=md_warnings,
                )
                feas_category = feas.category
                feas_reason = feas.rejection_reason
                feas_warnings = feas.warnings
        except (BinanceAPIError, MarketDataError, FeasibilityError, ValueError) as e:
            err = f"feasibility_unavailable: {e}"
            with connect(db_path) as conn:
                StateManager(conn).set_trade_request_validation(
                    trade_request_id=args.id,
                    validation_status="ERROR",
                    validation_error=err,
                    last_price=str(last_price),
                    estimated_qty=str(res.estimated_qty) if res.estimated_qty is not None else None,
                    symbol_base_asset=rules.base_asset,
                    symbol_quote_asset=rules.quote_asset,
                )
            print(f"ERROR: {err}")
            return 2

        if feas_category == "not_feasible":
            err = f"not_feasible: {feas_reason or 'unknown'}"
            with connect(db_path) as conn:
                StateManager(conn).set_trade_request_validation(
                    trade_request_id=args.id,
                    validation_status="INVALID",
                    validation_error=err,
                    last_price=str(last_price),
                    estimated_qty=str(res.estimated_qty) if res.estimated_qty is not None else None,
                    symbol_base_asset=rules.base_asset,
                    symbol_quote_asset=rules.quote_asset,
                )
            warnings_s = ",".join(feas_warnings) if feas_warnings else "-"
            print(
                f"INVALID: {err} (symbol={rules.symbol} price={last_price} est_qty={res.estimated_qty} notional={res.notional} warnings={warnings_s})"
            )
            return 2

        extra = None
        if feas_category in ("feasible_with_warning", "high_risk"):
            warnings_s = ",".join(feas_warnings) if feas_warnings else "-"
            extra = f"feasibility={feas_category}; warnings={warnings_s}"

        with connect(db_path) as conn:
            StateManager(conn).set_trade_request_validation(
                trade_request_id=args.id,
                validation_status="VALID",
                validation_error=extra,
                last_price=str(last_price),
                estimated_qty=str(res.estimated_qty) if res.estimated_qty is not None else None,
                symbol_base_asset=rules.base_asset,
                symbol_quote_asset=rules.quote_asset,
            )

        print(f"VALID: symbol={rules.symbol} price={last_price} est_qty={res.estimated_qty} notional={res.notional} {rules.quote_asset}")
        if extra:
            print(f"FEASIBILITY: {extra} market_data_env=mainnet_public")
        else:
            print("FEASIBILITY: feasible market_data_env=mainnet_public")
        return 0
    print(
        f"INVALID: {res.error} (symbol={rules.symbol} price={last_price} est_qty={res.estimated_qty} notional={res.notional})"
    )
    return 2


def cmd_trade_plan(args: argparse.Namespace) -> int:
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    cfg = load_config(config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)

    with connect(db_path) as conn:
        state = StateManager(conn)
        row = state.get_trade_request(args.id)
        if not row:
            print("(not found)")
            return 2
        if row.get("status") in ("CANCELLED",):
            print("Trade request is CANCELLED; not planning.")
            return 2
        if str(row.get("validation_status") or "").upper() != "VALID":
            print("Trade request is not VALID; run validation before planning.")
            return 2

        try:
            exec_client = _client_from_args(args)
            ca_bundle = args.ca_bundle.expanduser() if getattr(args, "ca_bundle", None) else None
            market_client = BinanceSpotClient(
                base_url=BINANCE_SPOT_BASE_URL,
                api_key=None,
                api_secret=None,
                recv_window_ms=exec_client.recv_window_ms,
                timeout_s=exec_client.timeout_s,
                tls_verify=exec_client.tls_verify,
                ca_bundle_path=ca_bundle,
            )
            exec_env = "testnet" if "testnet.binance.vision" in (exec_client.base_url or "") else "mainnet"
            plan = build_trade_plan(
                cfg=cfg,
                state=state,
                trade_request=row,
                market_client=market_client,
                execution_client=exec_client,
                execution_environment=exec_env,
                candle_interval=str(getattr(args, "candle_interval", "5m")),
                candle_count=int(getattr(args, "candle_count", 288)),
            )
            plan_id = persist_trade_plan(state=state, plan=plan)
        except (PlanningError, BinanceAPIError, RuleError, ValueError) as e:
            err = str(e)
            state.append_audit(level="ERROR", event="trade_plan_failed", details={"trade_request_id": int(args.id), "error": err})
            print(f"ERROR: {err}")
            return 2

    print("Trade Planning Summary")
    print(f"- Plan ID: {plan_id}")
    print(f"- Trade Request ID: {plan.trade_request_id}")
    if plan.request_id:
        print(f"- Request ID: {plan.request_id}")
    print(f"- Market Data Env: {plan.market_data_environment}")
    print(f"- Execution Env: {plan.execution_environment}")
    print(f"- Symbol: {plan.symbol}")
    print(f"- Feasibility: {plan.feasibility_category}")
    if plan.approved_budget_amount is not None:
        print(f"- Budget Approved: {plan.approved_budget_amount} {plan.approved_budget_asset}")
    if plan.usable_budget_amount is not None:
        print(f"- Budget Usable: {plan.usable_budget_amount} {plan.approved_budget_asset}")
    if plan.rounded_quantity is not None and plan.expected_notional is not None:
        print(f"- Est. Qty: {plan.rounded_quantity} ({plan.expected_notional} {plan.approved_budget_asset})")
    print(f"- Signal: {plan.signal.upper()} (confidence={plan.signal_confidence})")
    if plan.warnings:
        print("- Warnings:")
        for w in plan.warnings:
            print(f"  - {w}")
    return 0 if plan.feasibility_category != "not_feasible" else 2


def cmd_trade_plan_list(args: argparse.Namespace) -> int:
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)
    with connect(db_path) as conn:
        rows = StateManager(conn).list_trade_plans(limit=int(getattr(args, "limit", 20)))
    if not rows:
        print("(no trade plans)")
        return 0

    def _cell(v: object, width: int) -> str:
        s = "" if v is None else str(v)
        if len(s) > width:
            return s[: max(0, width - 1)] + "…"
        return s

    print(f"Trade plans: {len(rows)}")
    print(
        f"{'PLAN_ID':>7} {'REQ_ID':<10} {'TR_ID':>5} {'SYMBOL':<10} {'CATEGORY':<18} "
        f"{'BUDGET':<16} {'STATUS':<18} {'WARN':>4} {'CREATED (UTC)':<22}"
    )
    for r in rows:
        warnings_json = r.get("warnings_json") or "[]"
        warn_n = 0
        try:
            import json as _json

            parsed = _json.loads(warnings_json)
            warn_n = len(parsed) if isinstance(parsed, list) else 0
        except Exception:
            warn_n = 0
        budget_amt = r.get("approved_budget_amount")
        budget_asset = r.get("approved_budget_asset")
        budget_display = f"{budget_amt} {budget_asset}" if budget_amt is not None else f"{budget_asset}"
        print(
            f"{int(r['id']):>7} "
            f"{_cell(r.get('request_id') or '-', 10):<10} "
            f"{int(r.get('trade_request_id') or 0):>5} "
            f"{_cell(r.get('symbol') or '-', 10):<10} "
            f"{_cell(r.get('feasibility_category') or '-', 18):<18} "
            f"{_cell(budget_display, 16):<16} "
            f"{_cell(r.get('status') or '-', 18):<18} "
            f"{warn_n:>4} "
            f"{_cell(r.get('created_at_utc') or '-', 22):<22}"
        )
    return 0


def cmd_trade_plan_show(args: argparse.Namespace) -> int:
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)
    with connect(db_path) as conn:
        row = StateManager(conn).get_trade_plan(plan_id=int(args.plan_id))
    if not row:
        print("(not found)")
        return 2
    print(f"plan_id={row.get('id')}")
    print(f"trade_request_id={row.get('trade_request_id')}")
    print(f"request_id={row.get('request_id')}")
    print(f"status={row.get('status')}")
    print(f"feasibility_category={row.get('feasibility_category')}")
    print(f"warnings_json={row.get('warnings_json')}")
    print(f"rejection_reason={row.get('rejection_reason')}")
    print(f"market_data_environment={row.get('market_data_environment')}")
    print(f"execution_environment={row.get('execution_environment')}")
    print(f"symbol={row.get('symbol')}")
    print(f"price={row.get('price')}")
    print(f"bid={row.get('bid')}")
    print(f"ask={row.get('ask')}")
    print(f"spread_pct={row.get('spread_pct')}")
    print(f"volume_24h_quote={row.get('volume_24h_quote')}")
    print(f"volatility_pct={row.get('volatility_pct')}")
    print(f"momentum_pct={row.get('momentum_pct')}")
    print(f"budget_mode={row.get('budget_mode')}")
    print(f"approved_budget={row.get('approved_budget_amount')} {row.get('approved_budget_asset')}")
    print(f"usable_budget_amount={row.get('usable_budget_amount')}")
    print(f"raw_quantity={row.get('raw_quantity')}")
    print(f"rounded_quantity={row.get('rounded_quantity')}")
    print(f"expected_notional={row.get('expected_notional')}")
    print(f"signal={row.get('signal')}")
    print(f"signal_reasons_json={row.get('signal_reasons_json')}")
    print(f"rules_snapshot_json={row.get('rules_snapshot_json')}")
    print(f"market_summary_json={row.get('market_summary_json')}")
    print(f"candidate_list_json={row.get('candidate_list_json')}")
    print(f"created_at_utc={row.get('created_at_utc')}")
    return 0


def cmd_trade_safety(args: argparse.Namespace) -> int:
    client = _client_from_args(args)
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)

    with connect(db_path) as conn:
        state = StateManager(conn)
        plan = state.get_trade_plan(plan_id=int(args.plan_id))
        if not plan:
            print("(not found)")
            return 2
        tr_id = int(plan.get("trade_request_id") or 0)
        trade_request = state.get_trade_request(tr_id)
        if not trade_request:
            print("Missing linked trade_request")
            return 2

        order_type = str(getattr(args, "order_type", "MARKET_BUY")).strip().upper()
        limit_price_s = getattr(args, "limit_price", None)
        limit_price = None
        if limit_price_s not in (None, ""):
            try:
                limit_price = Decimal(str(limit_price_s))
            except Exception:
                limit_price = None
        close_mode = str(getattr(args, "close_mode", "all") or "all").strip().lower()
        close_amount_s = getattr(args, "close_amount", None)
        close_percent_s = getattr(args, "close_percent", None)
        position_id = getattr(args, "position_id", None)

        try:
            decision = evaluate_safety(
                state=state,
                execution_client=client,
                plan=plan,
                trade_request=trade_request,
                order_type=order_type,
                limit_price=limit_price,
                position_id=int(position_id) if position_id not in (None, "") else None,
                close_mode=close_mode,
                close_amount=Decimal(str(close_amount_s)) if close_amount_s not in (None, "") else None,
                close_percent=Decimal(str(close_percent_s)) if close_percent_s not in (None, "") else None,
                max_plan_age_minutes=int(getattr(args, "max_age_minutes", 60)),
                max_price_drift_warning_pct=Decimal(str(getattr(args, "price_drift_warn_pct", "1.0"))),
                max_price_drift_unsafe_pct=Decimal(str(getattr(args, "price_drift_unsafe_pct", "3.0"))),
                max_position_pct=Decimal(str(getattr(args, "max_position_pct", "25"))),
                max_stop_loss_pct=Decimal(str(getattr(args, "max_stop_loss_pct", "10"))),
            )
        except (SafetyError, BinanceAPIError, InvalidOperation, ValueError) as e:
            err = str(e)
            state.append_audit(level="ERROR", event="trade_safety_failed", details={"plan_id": int(args.plan_id), "error": err})
            print(f"ERROR: {err}")
            return 2

        details_json = None
        try:
            import json as _json

            details_json = _json.dumps(decision.details, separators=(",", ":"))
        except Exception:
            details_json = None

        side = "sell" if order_type.endswith("_SELL") else "buy"
        position_id_for_candidate: int | None = None
        if position_id not in (None, ""):
            try:
                position_id_for_candidate = int(position_id)
            except Exception:
                position_id_for_candidate = None
        if position_id_for_candidate is None:
            try:
                pid = decision.details.get("position_id") if isinstance(decision.details, dict) else None
                if pid not in (None, ""):
                    position_id_for_candidate = int(pid)
            except Exception:
                position_id_for_candidate = None

        candidate_id = state.create_execution_candidate(
            trade_plan_id=int(plan.get("id")),
            trade_request_id=tr_id,
            request_id=plan.get("request_id"),
            symbol=str(plan.get("symbol") or ""),
            side=side,
            order_type=order_type,
            limit_price=str(limit_price) if limit_price is not None else None,
            execution_environment=str(plan.get("execution_environment") or ""),
            position_id=position_id_for_candidate,
            validation_status=decision.validation_status,
            risk_status=decision.risk_status,
            approved_budget_asset=decision.approved_budget_asset,
            approved_budget_amount=str(decision.approved_budget_amount),
            approved_quantity=str(decision.approved_quantity),
            execution_ready=decision.category in ("safe", "safe_with_warning"),
            summary=decision.summary,
            details_json=details_json,
        )

    print("Safety Evaluation Summary")
    print(f"- Plan ID: {int(args.plan_id)}")
    print(f"- Candidate ID: {candidate_id}")
    print(f"- Result: {decision.category}")
    print(f"- Validation: {decision.validation_status}")
    print(f"- Risk: {decision.risk_status}")
    if str(getattr(args, "order_type", "")).strip().upper().endswith("_SELL"):
        print(f"- Approved Quantity: {decision.approved_quantity}")
        print(f"- Estimated Proceeds: {decision.approved_budget_amount} {decision.approved_budget_asset}")
    else:
        print(f"- Approved Budget: {decision.approved_budget_amount} {decision.approved_budget_asset}")
        print(f"- Approved Quantity: {decision.approved_quantity}")
    if decision.warnings:
        print("- Warnings:")
        for w in decision.warnings:
            print(f"  - {w}")
    if decision.errors:
        print("- Errors:")
        for e in decision.errors:
            print(f"  - {e}")
    print(f"- Summary: {decision.summary}")
    return 0 if decision.category in ("safe", "safe_with_warning") else 2


def cmd_trade_execute(args: argparse.Namespace) -> int:
    client = _client_from_args(args)
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    cfg = load_config(config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)

    runtime_env = "testnet" if cfg.binance_testnet else "mainnet"

    post_sync_bal: object | None = None
    post_sync_oo: object | None = None

    with connect(db_path) as conn:
        state = StateManager(conn)
        cand = state.get_execution_candidate(candidate_id=int(args.candidate_id))
        if not cand:
            print("(not found)")
            return 2

        if state.has_nonterminal_execution_for_candidate(candidate_id=int(args.candidate_id)):
            print("Rejected: this candidate already has an execution attempt (use a new candidate)")
            return 2

        # Hard gates (no execution row for gate failures).
        if int(cand.get("execution_ready") or 0) != 1:
            reason = str(cand.get("summary") or "not execution-ready").strip()
            details = cand.get("details_json")
            err_text = ""
            if details:
                try:
                    parsed = _json.loads(details)
                    errs = parsed.get("errors") if isinstance(parsed, dict) else None
                    if isinstance(errs, list) and errs:
                        err_text = "; errors=" + ",".join([str(e) for e in errs])
                except Exception:
                    pass
            print(f"Rejected: not execution-ready ({reason}){err_text}")
            return 2
        if str(cand.get("risk_status") or "") not in ("approved", "approved_with_warning"):
            print(f"Rejected: risk_status={cand.get('risk_status')}")
            return 2
        if str(cand.get("validation_status") or "") != "passed":
            print(f"Rejected: validation_status={cand.get('validation_status')}")
            return 2

        plan_id = int(cand.get("trade_plan_id") or 0)
        plan = state.get_trade_plan(plan_id=plan_id)
        if not plan:
            print("Rejected: missing linked trade plan")
            return 2

        cand_env = str(cand.get("execution_environment") or "").strip().lower()
        if cand_env not in ("mainnet", "testnet"):
            print("Rejected: invalid candidate execution_environment")
            return 2
        if cand_env != runtime_env:
            print(f"Rejected: environment mismatch candidate={cand_env} runtime={runtime_env}")
            return 2
        plan_env = str(plan.get("execution_environment") or "").strip().lower()
        if plan_env not in ("mainnet", "testnet"):
            print("Rejected: invalid plan execution_environment")
            return 2
        if plan_env != cand_env:
            print(f"Rejected: environment mismatch plan={plan_env} candidate={cand_env}")
            return 2

        # Confirm quote asset matches budget asset via rules snapshot.
        try:
            import json as _json

            rules_snapshot = _json.loads(str(plan.get("rules_snapshot_json") or ""))
        except Exception:
            print("Rejected: missing/invalid rules_snapshot_json")
            return 2
        if not isinstance(rules_snapshot, dict):
            print("Rejected: invalid rules_snapshot_json")
            return 2

        order_type = str(cand.get("order_type") or "").strip().upper()
        limit_price = cand.get("limit_price")
        if order_type not in ("MARKET_BUY", "LIMIT_BUY", "MARKET_SELL", "LIMIT_SELL"):
            print(f"Rejected: invalid order_type={order_type}")
            return 2
        if order_type in ("LIMIT_BUY", "LIMIT_SELL") and (limit_price in (None, "")):
            print(f"Rejected: {order_type} requires limit_price in candidate")
            return 2

        # SELL execution availability check (reservation-aware).
        if order_type.endswith("_SELL"):
            pos_id = cand.get("position_id")
            if pos_id in (None, ""):
                print("Rejected: missing position_id for SELL candidate")
                return 2
            pos = state.get_position(position_id=int(pos_id))
            if not pos or str(pos.get("status") or "").upper() != "OPEN":
                print("Rejected: position not open or not found")
                return 2
            try:
                pos_qty = Decimal(str(pos.get("quantity") or "0"))
            except Exception:
                pos_qty = Decimal("0")
            reserved_qty = state.get_position_reserved_sell_qty(position_id=int(pos_id))
            try:
                acct = client.get_account()
                balances = acct.get("balances", [])
                base_asset = str(rules_snapshot.get("base_asset") or "").strip().upper()
                free_base = Decimal("0")
                if isinstance(balances, list):
                    for b in balances:
                        if isinstance(b, dict) and str(b.get("asset") or "").upper() == base_asset:
                            free_base = Decimal(str(b.get("free") or "0"))
                            break
            except Exception:
                free_base = Decimal("0")
            max_tradable = min(pos_qty, free_base)
            available_to_sell = max(Decimal("0"), max_tradable - reserved_qty)
            try:
                approved_qty = Decimal(str(cand.get("approved_quantity") or "0"))
            except Exception:
                approved_qty = Decimal("0")
            if approved_qty > available_to_sell:
                print(
                    "Rejected: insufficient available-to-sell "
                    f"(approved={approved_qty} available={available_to_sell} reserved={reserved_qty} pos_qty={pos_qty})"
                )
                return 2

        # Confirmation prompt (unless --yes).
        if not getattr(args, "yes", False):
            budget_amt = cand.get("approved_budget_amount")
            budget_asset = cand.get("approved_budget_asset")
            sym = cand.get("symbol")
            side = str(cand.get("side") or "").strip().upper() or ("SELL" if order_type.endswith("_SELL") else "BUY")
            print("Execution Summary")
            print(f"- Candidate ID: {cand.get('id')}")
            print(f"- Plan ID: {plan_id}")
            print(f"- Symbol: {sym}")
            print(f"- Side: {side}")
            if order_type == "MARKET_BUY":
                print(f"- Type: MARKET BUY (quoteOrderQty)")
            elif order_type == "LIMIT_BUY":
                print(f"- Type: LIMIT BUY (GTC)")
                print(f"- Limit Price: {limit_price}")
            elif order_type == "MARKET_SELL":
                print(f"- Type: MARKET SELL (quantity)")
            else:
                print(f"- Type: LIMIT SELL (GTC)")
                print(f"- Limit Price: {limit_price}")
            if order_type.endswith("_BUY"):
                print(f"- Approved Budget: {budget_amt} {budget_asset}")
            else:
                print(f"- Approved Quantity: {cand.get('approved_quantity')}")
            print(f"- Environment: {runtime_env}")
            if not _prompt_yes_no("Execute now?", default=False):
                print("Cancelled")
                return 2

        try:
            if order_type == "MARKET_BUY":
                execution_id, outcome = execute_market_buy_quote(
                    execution_client=client,
                    state=state,
                    candidate=cand,
                    plan=plan,
                    rules_snapshot=rules_snapshot,
                    runtime_environment=runtime_env,
                )
            elif order_type == "LIMIT_BUY":
                execution_id, outcome = execute_limit_buy(
                    execution_client=client,
                    state=state,
                    candidate=cand,
                    plan=plan,
                    rules_snapshot=rules_snapshot,
                    runtime_environment=runtime_env,
                )
            elif order_type == "MARKET_SELL":
                execution_id, outcome = execute_market_sell_qty(
                    execution_client=client,
                    state=state,
                    candidate=cand,
                    plan=plan,
                    rules_snapshot=rules_snapshot,
                    runtime_environment=runtime_env,
                )
            else:
                execution_id, outcome = execute_limit_sell(
                    execution_client=client,
                    state=state,
                    candidate=cand,
                    plan=plan,
                    rules_snapshot=rules_snapshot,
                    runtime_environment=runtime_env,
                )
        except (ExecutionError, BinanceAPIError, ValueError) as e:
            state.append_audit(level="ERROR", event="execution_failed", details={"candidate_id": int(args.candidate_id), "error": str(e)})
            print(f"ERROR: {e}")
            return 2

        # Post-trade resync (best-effort): refresh cached balances + open orders.
        try:
            post_sync_bal = sync_balances(client=client, conn=conn)
        except Exception:
            post_sync_bal = None
        try:
            sym = str(cand.get("symbol") or "").strip().upper() or None
            post_sync_oo = sync_open_orders(client=client, conn=conn, symbol=sym)
        except Exception:
            post_sync_oo = None
        try:
            state.recompute_locked_qty_for_open_positions()
        except Exception:
            pass

    print("Execution Result")
    print(f"- Execution ID: {execution_id}")
    print(f"- Candidate ID: {cand.get('id')}")
    print(f"- Status: {outcome.local_status}")
    if outcome.raw_status:
        print(f"- Raw Status: {outcome.raw_status}")
    if outcome.binance_order_id:
        print(f"- Binance Order ID: {outcome.binance_order_id}")
    if outcome.fills:
        print(f"- Executed Qty: {outcome.fills.executed_qty}")
        if outcome.fills.avg_fill_price is not None:
            print(f"- Avg Fill Price: {outcome.fills.avg_fill_price}")
        if str(cand.get('side') or '').strip().lower() == "sell":
            print(f"- Total Quote Received: {outcome.fills.total_quote_spent}")
        else:
            print(f"- Total Quote Spent: {outcome.fills.total_quote_spent}")
        print(f"- Fills Count: {outcome.fills.fills_count}")
        if outcome.fills.commission_asset and outcome.fills.commission_total is not None:
            print(f"- Commission: {outcome.fills.commission_total} {outcome.fills.commission_asset}")
        elif outcome.fills.commission_asset:
            print(f"- Commission: {outcome.fills.commission_asset} (see audit for breakdown)")
    print(f"- Message: {outcome.message}")
    if post_sync_bal is not None:
        try:
            print(f"- Post-sync balances: {post_sync_bal.status}")
        except Exception:
            pass
    if post_sync_oo is not None:
        try:
            print(f"- Post-sync open orders: {post_sync_oo.status} seen={post_sync_oo.open_orders_seen}")
        except Exception:
            pass
    return 0 if outcome.local_status in ("filled", "submitted", "partially_filled") else 2


def cmd_trade_executions_list(args: argparse.Namespace) -> int:
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)
    with connect(db_path) as conn:
        rows = StateManager(conn).list_executions(limit=int(getattr(args, "limit", 20)))
    if not rows:
        print("(no executions)")
        return 0

    def _cell(v: object, width: int) -> str:
        s = "" if v is None else str(v)
        if len(s) > width:
            return s[: max(0, width - 1)] + "…"
        return s

    print(f"Executions: {len(rows)}")
    print(
        f"{'EXEC_ID':>7} {'CAND_ID':>7} {'PLAN_ID':>7} {'SYMBOL':<10} {'TYPE':<10} {'ENV':<7} {'STATUS':<18} "
        f"{'QUOTE_QTY':<10} {'LMT_PX':<10} {'EXEC_QTY':<12} {'AVG_PRICE':<14} {'ORDER_ID':<10}"
    )
    for r in rows:
        print(
            f"{int(r['execution_id']):>7} "
            f"{int(r.get('candidate_id') or 0):>7} "
            f"{int(r.get('plan_id') or 0):>7} "
            f"{_cell(r.get('symbol') or '-', 10):<10} "
            f"{_cell(r.get('order_type') or '-', 10):<10} "
            f"{_cell(r.get('execution_environment') or '-', 7):<7} "
            f"{_cell(r.get('local_status') or '-', 18):<18} "
            f"{_cell(r.get('quote_order_qty') or '-', 10):<10} "
            f"{_cell(r.get('limit_price') or '-', 10):<10} "
            f"{_cell(r.get('executed_quantity') or '-', 12):<12} "
            f"{_cell(r.get('avg_fill_price') or '-', 14):<14} "
            f"{_cell(r.get('binance_order_id') or '-', 10):<10} "
        )
    return 0


def cmd_trade_executions_show(args: argparse.Namespace) -> int:
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)
    with connect(db_path) as conn:
        row = StateManager(conn).get_execution(execution_id=int(args.execution_id))
    if not row:
        print("(not found)")
        return 2
    for k in [
        "execution_id",
        "candidate_id",
        "plan_id",
        "trade_request_id",
        "symbol",
        "side",
        "order_type",
        "execution_environment",
        "client_order_id",
        "binance_order_id",
        "quote_order_qty",
        "limit_price",
        "time_in_force",
        "requested_quantity",
        "executed_quantity",
        "avg_fill_price",
        "total_quote_spent",
        "commission_total",
        "commission_asset",
        "fee_breakdown_json",
        "realized_pnl_quote",
        "realized_pnl_quote_asset",
        "pnl_warnings_json",
        "fills_count",
        "local_status",
        "raw_status",
        "retry_count",
        "submitted_at_utc",
        "reconciled_at_utc",
        "expired_at_utc",
        "created_at_utc",
        "updated_at_utc",
    ]:
        print(f"{k}={row.get(k)}")
    return 0


def cmd_trade_execution_cancel(args: argparse.Namespace) -> int:
    client = _client_from_args(args)
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)

    from cryptogent.execution.result_parser import parse_fills

    with connect(db_path) as conn:
        state = StateManager(conn)
        row = state.get_execution(execution_id=int(args.execution_id))
        if not row:
            print("(not found)")
            return 2

        if str(row.get("order_type") or "").strip().upper() not in ("LIMIT_BUY", "LIMIT_SELL"):
            print("Not supported: only LIMIT_BUY / LIMIT_SELL executions can be cancelled.")
            return 2

        if str(row.get("local_status") or "") in ("filled", "cancelled", "expired", "failed", "rejected"):
            print(f"Not cancellable (status={row.get('local_status')})")
            return 2

        symbol = str(row.get("symbol") or "").strip().upper()
        client_order_id = str(row.get("client_order_id") or "").strip()
        if not (symbol and client_order_id):
            print("Missing symbol/client_order_id")
            return 2

        try:
            client.cancel_order_by_client_order_id(symbol=symbol, client_order_id=client_order_id)
        except BinanceAPIError as e:
            state.append_audit(
                level="ERROR",
                event="order_cancel_failed",
                details={"execution_id": int(args.execution_id), "symbol": symbol, "client_order_id": client_order_id, "error": str(e)},
            )
            print(f"ERROR: {e}")
            return 2

        # Reconcile immediately to reflect exchange truth.
        try:
            order = client.get_order_by_client_order_id(symbol=symbol, client_order_id=client_order_id)
            raw_status = str(order.get("status") or "") or None
            order_id = str(order.get("orderId") or "") or None
            fills = None
            try:
                fills = parse_fills(order)
            except Exception:
                fills = None

            local_status = "submitted"
            if raw_status in ("NEW",):
                local_status = "open"
            elif raw_status == "FILLED":
                local_status = "filled"
            elif raw_status == "PARTIALLY_FILLED":
                local_status = "partially_filled"
            elif raw_status in ("CANCELED", "CANCELLED"):
                local_status = "cancelled"
            elif raw_status in ("EXPIRED",):
                local_status = "expired"

            state.update_execution(
                execution_id=int(args.execution_id),
                local_status=local_status,
                raw_status=raw_status,
                binance_order_id=order_id,
                executed_quantity=str(fills.executed_qty) if fills else None,
                avg_fill_price=str(fills.avg_fill_price) if fills and fills.avg_fill_price is not None else None,
                total_quote_spent=str(fills.total_quote_spent) if fills else None,
                commission_total=str(fills.commission_total) if fills and fills.commission_total is not None else None,
                commission_asset=(fills.commission_asset if fills else None),
                fills_count=(fills.fills_count if fills else None),
                retry_count=int(row.get("retry_count") or 0),
                message="cancel_requested",
                details_json=None,
                submitted_at_utc=str(row.get("submitted_at_utc") or "") or None,
                reconciled_at_utc=utcnow_iso(),
            )
        except BinanceAPIError:
            # Best-effort: cancel request succeeded but reconciliation failed.
            pass

        # Refresh cached open orders and recompute locked quantities (best-effort).
        try:
            sync_open_orders(client=client, conn=conn, symbol=symbol)
        except Exception:
            pass
        try:
            state.recompute_locked_qty_for_open_positions()
        except Exception:
            pass

    print("Cancelled (requested)")
    return 0


@contextlib.contextmanager
def _cbreak_stdin():
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        yield
        return
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _sleep_with_ctrl_b(
    *,
    seconds: float,
    end_at: float | None,
    base_line: str | None = None,
    show_countdown: bool = True,
) -> bool:
    """
    Returns True if stop was requested (Ctrl-B), otherwise False.
    """
    stop_key = "\x02"  # Ctrl-B
    deadline = (time.monotonic() + seconds) if end_at is None else min(end_at, time.monotonic() + seconds)
    spinner = ["|", "/", "-", "\\"]
    spin_i = 0

    def _tty_write(text: str) -> None:
        # Clear the line to avoid leftover characters on terminals/loggers that don't honor plain '\r'.
        sys.stdout.write("\r\x1b[2K" + text)
        sys.stdout.flush()
    while True:
        if end_at is not None and time.monotonic() >= end_at:
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if not sys.stdin.isatty():
            time.sleep(min(0.5, remaining))
            continue
        if show_countdown:
            # Keep the UI "alive" during sleep without leaving remnants.
            try:
                s = int(max(0, remaining))
                ch = spinner[spin_i % len(spinner)]
                spin_i += 1
                suffix = f"{ch} t-{s:>2}s (Ctrl-B)"
                prefix = ""
                if base_line:
                    cols = 120
                    try:
                        cols = int(shutil.get_terminal_size((120, 20)).columns)
                    except Exception:
                        cols = 120
                    max_prefix = max(0, cols - len(suffix))
                    short = base_line
                    if len(short) > max_prefix and max_prefix > 3:
                        short = short[: max_prefix - 3] + "..."
                    prefix = short + "  "
                _tty_write(prefix + suffix)
            except Exception:
                pass
        r, _, _ = select.select([sys.stdin], [], [], min(1.0, remaining))
        if r:
            ch = sys.stdin.read(1)
            if ch == stop_key:
                return True


def _reconcile_open_orders_batch(
    *,
    client: BinanceSpotClient,
    state: StateManager,
    limit: int = 200,
    per_order_pause_s: float = 0.5,
    end_at: float | None = None,
    progress_line: callable | None = None,
    order_source: str | None = None,
) -> tuple[int, int]:
    """
    Reconcile *all currently open* orders (orders table NEW/PARTIALLY_FILLED)
    by calling GET /api/v3/order (orderId) one-by-one and upserting the latest fields.

    Returns: (open_total, errors)
    """
    if order_source:
        rows = state.list_open_orders_for_reconcile_by_source(order_source=order_source, limit=limit)
    else:
        rows = state.list_open_orders_for_reconcile(limit=limit)
    open_orders = [
        (str(r.get("symbol") or "").strip().upper(), str(r.get("exchange_order_id") or "").strip())
        for r in rows
        if str(r.get("symbol") or "").strip() and str(r.get("exchange_order_id") or "").strip()
    ]
    total = len(open_orders)
    errors = 0

    for i, (symbol, order_id) in enumerate(open_orders, start=1):
        if progress_line:
            try:
                progress_line(i, total)
            except Exception:
                pass
        try:
            order = client.get_order_by_order_id(symbol=symbol, order_id=order_id)

            def _iso_ms(v: object) -> str:
                try:
                    j = int(v)  # ms
                except Exception:
                    j = 0
                return ms_to_utc_iso(j) if j else utcnow_iso()

            row = OrderRow(
                exchange_order_id=str(order.get("orderId")) if order.get("orderId") is not None else order_id,
                symbol=str(order.get("symbol") or symbol),
                side=str(order.get("side") or ""),
                type=str(order.get("type") or ""),
                status=str(order.get("status") or ""),
                time_in_force=str(order.get("timeInForce")) if order.get("timeInForce") is not None else None,
                price=str(order.get("price")) if order.get("price") is not None else None,
                quantity=str(order.get("origQty") or "0"),
                filled_quantity=str(order.get("executedQty") or "0"),
                executed_quantity=str(order.get("executedQty") or "0"),
                created_at_utc=_iso_ms(order.get("time")),
                updated_at_utc=_iso_ms(order.get("updateTime") or order.get("time")),
            )
            state.upsert_orders([row])
        except Exception:
            errors += 1

        # Fixed delay between orders (rate-limit friendly). If Ctrl-B is available, allow immediate stop.
        if per_order_pause_s > 0:
            if sys.stdin.isatty() and sys.stdout.isatty():
                if _sleep_with_ctrl_b(seconds=float(per_order_pause_s), end_at=end_at, base_line=None, show_countdown=False):
                    break
            else:
                time.sleep(float(per_order_pause_s))

    return total, errors


def _trade_reconcile_once(args: argparse.Namespace, *, quiet: bool = False) -> tuple[int, dict]:
    client = _client_from_args(args)
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    cfg = load_config(config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)

    runtime_env = "testnet" if cfg.binance_testnet else "mainnet"
    timeout_min = int(getattr(args, "limit_order_timeout_minutes", 30))
    auto_cancel = getattr(args, "auto_cancel_expired", None)
    if auto_cancel is None:
        auto_cancel = False
    limit = int(getattr(args, "limit", 50))

    from cryptogent.execution.result_parser import parse_fills

    def _parse_iso(s: str) -> datetime:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)

    updated = 0
    expired = 0
    skipped = 0
    errors = 0
    status_counts: dict[str, int] = {}
    pre_status_counts: dict[str, int] = {}
    post_sync_bal: object | None = None
    post_sync_oo: object | None = None
    oo_exec_total: int | None = None
    oo_exec_errors: int = 0
    oo_exec_tracked: int = 0

    with connect(db_path) as conn:
        state = StateManager(conn)
        rows = state.list_reconcilable_executions(limit=limit)
        for r in rows:
            ls = str(r.get("local_status") or "").strip()
            if ls:
                pre_status_counts[ls] = pre_status_counts.get(ls, 0) + 1

        now = datetime.now(UTC)
        # Small fixed pause between per-execution reconciliations to reduce rate-limit risk.
        # Intentionally not user-configurable.
        per_item_pause_s = 0.5
        seen_exec_ids: set[int] = set()
        for i, r in enumerate(rows, start=1):
            exec_id = int(r["execution_id"])
            seen_exec_ids.add(exec_id)
            symbol = str(r.get("symbol") or "")
            client_order_id = str(r.get("client_order_id") or "")
            exec_env = str(r.get("execution_environment") or "").strip().lower()
            order_type = str(r.get("order_type") or "").strip().upper()

            if exec_env and exec_env != runtime_env:
                skipped += 1
                status_counts["skipped"] = status_counts.get("skipped", 0) + 1
                state.append_audit(
                    level="WARN",
                    event="reconcile_skipped_env_mismatch",
                    details={"execution_id": exec_id, "execution_environment": exec_env, "runtime_environment": runtime_env},
                )
                continue

            # Timeout enforcement for LIMIT orders (local expire; optional cancel).
            submitted_at = r.get("submitted_at_utc")
            if order_type in ("LIMIT_BUY", "LIMIT_SELL") and submitted_at:
                try:
                    age = now - _parse_iso(str(submitted_at))
                    if age.total_seconds() >= timeout_min * 60:
                        if auto_cancel:
                            try:
                                client.cancel_order_by_client_order_id(symbol=symbol, client_order_id=client_order_id)
                                state.append_audit(
                                    level="WARN",
                                    event="limit_order_cancel_requested",
                                    details={"execution_id": exec_id, "client_order_id": client_order_id, "reason": "timeout"},
                                )
                            except BinanceAPIError as e:
                                state.append_audit(
                                    level="ERROR",
                                    event="limit_order_cancel_failed",
                                    details={"execution_id": exec_id, "error": str(e), "reason": "timeout"},
                                )
                            # Always mark local expired_at_utc; reconciliation below may update status to cancelled/filled.
                            state.update_execution(
                                execution_id=exec_id,
                                local_status=str(r.get("local_status") or "open"),
                                raw_status=str(r.get("raw_status") or "") or None,
                                binance_order_id=None,
                                executed_quantity=None,
                                avg_fill_price=None,
                                total_quote_spent=None,
                                commission_total=None,
                                commission_asset=None,
                                fills_count=None,
                                retry_count=int(r.get("retry_count") or 0),
                                message=f"limit_order_timeout_reached:{timeout_min}m; cancel_attempted",
                                details_json=None,
                                submitted_at_utc=str(r.get("submitted_at_utc") or "") or None,
                                reconciled_at_utc=utcnow_iso(),
                                expired_at_utc=utcnow_iso(),
                            )
                        else:
                            state.mark_execution_expired(execution_id=exec_id, reason=f"limit_order_timeout_reached:{timeout_min}m")
                            expired += 1
                            status_counts["expired"] = status_counts.get("expired", 0) + 1
                            continue
                except Exception:
                    # If we can't parse time, fail closed by not expiring; reconciliation still runs.
                    pass

            try:
                order = client.get_order_by_client_order_id(symbol=symbol, client_order_id=client_order_id)
            except BinanceAPIError as e:
                errors += 1
                status_counts["error"] = status_counts.get("error", 0) + 1
                state.append_audit(level="ERROR", event="reconcile_failed", details={"execution_id": exec_id, "error": str(e)})
                continue

            raw_status = str(order.get("status") or "") or None
            order_id = str(order.get("orderId") or "") or None
            fills = None
            try:
                fills = parse_fills(order)
            except Exception:
                fills = None

            local_status = "submitted"
            if raw_status in ("NEW",):
                local_status = "open"
            elif raw_status == "FILLED":
                local_status = "filled"
            elif raw_status == "PARTIALLY_FILLED":
                local_status = "partially_filled"
            elif raw_status in ("CANCELED", "CANCELLED"):
                local_status = "cancelled"
            elif raw_status in ("EXPIRED",):
                local_status = "expired"

            state.update_execution(
                execution_id=exec_id,
                local_status=local_status,
                raw_status=raw_status,
                binance_order_id=order_id,
                executed_quantity=str(fills.executed_qty) if fills else None,
                avg_fill_price=str(fills.avg_fill_price) if fills and fills.avg_fill_price is not None else None,
                total_quote_spent=str(fills.total_quote_spent) if fills else None,
                commission_total=str(fills.commission_total) if fills and fills.commission_total is not None else None,
                commission_asset=(fills.commission_asset if fills else None),
                fills_count=(fills.fills_count if fills else None),
                retry_count=int(r.get("retry_count") or 0),
                message="reconciled",
                details_json=None,
                submitted_at_utc=str(r.get("submitted_at_utc") or "") or None,
                reconciled_at_utc=utcnow_iso(),
            )
            updated += 1
            status_counts[local_status] = status_counts.get(local_status, 0) + 1
            if i < len(rows):
                time.sleep(per_item_pause_s)

        # Post-reconcile resync (best-effort): refresh cached balances + open orders.
        try:
            post_sync_bal = sync_balances(client=client, conn=conn)
        except Exception:
            post_sync_bal = None
        try:
            post_sync_oo = sync_open_orders(client=client, conn=conn, symbol=None)
        except Exception:
            post_sync_oo = None

        # Reconcile *execution-sourced* open orders (from orders cache).
        # This is important when an execution row was locally marked expired but the exchange order is still NEW.
        try:
            open_exec_orders = state.list_open_orders_for_reconcile_by_source(order_source="execution", limit=200)
            oo_exec_total = len(open_exec_orders)
            for j, o in enumerate(open_exec_orders, start=1):
                oo_exec_tracked = j
                order_id = str(o.get("exchange_order_id") or "").strip()
                sym = str(o.get("symbol") or "").strip().upper()
                if not (order_id and sym):
                    continue
                ex = state.get_execution_by_binance_order_id(binance_order_id=order_id)
                if not ex:
                    continue
                exec_id = int(ex.get("execution_id") or 0)
                if exec_id in seen_exec_ids:
                    continue
                seen_exec_ids.add(exec_id)

                exec_env = str(ex.get("execution_environment") or "").strip().lower()
                if exec_env and exec_env != runtime_env:
                    skipped += 1
                    status_counts["skipped"] = status_counts.get("skipped", 0) + 1
                    continue

                client_order_id = str(ex.get("client_order_id") or "").strip()
                order_type = str(ex.get("order_type") or "").strip().upper()
                submitted_at = ex.get("submitted_at_utc")

                # If it's a LIMIT order and it exceeded timeout, cancel on Binance by default.
                if order_type in ("LIMIT_BUY", "LIMIT_SELL") and submitted_at:
                    try:
                        age = now - _parse_iso(str(submitted_at))
                        if age.total_seconds() >= timeout_min * 60 and auto_cancel:
                            try:
                                client.cancel_order_by_client_order_id(symbol=sym, client_order_id=client_order_id)
                            except Exception:
                                oo_exec_errors += 1
                    except Exception:
                        pass

                try:
                    order = client.get_order_by_client_order_id(symbol=sym, client_order_id=client_order_id)
                except Exception:
                    oo_exec_errors += 1
                    continue

                raw_status = str(order.get("status") or "") or None
                order_id2 = str(order.get("orderId") or "") or None
                fills = None
                try:
                    fills = parse_fills(order)
                except Exception:
                    fills = None

                local_status = "submitted"
                if raw_status in ("NEW",):
                    local_status = "open"
                elif raw_status == "FILLED":
                    local_status = "filled"
                elif raw_status == "PARTIALLY_FILLED":
                    local_status = "partially_filled"
                elif raw_status in ("CANCELED", "CANCELLED"):
                    local_status = "cancelled"
                elif raw_status in ("EXPIRED",):
                    local_status = "expired"

                state.update_execution(
                    execution_id=exec_id,
                    local_status=local_status,
                    raw_status=raw_status,
                    binance_order_id=order_id2,
                    executed_quantity=str(fills.executed_qty) if fills else None,
                    avg_fill_price=str(fills.avg_fill_price) if fills and fills.avg_fill_price is not None else None,
                    total_quote_spent=str(fills.total_quote_spent) if fills else None,
                    commission_total=str(fills.commission_total) if fills and fills.commission_total is not None else None,
                    commission_asset=(fills.commission_asset if fills else None),
                    fills_count=(fills.fills_count if fills else None),
                    retry_count=int(ex.get("retry_count") or 0),
                    message="reconciled_open_execution_order",
                    details_json=None,
                    submitted_at_utc=str(ex.get("submitted_at_utc") or "") or None,
                    reconciled_at_utc=utcnow_iso(),
                )
                updated += 1
                status_counts[local_status] = status_counts.get(local_status, 0) + 1
                if j < len(open_exec_orders):
                    time.sleep(per_item_pause_s)
        except Exception:
            oo_exec_total = None

    locked_updated = 0
    try:
        locked_updated = state.recompute_locked_qty_for_open_positions()
    except Exception:
        locked_updated = 0

    open_orders_seen = None
    if post_sync_oo is not None:
        try:
            open_orders_seen = int(post_sync_oo.open_orders_seen)
        except Exception:
            open_orders_seen = None

    stats = {
        "updated": updated,
        "expired": expired,
        "skipped": skipped,
        "errors": errors,
        "locked_updated": locked_updated,
        "status_counts": status_counts,
        "pre_status_counts": pre_status_counts,
        "open_orders_seen": open_orders_seen,
        "open_exec_total": oo_exec_total,
        "open_exec_errors": oo_exec_errors,
        "open_exec_tracked": oo_exec_tracked,
    }

    if not quiet:
        msg = f"OK reconciled updated={updated} expired={expired} skipped={skipped} errors={errors}"
        if post_sync_bal is not None:
            try:
                msg += f" balances={post_sync_bal.status}"
            except Exception:
                pass
        if post_sync_oo is not None:
            try:
                msg += f" open_orders={post_sync_oo.status}({post_sync_oo.open_orders_seen})"
            except Exception:
                pass
        print(msg)

    return (0 if errors == 0 else 2), stats


def cmd_trade_reconcile(args: argparse.Namespace) -> int:
    if getattr(args, "auto_cancel_expired", None) is None:
        if sys.stdin.isatty() and sys.stdout.isatty():
            ans = _prompt_yes_no("Auto-cancel expired LIMIT orders on Binance?", default=False)
            setattr(args, "auto_cancel_expired", True if ans else False)
        else:
            setattr(args, "auto_cancel_expired", False)
    if not getattr(args, "loop", False):
        rc, _ = _trade_reconcile_once(args, quiet=False)
        return rc
    interval = int(getattr(args, "interval_seconds", 60))
    duration = getattr(args, "duration_seconds", None)
    end_at = None
    if duration not in (None, ""):
        end_at = time.monotonic() + int(duration)
    print("Reconcile loop started. Press Ctrl-B or Ctrl-C to stop.")
    with _cbreak_stdin():
        try:
            while True:
                rc, stats = _trade_reconcile_once(args, quiet=True)
                exec_open_total = stats.get("open_exec_total")
                if exec_open_total is None:
                    # Fallback to exchange open order count when we can't compute execution-specific open count.
                    exec_open_total = stats.get("open_orders_seen")
                exec_open_total_i = int(exec_open_total or 0)
                exec_open_err_i = int(stats.get("open_exec_errors", 0) or 0)
                exec_open_tracked_i = int(stats.get("open_exec_tracked", 0) or 0)
                pre = stats.get("pre_status_counts") or {}
                open_n = int(pre.get("open", 0))
                partial_n = int(pre.get("partially_filled", 0))
                uncertain_n = int(pre.get("uncertain_submitted", 0))
                submitted_n = int(pre.get("submitted", 0))
                sc = stats.get("status_counts") or {}
                filled_n = int(sc.get("filled", 0))
                cancelled_n = int(sc.get("cancelled", 0))
                expired_n = int(sc.get("expired", 0)) + int(stats.get("expired", 0))
                oo_seen = stats.get("open_orders_seen")
                oo_s = f" oo={oo_seen}" if oo_seen is not None else ""
                line = (
                    f"reconcile: exec_open={exec_open_total_i} tracked_open={exec_open_tracked_i}/{exec_open_total_i} "
                    f"open={open_n} submitted={submitted_n} partial={partial_n} "
                    f"uncertain={uncertain_n} filled={filled_n} cancelled={cancelled_n} expired={expired_n} "
                    f"updated={stats.get('updated', 0)} errors={int(stats.get('errors', 0) or 0) + exec_open_err_i}{oo_s}"
                )
                if sys.stdout.isatty():
                    sys.stdout.write("\r\x1b[2K" + line)
                    sys.stdout.flush()
                else:
                    print(line)
                active_n = open_n + submitted_n + partial_n + uncertain_n
                if active_n == 0 and exec_open_total_i == 0:
                    if sys.stdout.isatty():
                        print()
                    print("Stopped (no active executions)")
                    return 0
                if end_at is not None and time.monotonic() >= end_at:
                    if sys.stdout.isatty():
                        print()
                    print("Done")
                    return rc
                if _sleep_with_ctrl_b(seconds=float(interval), end_at=end_at, base_line=line, show_countdown=True):
                    if sys.stdout.isatty():
                        print()
                    print("Stopped")
                    return rc
        except KeyboardInterrupt:
            if sys.stdout.isatty():
                print()
            print("Stopped")
            return 0


def _d_position(value: object, name: str) -> Decimal:
    try:
        d = Decimal(str(value))
    except Exception as e:
        raise ValueError(f"Invalid decimal for {name}") from e
    if d.is_nan() or d.is_infinite():
        raise ValueError(f"Invalid decimal for {name}")
    return d


def _price_client_for_market_env(*, cfg, market_env: str, ca_bundle: Path | None, insecure: bool) -> BinanceSpotClient:
    env = (market_env or "").strip().lower()
    if env not in ("mainnet_public", "testnet"):
        env = "mainnet_public"
    base_url = "https://api.binance.com" if env == "mainnet_public" else "https://testnet.binance.vision"
    client = BinanceSpotClient.from_config(cfg)
    client = BinanceSpotClient(**{**client.__dict__, "base_url": base_url, "api_key": None, "api_secret": None})
    if ca_bundle:
        client = BinanceSpotClient(**{**client.__dict__, "ca_bundle_path": ca_bundle.expanduser(), "tls_verify": True})
    if insecure:
        client = BinanceSpotClient(**{**client.__dict__, "tls_verify": False})
    return client


def cmd_position_list(args: argparse.Namespace) -> int:
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)
    status = getattr(args, "status", None)
    limit = int(getattr(args, "limit", 50))
    with connect(db_path) as conn:
        rows = StateManager(conn).list_positions(status=status, limit=limit)
    if not rows:
        print("(no positions)")
        return 0
    print(f"Positions: {len(rows)}")
    print(f"{'POS_ID':>6} {'STATUS':<8} {'SYMBOL':<10} {'QTY':<14} {'LOCKED':<14} {'ENTRY':<14} {'MD_ENV':<13} {'EXEC_ENV':<7}")
    for r in rows:
        print(
            f"{int(r.get('id') or 0):>6} "
            f"{str(r.get('status') or '-'): <8} "
            f"{str(r.get('symbol') or '-'): <10} "
            f"{str(r.get('quantity') or '-'): <14} "
            f"{str(r.get('locked_qty') or '0'): <14} "
            f"{str(r.get('entry_price') or '-'): <14} "
            f"{str(r.get('market_data_environment') or '-'): <13} "
            f"{str(r.get('execution_environment') or '-'): <7}"
        )
    return 0


def cmd_position_show(args: argparse.Namespace) -> int:
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    cfg = load_config(config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)

    md_env = "mainnet_public"
    current_price = Decimal("0")
    entry_price = Decimal("0")
    qty = Decimal("0")
    market_value = Decimal("0")
    cost_basis = Decimal("0")
    unrealized = Decimal("0")
    pnl_pct = Decimal("0")

    with connect(db_path) as conn:
        state = StateManager(conn)
        pos = state.get_position(position_id=int(args.position_id))
        if not pos:
            print("(not found)")
            return 2

        for k in [
            "id",
            "symbol",
            "status",
            "market_data_environment",
            "execution_environment",
            "entry_price",
            "quantity",
            "stop_loss_price",
            "profit_target_price",
            "deadline_utc",
            "opened_at_utc",
            "closed_at_utc",
            "last_monitored_at_utc",
            "created_at_utc",
            "updated_at_utc",
        ]:
            print(f"{k}={pos.get(k)}")

        if str(pos.get("status") or "").upper() != "OPEN":
            return 0
        if not getattr(args, "live", False):
            return 0

        symbol = str(pos.get("symbol") or "").strip().upper()
        md_env = str(pos.get("market_data_environment") or "mainnet_public")
        client = _price_client_for_market_env(
            cfg=cfg,
            market_env=md_env,
            ca_bundle=getattr(args, "ca_bundle", None),
            insecure=bool(getattr(args, "insecure", False)),
        )

        current_price = _d_position(client.get_ticker_price(symbol=symbol), "current_price")
        entry_price = _d_position(pos.get("entry_price"), "entry_price")
        qty = _d_position(pos.get("quantity"), "net_position_qty")

        market_value = current_price * qty
        cost_basis = entry_price * qty
        unrealized = market_value - cost_basis
        pnl_pct = (unrealized / cost_basis * Decimal("100")) if cost_basis > 0 else Decimal("0")

        state.update_position_last_monitored(position_id=int(pos["id"]), at_utc=utcnow_iso())

    print("Unrealized PnL")
    print(f"- Price Env: {md_env}")
    print(f"- Current Price: {current_price}")
    print(f"- Net Qty: {qty}")
    print(f"- Market Value: {market_value}")
    print(f"- Cost Basis: {cost_basis}")
    print(f"- Unrealized PnL: {unrealized}")
    print(f"- PnL %: {pnl_pct}")
    return 0


def cmd_market_status(args: argparse.Namespace) -> int:
    symbol = str(getattr(args, "symbol", "") or "").strip().upper()
    timeframe = str(getattr(args, "timeframe", "") or "").strip()
    if not symbol or not timeframe:
        print("Missing required --symbol and/or --timeframe")
        return 2

    supported = {
        "1m",
        "3m",
        "5m",
        "15m",
        "30m",
        "1h",
        "2h",
        "4h",
        "6h",
        "8h",
        "12h",
        "1d",
        "1w",
        "1M",
    }
    if timeframe not in supported:
        print(f"Unsupported timeframe: {timeframe}")
        return 2

    limit = int(getattr(args, "limit", 100))
    if limit <= 0:
        print("Invalid --limit (must be > 0)")
        return 2

    cache_raw = getattr(args, "cache", None)
    cache_ttl_s = 0
    if cache_raw not in (None, ""):
        try:
            s = str(cache_raw).strip().lower()
            if s.endswith("s"):
                cache_ttl_s = int(s[:-1])
            elif s.endswith("m"):
                cache_ttl_s = int(s[:-1]) * 60
            elif s.endswith("h"):
                cache_ttl_s = int(s[:-1]) * 3600
            else:
                cache_ttl_s = int(s)
        except Exception:
            print("Invalid --cache (use seconds like 5s, 60, 1m, 1h)")
            return 2
        if cache_ttl_s < 0:
            print("Invalid --cache (must be >= 0)")
            return 2

    market_env = str(getattr(args, "market_env", "mainnet_public") or "mainnet_public").strip().lower()

    profile = str(getattr(args, "profile", "") or "").strip().lower()
    if profile:
        def _enable(flag: str) -> None:
            if not getattr(args, flag, False):
                setattr(args, flag, True)

        if profile == "quick":
            for f in ("momentum", "trend", "volatility"):
                _enable(f)
        elif profile == "trend":
            for f in ("momentum", "trend", "structure", "price_action"):
                _enable(f)
        elif profile == "full":
            for f in ("momentum", "trend", "volatility", "volume", "structure", "price_action", "execution", "quant", "crypto", "risk"):
                _enable(f)
        else:
            print("Invalid --profile (use quick|trend|full)")
            return 2
    if market_env not in ("mainnet_public", "testnet"):
        print("Invalid --market-env (mainnet_public|testnet)")
        return 2

    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    cfg = load_config(config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)
    ca_bundle = getattr(args, "ca_bundle", None)
    insecure = bool(getattr(args, "insecure", False))
    client = _price_client_for_market_env(cfg=cfg, market_env=market_env, ca_bundle=ca_bundle, insecure=insecure)

    # Volume config with CLI override priority
    vol_window_fast = getattr(args, "volume_window_fast", None)
    vol_window_slow = getattr(args, "volume_window_slow", None)
    vol_spike_ratio = getattr(args, "volume_spike_ratio", None)
    vol_zscore = getattr(args, "volume_zscore", None)
    vol_buy_ratio = getattr(args, "volume_buy_ratio", None)
    vol_sell_ratio = getattr(args, "volume_sell_ratio", None)
    vol_depth = getattr(args, "volume_depth", None)
    vol_wall_ratio = getattr(args, "volume_wall_ratio", None)
    vol_imbalance = getattr(args, "volume_imbalance", None)
    quant_window = getattr(args, "quant_window", None)
    quant_benchmark = getattr(args, "benchmark", None)
    corr_method = getattr(args, "corr_method", None)
    exec_depth = getattr(args, "execution_depth", None)
    exec_notional = getattr(args, "execution_notional", None)
    exec_side = getattr(args, "execution_side", None)
    risk_side = getattr(args, "risk_side", None)
    risk_entry = getattr(args, "risk_entry", None)
    risk_pct = getattr(args, "risk_pct", None)
    risk_account_balance = getattr(args, "risk_account_balance", None)
    risk_max_position_pct = getattr(args, "risk_max_position_pct", None)

    vol_window_fast = int(vol_window_fast) if vol_window_fast not in (None, "") else cfg.market_volume_window_fast
    vol_window_slow = int(vol_window_slow) if vol_window_slow not in (None, "") else cfg.market_volume_window_slow
    vol_spike_ratio = float(vol_spike_ratio) if vol_spike_ratio not in (None, "") else cfg.market_volume_spike_ratio
    vol_zscore = float(vol_zscore) if vol_zscore not in (None, "") else cfg.market_volume_zscore_threshold
    vol_buy_ratio = float(vol_buy_ratio) if vol_buy_ratio not in (None, "") else cfg.market_volume_buy_ratio
    vol_sell_ratio = float(vol_sell_ratio) if vol_sell_ratio not in (None, "") else cfg.market_volume_sell_ratio
    vol_depth = int(vol_depth) if vol_depth not in (None, "") else cfg.market_volume_depth_limit
    vol_wall_ratio = float(vol_wall_ratio) if vol_wall_ratio not in (None, "") else cfg.market_volume_wall_ratio
    vol_imbalance = float(vol_imbalance) if vol_imbalance not in (None, "") else cfg.market_volume_imbalance_threshold
    quant_window = int(quant_window) if quant_window not in (None, "") else 200
    quant_benchmark = str(quant_benchmark) if quant_benchmark not in (None, "") else "BTCUSDT"
    corr_method = str(corr_method) if corr_method not in (None, "") else "pearson"
    exec_depth = int(exec_depth) if exec_depth not in (None, "") else 10
    exec_notional = Decimal(str(exec_notional)) if exec_notional not in (None, "") else Decimal("1000")
    exec_side = str(exec_side) if exec_side not in (None, "") else "buy"
    risk_side = str(risk_side) if risk_side not in (None, "") else "long"
    risk_entry = Decimal(str(risk_entry)) if risk_entry not in (None, "") else None
    risk_pct = Decimal(str(risk_pct)) if risk_pct not in (None, "") else Decimal("1")
    risk_account_balance = Decimal(str(risk_account_balance)) if risk_account_balance not in (None, "") else None
    risk_max_position_pct = Decimal(str(risk_max_position_pct)) if risk_max_position_pct not in (None, "") else Decimal("20")
    if corr_method not in ("pearson",):
        print("Invalid --corr-method (pearson)")
        return 2
    if exec_depth <= 0:
        print("Invalid --execution-depth (must be > 0)")
        return 2
    if exec_notional <= 0:
        print("Invalid --execution-notional (must be > 0)")
        return 2
    if exec_side.lower() not in ("buy", "sell"):
        print("Invalid --execution-side (buy|sell)")
        return 2
    if risk_side.lower() not in ("long", "short"):
        print("Invalid --risk-side (long|short)")
        return 2

    risk_on = bool(getattr(args, "risk", False))
    need_momentum = bool(getattr(args, "momentum", False) or risk_on)
    need_trend = bool(getattr(args, "trend", False) or risk_on)
    need_volatility = bool(getattr(args, "volatility", False) or risk_on)
    need_volume = bool(getattr(args, "volume", False) or risk_on)
    need_structure = bool(getattr(args, "structure", False) or risk_on)
    need_price_action = bool(getattr(args, "price_action", False))
    need_execution = bool(getattr(args, "execution", False) or risk_on)

    cache_hit = False
    snap = None
    indicators: dict | None = None
    payload: dict | None = None
    cond = None
    high_24h = low_24h = change_pct = volume_24h = None
    if cache_ttl_s > 0:
        try:
            with connect(db_path) as conn:
                state = StateManager(conn)
                cached = state.get_latest_market_snapshot(symbol=symbol, timeframe=timeframe)
            if cached:
                try:
                    captured_at = datetime.fromisoformat(str(cached.get("captured_at_utc") or "").replace("Z", "+00:00")).astimezone(UTC)
                    age_s = (datetime.now(UTC) - captured_at).total_seconds()
                except Exception:
                    age_s = cache_ttl_s + 1
                if age_s <= cache_ttl_s:
                    cache_hit = True
                    cond = cached.get("condition_summary") or "unknown"
                    indicators = None
                    try:
                        if cached.get("indicators_json"):
                            indicators = _json.loads(str(cached.get("indicators_json")))
                    except Exception:
                        indicators = None
                    high_24h = None
                    low_24h = None
                    change_pct = cached.get("change_percent")
                    volume_24h = cached.get("volume_quote")
                    payload = {
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "last_price": str(cached.get("last_price")),
                        "bid": cached.get("bid"),
                        "ask": cached.get("ask"),
                        "spread_pct": cached.get("spread_pct"),
                        "high_24h": high_24h,
                        "low_24h": low_24h,
                        "change_pct_24h": change_pct,
                        "volume_quote_24h": volume_24h,
                        "condition": cond,
                        "momentum_pct": str(indicators.get("momentum_pct")) if indicators and indicators.get("momentum_pct") is not None else None,
                        "volatility_pct": str(indicators.get("volatility_pct")) if indicators and indicators.get("volatility_pct") is not None else None,
                        "candle_count": str(indicators.get("candle_count")) if indicators and indicators.get("candle_count") is not None else None,
                    }
                    if indicators:
                        for key in (
                            "candle_count",
                            "rsi",
                            "rsi_prev",
                            "rsi_zone",
                            "macd",
                            "macd_signal",
                            "macd_hist",
                            "stoch_rsi",
                            "stoch_rsi_k",
                            "stoch_rsi_d",
                            "stoch_rsi_bias",
                            "williams_r",
                            "williams_r_zone",
                            "cci",
                            "cci_zone",
                            "roc",
                            "roc_bias",
                            "macd_bias",
                            "composite_signal",
                            "rsi_bullish_divergence",
                            "rsi_bearish_divergence",
                            "ema_20",
                            "ema_50",
                            "ema_200",
                            "sma_20",
                            "sma_50",
                            "sma_200",
                            "trend_crossover",
                            "trend_crossover_event",
                            "trend_crossover_strength_pct",
                            "ema_50_200_crossover",
                            "ema_50_200_event",
                            "ema_50_200_strength_pct",
                            "sma_20_50_crossover",
                            "sma_20_50_event",
                            "sma_50_200_crossover",
                            "sma_50_200_event",
                            "adx",
                            "adx_pos",
                            "adx_neg",
                            "adx_trend_strength",
                            "ichi_tenkan",
                            "ichi_kijun",
                            "ichi_senkou_a",
                            "ichi_senkou_b",
                            "ichi_cloud_bias",
                            "price_vs_ema20_pct",
                            "price_vs_ema50_pct",
                            "price_vs_ema200_pct",
                            "trend_bias",
                            "atr",
                            "atr_pct",
                            "bb_upper",
                            "bb_mid",
                            "bb_lower",
                            "bb_width_pct",
                            "bb_pct_b",
                            "bb_position",
                            "kc_upper",
                            "kc_lower",
                            "squeeze",
                            "hist_vol_pct",
                            "chandelier_long",
                            "chandelier_short",
                            "vol_regime",
                            "volume_base_last",
                            "volume_quote_last",
                            "volume_quote_avg_20",
                            "volume_quote_avg_50",
                            "volume_quote_std_20",
                            "volume_quote_zscore_20",
                            "volume_quote_trend",
                            "volume_spike",
                            "taker_buy_ratio",
                            "taker_buy_ratio_avg20",
                            "buy_pressure",
                            "sustained_buy_pressure",
                            "sustained_sell_pressure",
                            "obv",
                            "obv_trend",
                            "vwap_20",
                            "price_vs_vwap_pct",
                            "vol_price_confirmation",
                            "bid_qty",
                            "ask_qty",
                            "book_imbalance",
                            "liquidity_zones",
                            "buy_wall_price",
                            "buy_wall_qty",
                            "sell_wall_price",
                            "sell_wall_qty",
                            "structure_trend",
                            "range_state",
                            "range_high",
                            "range_low",
                            "range_width_pct",
                            "bos",
                            "bos_direction",
                            "bos_streak",
                            "choch",
                            "choch_direction",
                            "accumulation",
                            "last_swing_high",
                            "last_swing_low",
                            "prev_swing_high",
                            "prev_swing_low",
                            "price_zone",
                            "fvg_count",
                            "last_fvg_direction",
                            "last_fvg_high",
                            "last_fvg_low",
                            "last_fvg_mitigated",
                            "price_action_available",
                            "pa_unavailable_reason",
                            "pa_support_level",
                            "pa_support_strength",
                            "pa_support_distance_pct",
                            "pa_resistance_level",
                            "pa_resistance_strength",
                            "pa_resistance_distance_pct",
                            "pa_structure_type",
                            "pa_last_swing_high",
                            "pa_last_swing_low",
                            "pa_breakout",
                            "pa_breakdown",
                            "pa_breakout_level",
                            "pa_breakdown_level",
                            "pa_breakout_strength",
                            "pa_patterns_json",
                            "pa_last_pattern",
                            "pa_dominant_bias",
                            "pa_signal_count",
                            "pa_confluence",
                            "execution_available",
                            "execution_unavailable_reason",
                            "exec_mid",
                            "exec_best_bid",
                            "exec_best_ask",
                            "exec_spread_abs",
                            "exec_spread_pct",
                            "exec_spread_quality",
                            "exec_slippage_pct",
                            "exec_effective_spread_pct",
                            "exec_market_impact_pct",
                            "exec_avg_fill_price",
                            "exec_notional_used",
                            "exec_fill_ratio_pct",
                            "exec_levels_used",
                            "exec_side",
                            "exec_depth_levels",
                            "exec_bid_depth_notional",
                            "exec_ask_depth_notional",
                            "exec_notional_available",
                            "exec_depth_imbalance",
                            "exec_depth_spread_pct",
                            "risk_available",
                            "risk_viable",
                            "risk_rejection_reason",
                            "risk_score",
                            "risk_score_breakdown",
                            "risk_entry_price",
                            "risk_side",
                            "risk_effective_entry",
                            "risk_stop_price",
                            "risk_stop_method",
                            "risk_stop_distance_pct",
                            "risk_stop_atr_multiple",
                            "risk_stop_candidates",
                            "risk_tp1",
                            "risk_tp2",
                            "risk_tp3",
                            "risk_tp_fvg",
                            "risk_tp_structure",
                            "risk_tp_cloud",
                            "risk_reward_risk_ratio",
                            "risk_position_size_base",
                            "risk_position_size_quote",
                            "risk_position_size_pct",
                            "risk_max_loss_quote",
                            "risk_pct_used",
                            "risk_caps_applied",
                            "risk_suggested_leverage",
                            "risk_liquidation_price",
                            "risk_liquidation_distance_pct",
                            "risk_flag_wide_stop",
                            "risk_flag_concentration_cap",
                            "risk_flag_liquidity_cap",
                            "risk_flag_liquidation_warning",
                            "risk_flag_low_adx_warning",
                            "quant_available",
                            "quant_unavailable_reason",
                            "quant_window",
                            "quant_benchmark",
                            "quant_corr_method",
                            "quant_corr",
                            "quant_beta",
                            "quant_return_zscore",
                            "quant_realized_vol",
                            "quant_vol_regime",
                            "quant_mean_dev_pct",
                            "quant_mean_reversion_state",
                            "quant_max_drawdown_pct",
                            "quant_sharpe_ratio",
                            "quant_calmar_ratio",
                            "quant_skewness",
                            "quant_kurtosis",
                            "quant_log_return",
                            "quant_ret_mean",
                            "quant_ret_std",
                            "quant_price_vs_ema_pct",
                            "quant_rsi",
                            "quant_macd_hist",
                            "quant_atr_norm",
                            "quant_volume_zscore",
                            "quant_spread_pct",
                            "quant_range_pct",
                            "futures_market",
                            "funding_rate",
                            "next_funding_time",
                            "open_interest",
                        ):
                            if key in indicators and indicators.get(key) is not None:
                                payload[key] = str(indicators.get(key))
        except Exception:
            cache_hit = False
    if not cache_hit:
        try:
            fetch_fn = fetch_market_snapshot_cached if cache_ttl_s > 0 else fetch_market_snapshot
            if fetch_fn is fetch_market_snapshot_cached:
                snap, _ = fetch_fn(
                    client=client,
                    symbol=symbol,
                    candle_interval=timeframe,
                    candle_count=limit,
                    fetch_book_ticker=True,
                    cache_ttl_s=cache_ttl_s,
                    return_meta=True,
                )
            else:
                snap = fetch_fn(
                    client=client,
                    symbol=symbol,
                    candle_interval=timeframe,
                    candle_count=limit,
                    fetch_book_ticker=True,
                )
        except (BinanceAPIError, MarketDataError, ValueError) as e:
            print(f"ERROR: {e}")
            return 2

        stats = snap.stats_24h if isinstance(snap.stats_24h, dict) else {}
        high_24h = stats.get("highPrice")
        low_24h = stats.get("lowPrice")
        change_pct = stats.get("priceChangePercent")
        volume_24h = stats.get("quoteVolume")

        momentum = snap.candles.momentum_pct
        if momentum > 0:
            cond = "bullish"
        elif momentum < 0:
            cond = "bearish"
        else:
            cond = "neutral"

    if payload is None:
        payload = {
            "symbol": symbol,
            "timeframe": timeframe,
            "last_price": str(snap.price),
            "bid": str(snap.bid) if snap.bid is not None else None,
            "ask": str(snap.ask) if snap.ask is not None else None,
            "spread_pct": str(snap.spread_pct) if snap.spread_pct is not None else None,
            "high_24h": high_24h,
            "low_24h": low_24h,
            "change_pct_24h": change_pct,
            "volume_quote_24h": volume_24h,
            "condition": cond,
            "momentum_pct": str(momentum),
            "volatility_pct": str(snap.candles.volatility_pct),
            "candle_count": str(len(snap.klines)) if snap.klines is not None else None,
        }

    if not cache_hit and need_momentum:
        m = compute_momentum_metrics(snap.candles.closes)
        payload["rsi"] = str(m.rsi) if m.rsi is not None else None
        payload["rsi_prev"] = str(m.rsi_prev) if m.rsi_prev is not None else None
        payload["rsi_zone"] = m.rsi_zone
        payload["macd"] = str(m.macd) if m.macd is not None else None
        payload["macd_signal"] = str(m.macd_signal) if m.macd_signal is not None else None
        payload["macd_hist"] = str(m.macd_hist) if m.macd_hist is not None else None
        payload["macd_bias"] = m.macd_bias
        stoch_k = getattr(m, "stoch_rsi_k", None)
        stoch_d = getattr(m, "stoch_rsi_d", None)
        stoch_v = stoch_k if stoch_k is not None else getattr(m, "stoch_rsi", None)
        payload["stoch_rsi"] = str(stoch_v) if stoch_v is not None else None
        payload["stoch_rsi_k"] = str(stoch_k) if stoch_k is not None else None
        payload["stoch_rsi_d"] = str(stoch_d) if stoch_d is not None else None
        payload["stoch_rsi_bias"] = m.stoch_rsi_bias
        payload["williams_r"] = str(m.williams_r) if m.williams_r is not None else None
        payload["williams_r_zone"] = m.williams_r_zone
        payload["cci"] = str(m.cci) if m.cci is not None else None
        payload["cci_zone"] = m.cci_zone
        payload["roc"] = str(m.roc) if m.roc is not None else None
        payload["roc_bias"] = m.roc_bias
        payload["composite_signal"] = m.composite_signal
        payload["rsi_bullish_divergence"] = (
            str(m.rsi_bullish_divergence) if m.rsi_bullish_divergence is not None else None
        )
        payload["rsi_bearish_divergence"] = (
            str(m.rsi_bearish_divergence) if m.rsi_bearish_divergence is not None else None
        )

    if not cache_hit and need_trend:
        t = compute_trend_metrics(snap.candles.closes)
        payload["ema_20"] = str(t.ema_20) if t.ema_20 is not None else None
        payload["ema_50"] = str(t.ema_50) if t.ema_50 is not None else None
        payload["ema_200"] = str(t.ema_200) if t.ema_200 is not None else None
        payload["sma_20"] = str(t.sma_20) if t.sma_20 is not None else None
        payload["sma_50"] = str(t.sma_50) if t.sma_50 is not None else None
        payload["sma_200"] = str(t.sma_200) if t.sma_200 is not None else None
        payload["trend_crossover"] = t.crossover
        payload["trend_crossover_event"] = t.crossover_event
        payload["trend_crossover_strength_pct"] = str(t.crossover_strength_pct) if t.crossover_strength_pct is not None else None
        payload["ema_50_200_crossover"] = t.ema_50_200_crossover
        payload["ema_50_200_event"] = t.ema_50_200_event
        payload["ema_50_200_strength_pct"] = str(t.ema_50_200_strength_pct) if t.ema_50_200_strength_pct is not None else None
        payload["sma_20_50_crossover"] = t.sma_20_50_crossover
        payload["sma_20_50_event"] = t.sma_20_50_event
        payload["sma_50_200_crossover"] = t.sma_50_200_crossover
        payload["sma_50_200_event"] = t.sma_50_200_event
        payload["adx"] = str(t.adx) if t.adx is not None else None
        payload["adx_pos"] = str(t.adx_pos) if t.adx_pos is not None else None
        payload["adx_neg"] = str(t.adx_neg) if t.adx_neg is not None else None
        payload["adx_trend_strength"] = t.adx_trend_strength
        payload["ichi_tenkan"] = str(t.ichi_tenkan) if t.ichi_tenkan is not None else None
        payload["ichi_kijun"] = str(t.ichi_kijun) if t.ichi_kijun is not None else None
        payload["ichi_senkou_a"] = str(t.ichi_senkou_a) if t.ichi_senkou_a is not None else None
        payload["ichi_senkou_b"] = str(t.ichi_senkou_b) if t.ichi_senkou_b is not None else None
        payload["ichi_cloud_bias"] = t.ichi_cloud_bias
        payload["price_vs_ema20_pct"] = str(t.price_vs_ema20_pct) if t.price_vs_ema20_pct is not None else None
        payload["price_vs_ema50_pct"] = str(t.price_vs_ema50_pct) if t.price_vs_ema50_pct is not None else None
        payload["price_vs_ema200_pct"] = str(t.price_vs_ema200_pct) if t.price_vs_ema200_pct is not None else None
        payload["trend_bias"] = t.trend_bias
        payload["ema_20_prev"] = str(t.ema_20_prev) if t.ema_20_prev is not None else None
        payload["ema_50_prev"] = str(t.ema_50_prev) if t.ema_50_prev is not None else None
        payload["ema_200_prev"] = str(t.ema_200_prev) if t.ema_200_prev is not None else None
        payload["sma_20_prev"] = str(t.sma_20_prev) if t.sma_20_prev is not None else None
        payload["sma_50_prev"] = str(t.sma_50_prev) if t.sma_50_prev is not None else None
        payload["sma_200_prev"] = str(t.sma_200_prev) if t.sma_200_prev is not None else None

    if not cache_hit and need_volatility:
        highs: list[Decimal] = []
        lows: list[Decimal] = []
        try:
            for row in snap.klines:
                if not isinstance(row, list) or len(row) < 5:
                    continue
                highs.append(Decimal(str(row[2])))
                lows.append(Decimal(str(row[3])))
        except Exception:
            highs = []
            lows = []
        v = compute_volatility_metrics(highs, lows, snap.candles.closes)
        payload["atr"] = str(v.atr) if v.atr is not None else None
        payload["atr_pct"] = str(v.atr_pct) if v.atr_pct is not None else None
        payload["bb_upper"] = str(v.bb_upper) if v.bb_upper is not None else None
        payload["bb_mid"] = str(v.bb_mid) if v.bb_mid is not None else None
        payload["bb_lower"] = str(v.bb_lower) if v.bb_lower is not None else None
        payload["bb_width_pct"] = str(v.bb_width_pct) if v.bb_width_pct is not None else None
        payload["bb_pct_b"] = str(v.bb_pct_b) if v.bb_pct_b is not None else None
        payload["bb_position"] = v.bb_position
        payload["kc_upper"] = str(v.kc_upper) if v.kc_upper is not None else None
        payload["kc_lower"] = str(v.kc_lower) if v.kc_lower is not None else None
        payload["squeeze"] = str(v.squeeze) if v.squeeze is not None else None
        payload["hist_vol_pct"] = str(v.hist_vol_pct) if v.hist_vol_pct is not None else None
        payload["chandelier_long"] = str(v.chandelier_long) if v.chandelier_long is not None else None
        payload["chandelier_short"] = str(v.chandelier_short) if v.chandelier_short is not None else None
        payload["vol_regime"] = v.vol_regime

    # Shared depth snapshot for volume/execution
    depth_bids: list[tuple[Decimal, Decimal]] | None = None
    depth_asks: list[tuple[Decimal, Decimal]] | None = None
    depth_limit = 0
    if need_volume and vol_depth and vol_depth > 0:
        depth_limit = max(depth_limit, int(vol_depth))
    if need_execution:
        depth_limit = max(depth_limit, int(exec_depth))
    if not cache_hit and depth_limit > 0:
        try:
            depth = client.get_order_book(symbol=symbol, limit=int(depth_limit))
            bids = depth.get("bids") if isinstance(depth, dict) else None
            asks = depth.get("asks") if isinstance(depth, dict) else None
            if isinstance(bids, list):
                depth_bids = []
                for row in bids:
                    if isinstance(row, list) and len(row) >= 2:
                        depth_bids.append((Decimal(str(row[0])), Decimal(str(row[1]))))
            if isinstance(asks, list):
                depth_asks = []
                for row in asks:
                    if isinstance(row, list) and len(row) >= 2:
                        depth_asks.append((Decimal(str(row[0])), Decimal(str(row[1]))))
        except Exception:
            depth_bids = None
            depth_asks = None

    if not cache_hit and need_volume:
        base_vols: list[Decimal] = []
        quote_vols: list[Decimal] = []
        taker_buy_quote: list[Decimal] = []
        try:
            for row in snap.klines:
                if not isinstance(row, list) or len(row) < 8:
                    continue
                base_vols.append(Decimal(str(row[5])))
                quote_vols.append(Decimal(str(row[7])))
                if len(row) >= 11:
                    taker_buy_quote.append(Decimal(str(row[10])))
        except Exception:
            base_vols = []
            quote_vols = []
            taker_buy_quote = []
        bid_qty = None
        ask_qty = None
        if isinstance(snap.book_ticker, dict):
            try:
                bid_qty = Decimal(str(snap.book_ticker.get("bidQty")))
                ask_qty = Decimal(str(snap.book_ticker.get("askQty")))
            except Exception:
                bid_qty = None
                ask_qty = None
        vm = compute_volume_metrics(
            base_volumes=base_vols,
            quote_volumes=quote_vols,
            closes=snap.candles.closes,
            taker_buy_quote_volumes=taker_buy_quote or None,
            bid_qty=bid_qty,
            ask_qty=ask_qty,
            depth_bids=depth_bids,
            depth_asks=depth_asks,
            window_fast=vol_window_fast,
            window_slow=vol_window_slow,
            spike_ratio=Decimal(str(vol_spike_ratio)),
            z_threshold=Decimal(str(vol_zscore)),
            buy_ratio_hi=Decimal(str(vol_buy_ratio)),
            buy_ratio_lo=Decimal(str(vol_sell_ratio)),
            wall_ratio=Decimal(str(vol_wall_ratio)),
            imbalance_threshold=Decimal(str(vol_imbalance)),
        )
        payload["volume_base_last"] = str(vm.base_last) if vm.base_last is not None else None
        payload["volume_quote_last"] = str(vm.quote_last) if vm.quote_last is not None else None
        payload["volume_quote_avg_20"] = str(vm.quote_avg_20) if vm.quote_avg_20 is not None else None
        payload["volume_quote_avg_50"] = str(vm.quote_avg_50) if vm.quote_avg_50 is not None else None
        payload["volume_quote_std_20"] = str(vm.quote_std_20) if vm.quote_std_20 is not None else None
        payload["volume_quote_zscore_20"] = str(vm.quote_zscore_20) if vm.quote_zscore_20 is not None else None
        payload["volume_quote_trend"] = vm.quote_trend
        payload["volume_spike"] = str(vm.spike) if vm.spike is not None else None
        payload["taker_buy_ratio"] = str(vm.taker_buy_ratio) if vm.taker_buy_ratio is not None else None
        payload["taker_buy_ratio_avg20"] = str(vm.taker_buy_ratio_avg20) if vm.taker_buy_ratio_avg20 is not None else None
        payload["buy_pressure"] = vm.buy_pressure
        payload["sustained_buy_pressure"] = str(vm.sustained_buy_pressure) if vm.sustained_buy_pressure is not None else None
        payload["sustained_sell_pressure"] = str(vm.sustained_sell_pressure) if vm.sustained_sell_pressure is not None else None
        payload["obv"] = str(vm.obv) if vm.obv is not None else None
        payload["obv_trend"] = vm.obv_trend
        payload["vwap_20"] = str(vm.vwap_20) if vm.vwap_20 is not None else None
        payload["price_vs_vwap_pct"] = str(vm.price_vs_vwap_pct) if vm.price_vs_vwap_pct is not None else None
        payload["vol_price_confirmation"] = vm.vol_price_confirmation
        payload["bid_qty"] = str(vm.bid_qty) if vm.bid_qty is not None else None
        payload["ask_qty"] = str(vm.ask_qty) if vm.ask_qty is not None else None
        payload["book_imbalance"] = str(vm.book_imbalance) if vm.book_imbalance is not None else None
        payload["liquidity_zones"] = vm.liquidity_zones
        payload["buy_wall_price"] = str(vm.buy_wall_price) if vm.buy_wall_price is not None else None
        payload["buy_wall_qty"] = str(vm.buy_wall_qty) if vm.buy_wall_qty is not None else None
        payload["sell_wall_price"] = str(vm.sell_wall_price) if vm.sell_wall_price is not None else None
        payload["sell_wall_qty"] = str(vm.sell_wall_qty) if vm.sell_wall_qty is not None else None

    if not cache_hit and need_execution:
        best_bid = None
        best_ask = None
        if isinstance(snap.book_ticker, dict):
            try:
                best_bid = Decimal(str(snap.book_ticker.get("bidPrice")))
                best_ask = Decimal(str(snap.book_ticker.get("askPrice")))
            except Exception:
                best_bid = None
                best_ask = None
        if depth_bids is None or depth_asks is None:
            payload["execution_available"] = "False"
            payload["execution_unavailable_reason"] = "missing_depth"
        else:
            em = compute_execution_metrics(
                bids=depth_bids,
                asks=depth_asks,
                best_bid=best_bid,
                best_ask=best_ask,
                depth_levels=int(exec_depth),
                notional=exec_notional,
                side=str(exec_side),
            )
            payload["execution_available"] = str(em.available)
            payload["execution_unavailable_reason"] = em.unavailable_reason
            payload["exec_mid"] = str(em.mid_price) if em.mid_price is not None else None
            payload["exec_best_bid"] = str(em.best_bid) if em.best_bid is not None else None
            payload["exec_best_ask"] = str(em.best_ask) if em.best_ask is not None else None
            payload["exec_spread_abs"] = str(em.spread_abs) if em.spread_abs is not None else None
            payload["exec_spread_pct"] = str(em.spread_pct) if em.spread_pct is not None else None
            payload["exec_spread_quality"] = em.spread_quality
            payload["exec_slippage_pct"] = str(em.slippage_pct) if em.slippage_pct is not None else None
            payload["exec_effective_spread_pct"] = str(em.effective_spread_pct) if em.effective_spread_pct is not None else None
            payload["exec_market_impact_pct"] = str(em.market_impact_pct) if em.market_impact_pct is not None else None
            payload["exec_avg_fill_price"] = str(em.avg_fill_price) if em.avg_fill_price is not None else None
            payload["exec_notional_used"] = str(em.notional_used) if em.notional_used is not None else None
            payload["exec_fill_ratio_pct"] = str(em.fill_ratio_pct) if em.fill_ratio_pct is not None else None
            payload["exec_levels_used"] = str(em.levels_used) if em.levels_used is not None else None
            payload["exec_side"] = em.side
            payload["exec_depth_levels"] = str(em.depth_levels) if em.depth_levels is not None else None
            payload["exec_bid_depth_notional"] = str(em.bid_depth_notional) if em.bid_depth_notional is not None else None
            payload["exec_ask_depth_notional"] = str(em.ask_depth_notional) if em.ask_depth_notional is not None else None
            payload["exec_notional_available"] = str(em.notional_available) if em.notional_available is not None else None
            payload["exec_depth_imbalance"] = str(em.depth_imbalance) if em.depth_imbalance is not None else None
            payload["exec_depth_spread_pct"] = str(em.depth_spread_pct) if em.depth_spread_pct is not None else None

    if not cache_hit and getattr(args, "quant", False):
        quant_unavailable = None
        bench_klines: list[list] = []
        try:
            bench_klines = client.get_klines(
                symbol=quant_benchmark,
                interval=timeframe,
                limit=len(snap.klines),
            )
        except Exception:
            quant_unavailable = "benchmark_fetch_failed"
            bench_klines = []
        quote_vols_float: list[float] = []
        try:
            for row in snap.klines:
                if isinstance(row, list) and len(row) >= 8:
                    quote_vols_float.append(float(row[7]))
        except Exception:
            quote_vols_float = []
        qm = compute_quant_metrics(
            target_klines=snap.klines,
            benchmark_klines=bench_klines,
            window=quant_window,
            benchmark_symbol=quant_benchmark,
            corr_method=corr_method,
            spread_pct=float(payload.get("spread_pct")) if payload.get("spread_pct") not in (None, "") else None,
            range_pct=float(payload.get("volatility_pct")) if payload.get("volatility_pct") not in (None, "") else None,
            quote_volumes=quote_vols_float or None,
        )
        if quant_unavailable and qm.available:
            quant_unavailable = None
        payload["quant_available"] = str(qm.available)
        payload["quant_unavailable_reason"] = quant_unavailable or qm.unavailable_reason
        payload["quant_window"] = str(qm.window)
        payload["quant_benchmark"] = qm.benchmark
        payload["quant_corr_method"] = qm.corr_method
        payload["quant_corr"] = str(qm.correlation) if qm.correlation is not None else None
        payload["quant_beta"] = str(qm.beta) if qm.beta is not None else None
        payload["quant_return_zscore"] = str(qm.return_zscore) if qm.return_zscore is not None else None
        payload["quant_realized_vol"] = str(qm.realized_vol) if qm.realized_vol is not None else None
        payload["quant_vol_regime"] = qm.vol_regime
        payload["quant_mean_dev_pct"] = str(qm.mean_dev_pct) if qm.mean_dev_pct is not None else None
        payload["quant_mean_reversion_state"] = qm.mean_reversion_state
        payload["quant_max_drawdown_pct"] = str(qm.max_drawdown_pct) if qm.max_drawdown_pct is not None else None
        payload["quant_sharpe_ratio"] = str(qm.sharpe_ratio) if qm.sharpe_ratio is not None else None
        payload["quant_calmar_ratio"] = str(qm.calmar_ratio) if qm.calmar_ratio is not None else None
        payload["quant_skewness"] = str(qm.skewness) if qm.skewness is not None else None
        payload["quant_kurtosis"] = str(qm.kurtosis) if qm.kurtosis is not None else None
        payload["quant_log_return"] = str(qm.log_return) if qm.log_return is not None else None
        payload["quant_ret_mean"] = str(qm.rolling_return_mean) if qm.rolling_return_mean is not None else None
        payload["quant_ret_std"] = str(qm.rolling_return_std) if qm.rolling_return_std is not None else None
        payload["quant_price_vs_ema_pct"] = str(qm.price_vs_ema_pct) if qm.price_vs_ema_pct is not None else None
        payload["quant_rsi"] = str(qm.rsi) if qm.rsi is not None else None
        payload["quant_macd_hist"] = str(qm.macd_hist) if qm.macd_hist is not None else None
        payload["quant_atr_norm"] = str(qm.atr_norm) if qm.atr_norm is not None else None
        payload["quant_volume_zscore"] = str(qm.volume_zscore) if qm.volume_zscore is not None else None
        payload["quant_spread_pct"] = str(qm.spread_pct) if qm.spread_pct is not None else None
        payload["quant_range_pct"] = str(qm.range_pct) if qm.range_pct is not None else None

    if not cache_hit and need_structure:
        highs: list[Decimal] = []
        lows: list[Decimal] = []
        try:
            for row in snap.klines:
                if not isinstance(row, list) or len(row) < 5:
                    continue
                highs.append(Decimal(str(row[2])))
                lows.append(Decimal(str(row[3])))
        except Exception:
            highs = []
            lows = []
        atr_pct = None
        if payload.get("atr_pct") is not None:
            try:
                atr_pct = Decimal(str(payload.get("atr_pct")))
            except Exception:
                atr_pct = None
        sm = compute_structure_metrics(
            highs=highs,
            lows=lows,
            closes=snap.candles.closes,
            atr_pct=atr_pct,
            volume_trend=payload.get("volume_quote_trend"),
            buy_pressure=payload.get("buy_pressure"),
        )
        payload["structure_trend"] = sm.structure_trend
        payload["range_state"] = sm.range_state
        payload["range_high"] = str(sm.range_high) if sm.range_high is not None else None
        payload["range_low"] = str(sm.range_low) if sm.range_low is not None else None
        payload["range_width_pct"] = str(sm.range_width_pct) if sm.range_width_pct is not None else None
        payload["bos"] = str(sm.bos) if sm.bos is not None else None
        payload["bos_direction"] = sm.bos_direction
        payload["bos_streak"] = str(sm.bos_streak) if sm.bos_streak is not None else None
        payload["choch"] = str(sm.choch) if sm.choch is not None else None
        payload["choch_direction"] = sm.choch_direction
        payload["accumulation"] = sm.accumulation
        payload["price_zone"] = sm.price_zone
        payload["last_swing_high"] = str(sm.last_swing_high) if sm.last_swing_high is not None else None
        payload["last_swing_low"] = str(sm.last_swing_low) if sm.last_swing_low is not None else None
        payload["prev_swing_high"] = str(sm.prev_swing_high) if sm.prev_swing_high is not None else None
        payload["prev_swing_low"] = str(sm.prev_swing_low) if sm.prev_swing_low is not None else None
        payload["swing_high_history"] = str(list(sm.swing_high_history)) if sm.swing_high_history else None
        payload["swing_low_history"] = str(list(sm.swing_low_history)) if sm.swing_low_history else None
        payload["fvg_count"] = str(len(sm.fvg_list)) if sm.fvg_list is not None else None
        payload["last_fvg_direction"] = sm.last_fvg.direction if sm.last_fvg is not None else None
        payload["last_fvg_high"] = str(sm.last_fvg.gap_high) if sm.last_fvg is not None else None
        payload["last_fvg_low"] = str(sm.last_fvg.gap_low) if sm.last_fvg is not None else None
        payload["last_fvg_mitigated"] = str(sm.last_fvg.mitigated) if sm.last_fvg is not None else None

    if not cache_hit and need_price_action:
        opens: list[Decimal] = []
        highs: list[Decimal] = []
        lows: list[Decimal] = []
        closes: list[Decimal] = []
        volumes: list[Decimal] = []
        try:
            for row in snap.klines:
                if not isinstance(row, list) or len(row) < 6:
                    continue
                opens.append(Decimal(str(row[1])))
                highs.append(Decimal(str(row[2])))
                lows.append(Decimal(str(row[3])))
                closes.append(Decimal(str(row[4])))
                volumes.append(Decimal(str(row[5])))
        except Exception:
            opens, highs, lows, closes, volumes = [], [], [], [], []
        if len(opens) < 5:
            payload["price_action_available"] = "False"
            payload["pa_unavailable_reason"] = "insufficient_candles"
        else:
            pm = compute_price_action_metrics(
                opens=opens,
                highs=highs,
                lows=lows,
                closes=closes,
                volumes=volumes or None,
            )
            payload["price_action_available"] = "True"
            payload["pa_support_level"] = str(pm.support_level) if pm.support_level is not None else None
            payload["pa_support_strength"] = str(pm.support_strength) if pm.support_strength is not None else None
            payload["pa_support_distance_pct"] = str(pm.support_distance_pct) if pm.support_distance_pct is not None else None
            payload["pa_resistance_level"] = str(pm.resistance_level) if pm.resistance_level is not None else None
            payload["pa_resistance_strength"] = str(pm.resistance_strength) if pm.resistance_strength is not None else None
            payload["pa_resistance_distance_pct"] = str(pm.resistance_distance_pct) if pm.resistance_distance_pct is not None else None
            payload["pa_structure_type"] = pm.structure_type
            payload["pa_last_swing_high"] = str(pm.last_swing_high) if pm.last_swing_high is not None else None
            payload["pa_last_swing_low"] = str(pm.last_swing_low) if pm.last_swing_low is not None else None
            payload["pa_breakout"] = str(pm.breakout)
            payload["pa_breakdown"] = str(pm.breakdown)
            payload["pa_breakout_level"] = str(pm.breakout_level) if pm.breakout_level is not None else None
            payload["pa_breakdown_level"] = str(pm.breakdown_level) if pm.breakdown_level is not None else None
            payload["pa_breakout_strength"] = pm.breakout_strength
            if pm.patterns:
                payload["pa_patterns_json"] = _safe_json(
                    [
                        {
                            "pattern_name": p.pattern_name,
                            "direction": p.direction,
                            "candle_count": p.candle_count,
                            "bar_index": p.bar_index,
                            "reliability": str(p.reliability) if p.reliability is not None else None,
                            "context_valid": p.context_valid,
                            "strength_score": str(p.strength_score) if p.strength_score is not None else None,
                        }
                        for p in pm.patterns
                    ]
                )
            else:
                payload["pa_patterns_json"] = None
            payload["pa_last_pattern"] = pm.last_pattern
            payload["pa_dominant_bias"] = pm.dominant_bias
            payload["pa_signal_count"] = str(pm.signal_count)
            payload["pa_confluence"] = str(pm.confluence)
    elif getattr(args, "price_action", False):
        payload["price_action_available"] = "False"
        payload["pa_unavailable_reason"] = "missing_ohlc"

    if not cache_hit and risk_on:
        def _dec(val: object) -> Decimal | None:
            if val in (None, ""):
                return None
            try:
                return Decimal(str(val))
            except Exception:
                return None

        def _bool_from_str(val: object) -> bool | None:
            if val in (None, ""):
                return None
            s = str(val).strip().lower()
            if s == "true":
                return True
            if s == "false":
                return False
            return None

        def _infer_quote_asset(sym: str) -> str | None:
            for q in ("USDT", "USDC", "BUSD", "FDUSD", "TUSD", "BTC", "ETH", "BNB"):
                if sym.endswith(q):
                    return q
            return None

        entry = risk_entry or _dec(payload.get("exec_mid")) or _dec(payload.get("last_price"))
        if entry is None:
            payload["risk_available"] = "False"
            payload["risk_rejection_reason"] = "missing_entry_price"
        else:
            acct = risk_account_balance
            if acct is None:
                quote_asset = _infer_quote_asset(symbol)
                if quote_asset:
                    try:
                        with connect(db_path) as conn:
                            state = StateManager(conn)
                            acct = state.get_cached_balance_free(asset=quote_asset)
                    except Exception:
                        acct = None
            if acct is None:
                payload["risk_available"] = "False"
                payload["risk_rejection_reason"] = "missing_account_balance"
            else:
                last_low = None
                last_high = None
                try:
                    if snap.klines:
                        last_low = _dec(snap.klines[-1][3])
                        last_high = _dec(snap.klines[-1][2])
                except Exception:
                    last_low = None
                    last_high = None

                rm = compute_risk_metrics(
                    entry_price=entry,
                    side=risk_side,
                    account_balance=acct,
                    risk_pct=risk_pct,
                    max_position_pct=risk_max_position_pct,
                    atr=_dec(payload.get("atr")),
                    vol_regime=payload.get("vol_regime"),
                    chandelier_long=_dec(payload.get("chandelier_long")),
                    chandelier_short=_dec(payload.get("chandelier_short")),
                    structure_trend=payload.get("structure_trend"),
                    last_swing_low=_dec(payload.get("last_swing_low")),
                    last_swing_high=_dec(payload.get("last_swing_high")),
                    prev_swing_low=_dec(payload.get("prev_swing_low")),
                    prev_swing_high=_dec(payload.get("prev_swing_high")),
                    price_zone=payload.get("price_zone"),
                    bos_streak=int(payload.get("bos_streak")) if payload.get("bos_streak") not in (None, "") else None,
                    choch=_bool_from_str(payload.get("choch")),
                    adx=_dec(payload.get("adx")),
                    adx_trend_strength=payload.get("adx_trend_strength"),
                    ema_50_200_crossover=payload.get("ema_50_200_crossover"),
                    trend_bias=payload.get("trend_bias"),
                    composite_signal=payload.get("composite_signal"),
                    rsi_zone=payload.get("rsi_zone"),
                    macd_bias=payload.get("macd_bias"),
                    slippage_pct=_dec(payload.get("exec_slippage_pct")),
                    spread_pct=_dec(payload.get("exec_spread_pct")),
                    notional_available=_dec(payload.get("exec_notional_available")),
                    fill_ratio_pct=_dec(payload.get("exec_fill_ratio_pct")),
                    buy_pressure=payload.get("buy_pressure"),
                    sustained_buy_pressure=_bool_from_str(payload.get("sustained_buy_pressure")),
                    vol_price_confirmation=payload.get("vol_price_confirmation"),
                    last_candle_low=last_low,
                    last_candle_high=last_high,
                    last_fvg_direction=payload.get("last_fvg_direction"),
                    last_fvg_low=_dec(payload.get("last_fvg_low")),
                    last_fvg_high=_dec(payload.get("last_fvg_high")),
                    ichi_senkou_a=_dec(payload.get("ichi_senkou_a")),
                    ichi_senkou_b=_dec(payload.get("ichi_senkou_b")),
                )

                payload["risk_available"] = "True"
                payload["risk_viable"] = str(rm.viable)
                payload["risk_rejection_reason"] = rm.rejection_reason
                payload["risk_score"] = str(rm.risk_score) if rm.risk_score is not None else None
                payload["risk_score_breakdown"] = _safe_json(rm.risk_score_breakdown) if rm.risk_score_breakdown else None
                payload["risk_entry_price"] = str(rm.entry_price) if rm.entry_price is not None else None
                payload["risk_side"] = rm.side
                payload["risk_effective_entry"] = str(rm.effective_entry) if rm.effective_entry is not None else None
                payload["risk_stop_price"] = str(rm.stop_price) if rm.stop_price is not None else None
                payload["risk_stop_method"] = rm.stop_method
                payload["risk_stop_distance_pct"] = str(rm.stop_distance_pct) if rm.stop_distance_pct is not None else None
                payload["risk_stop_atr_multiple"] = str(rm.stop_atr_multiple) if rm.stop_atr_multiple is not None else None
                payload["risk_stop_candidates"] = _safe_json(rm.stop_candidates) if rm.stop_candidates else None
                payload["risk_tp1"] = str(rm.tp1) if rm.tp1 is not None else None
                payload["risk_tp2"] = str(rm.tp2) if rm.tp2 is not None else None
                payload["risk_tp3"] = str(rm.tp3) if rm.tp3 is not None else None
                payload["risk_tp_fvg"] = str(rm.tp_fvg) if rm.tp_fvg is not None else None
                payload["risk_tp_structure"] = str(rm.tp_structure) if rm.tp_structure is not None else None
                payload["risk_tp_cloud"] = str(rm.tp_cloud) if rm.tp_cloud is not None else None
                payload["risk_reward_risk_ratio"] = str(rm.reward_risk_ratio) if rm.reward_risk_ratio is not None else None
                payload["risk_position_size_base"] = str(rm.position_size_base) if rm.position_size_base is not None else None
                payload["risk_position_size_quote"] = str(rm.position_size_quote) if rm.position_size_quote is not None else None
                payload["risk_position_size_pct"] = str(rm.position_size_pct) if rm.position_size_pct is not None else None
                payload["risk_max_loss_quote"] = str(rm.max_loss_quote) if rm.max_loss_quote is not None else None
                payload["risk_pct_used"] = str(rm.risk_pct_used) if rm.risk_pct_used is not None else None
                payload["risk_caps_applied"] = _safe_json(rm.caps_applied) if rm.caps_applied else None
                payload["risk_suggested_leverage"] = str(rm.suggested_leverage) if rm.suggested_leverage is not None else None
                payload["risk_liquidation_price"] = str(rm.liquidation_price) if rm.liquidation_price is not None else None
                payload["risk_liquidation_distance_pct"] = str(rm.liquidation_distance_pct) if rm.liquidation_distance_pct is not None else None
                payload["risk_flag_wide_stop"] = str(rm.wide_stop)
                payload["risk_flag_concentration_cap"] = str(rm.concentration_cap)
                payload["risk_flag_liquidity_cap"] = str(rm.liquidity_cap)
                payload["risk_flag_liquidation_warning"] = str(rm.liquidation_warning)
                payload["risk_flag_low_adx_warning"] = str(rm.low_adx_warning)

    if not cache_hit and getattr(args, "crypto", False):
        futures_market = "usdtm" if symbol.endswith("USDT") else "coinm"
        crypto = compute_crypto_metrics(
            symbol=symbol,
            futures_market=futures_market,
            timeout_s=cfg.binance_timeout_s,
            tls_verify=cfg.binance_tls_verify if not insecure else False,
            ca_bundle_path=ca_bundle.expanduser() if ca_bundle else cfg.binance_ca_bundle_path,
        )
        payload["futures_market"] = crypto.futures_market
        payload["funding_rate"] = crypto.funding_rate
        payload["next_funding_time"] = crypto.next_funding_time
        payload["open_interest"] = crypto.open_interest

    if getattr(args, "strict", False):
        if getattr(args, "momentum", False):
            if any(payload.get(k) in (None, "") for k in ("rsi", "macd", "macd_signal", "macd_hist", "stoch_rsi")):
                print("ERROR: momentum indicators unavailable (strict mode)")
                return 2
        if getattr(args, "trend", False):
            if any(payload.get(k) in (None, "") for k in ("ema_20", "ema_50", "sma_20", "sma_50")):
                print("ERROR: trend indicators unavailable (strict mode)")
                return 2
        if getattr(args, "volatility", False):
            if any(payload.get(k) in (None, "") for k in ("atr", "bb_upper", "bb_mid", "bb_lower", "bb_width_pct")):
                print("ERROR: volatility indicators unavailable (strict mode)")
                return 2
        if getattr(args, "crypto", False):
            if any(payload.get(k) in (None, "") for k in ("funding_rate", "open_interest")):
                print("ERROR: crypto indicators unavailable (strict mode)")
                return 2
        if getattr(args, "quant", False):
            if payload.get("quant_available") != "True":
                print("ERROR: quant indicators unavailable (strict mode)")
                return 2
        if getattr(args, "execution", False):
            if payload.get("execution_available") != "True":
                print("ERROR: execution indicators unavailable (strict mode)")
                return 2
        if getattr(args, "price_action", False):
            if payload.get("price_action_available") != "True":
                print("ERROR: price-action indicators unavailable (strict mode)")
                return 2
        if getattr(args, "risk", False):
            if payload.get("risk_available") != "True":
                print("ERROR: risk indicators unavailable (strict mode)")
                return 2

    if cache_ttl_s > 0:
        payload["cache"] = "hit" if cache_hit else "miss"

    if getattr(args, "json", False):
        print(_json.dumps(payload, separators=(",", ":")))
    elif getattr(args, "compact", False):
        summary = (
            f"{symbol} {timeframe} candles={payload.get('candle_count')} price={payload.get('last_price')} spread_pct={payload.get('spread_pct')} "
            f"chg24h={payload.get('change_pct_24h')} vol24h={payload.get('volume_quote_24h')} cond={cond}"
        )
        if getattr(args, "volume", False):
            summary += (
                f" vol_trend={payload.get('volume_quote_trend')}"
                f" spike={payload.get('volume_spike')}"
                f" pressure={payload.get('buy_pressure')}"
            )
        if getattr(args, "price_action", False):
            summary += (
                f" pa_bias={payload.get('pa_dominant_bias')}"
                f" pa_breakout={payload.get('pa_breakout')}"
            )
        if getattr(args, "risk", False):
            if payload.get("risk_available") == "True":
                summary += (
                    f" risk_score={payload.get('risk_score')}"
                    f" rr={payload.get('risk_reward_risk_ratio')}"
                    f" viable={payload.get('risk_viable')}"
                )
            else:
                summary += " risk=unavailable"
        if cache_ttl_s > 0:
            summary += f" cache={payload.get('cache')}"
        print(summary)
    elif getattr(args, "table", False):
        print("MARKET STATUS")
        for k, v in payload.items():
            print(f"{k:<18} {v}")
    else:
        print("Market Status")
        print(f"- Symbol: {symbol}")
        print(f"- Timeframe: {timeframe}")
        print(f"- Candles: {payload.get('candle_count')}")
        print(f"- Last Price: {payload.get('last_price')}")
        if payload.get("bid") is not None and payload.get("ask") is not None:
            print(f"- Best Bid: {payload.get('bid')}")
            print(f"- Best Ask: {payload.get('ask')}")
        if payload.get("spread_pct") is not None:
            print(f"- Spread %: {payload.get('spread_pct')}")
        if payload.get("high_24h") is not None:
            print(f"- 24h High: {payload.get('high_24h')}")
        if payload.get("low_24h") is not None:
            print(f"- 24h Low: {payload.get('low_24h')}")
        if payload.get("change_pct_24h") is not None:
            print(f"- 24h Change %: {payload.get('change_pct_24h')}")
        if payload.get("volume_quote_24h") is not None:
            print(f"- 24h Volume (quote): {payload.get('volume_quote_24h')}")
        print(f"- Condition Summary: {cond} (momentum_pct={payload.get('momentum_pct')}, volatility_pct={payload.get('volatility_pct')})")
        if getattr(args, "momentum", False):
            momentum_bias = payload.get("composite_signal")
            if momentum_bias:
                print(f"- Momentum Bias: {momentum_bias}")
        if getattr(args, "trend", False):
            trend_bias = payload.get("trend_bias")
            adx_strength = payload.get("adx_trend_strength")
            ichi_bias = payload.get("ichi_cloud_bias")
            if trend_bias or adx_strength or ichi_bias:
                pieces = []
                if trend_bias:
                    pieces.append(f"bias={trend_bias}")
                if adx_strength:
                    pieces.append(f"adx={adx_strength}")
                if ichi_bias:
                    pieces.append(f"ichimoku={ichi_bias}")
                print(f"- Trend Bias: {' '.join(pieces)}")
        if getattr(args, "volatility", False):
            vol_regime = payload.get("vol_regime")
            squeeze = payload.get("squeeze")
            bb_pos = payload.get("bb_position")
            if vol_regime or squeeze or bb_pos:
                pieces = []
                if vol_regime:
                    pieces.append(f"regime={vol_regime}")
                if squeeze is not None:
                    pieces.append(f"squeeze={squeeze}")
                if bb_pos:
                    pieces.append(f"bb_pos={bb_pos}")
                print(f"- Volatility Bias: {' '.join(pieces)}")
        if getattr(args, "volume", False):
            vol_trend = payload.get("volume_quote_trend")
            spike = payload.get("volume_spike")
            buy_pressure = payload.get("buy_pressure")
            if vol_trend or spike or buy_pressure:
                pieces = []
                if vol_trend:
                    pieces.append(f"trend={vol_trend}")
                if spike is not None:
                    pieces.append(f"spike={spike}")
                if buy_pressure:
                    pieces.append(f"pressure={buy_pressure}")
                print(f"- Volume Bias: {' '.join(pieces)}")
        if getattr(args, "structure", False):
            structure_trend = payload.get("structure_trend")
            range_state = payload.get("range_state")
            bos = payload.get("bos")
            choch = payload.get("choch")
            if structure_trend or range_state or bos or choch:
                pieces = []
                if structure_trend:
                    pieces.append(f"trend={structure_trend}")
                if range_state:
                    pieces.append(f"range={range_state}")
                if bos is not None:
                    pieces.append(f"bos={bos}")
                if choch is not None:
                    pieces.append(f"choch={choch}")
                print(f"- Structure Bias: {' '.join(pieces)}")
        if getattr(args, "price_action", False):
            print("Price Action")
            print(f"- Support: {payload.get('pa_support_level')} (strength={payload.get('pa_support_strength')}, dist_pct={payload.get('pa_support_distance_pct')})")
            print(f"- Resistance: {payload.get('pa_resistance_level')} (strength={payload.get('pa_resistance_strength')}, dist_pct={payload.get('pa_resistance_distance_pct')})")
            print(f"- Structure: {payload.get('pa_structure_type')} last_high={payload.get('pa_last_swing_high')} last_low={payload.get('pa_last_swing_low')}")
            if payload.get("pa_breakout") == "True":
                print(f"- Breakout: level={payload.get('pa_breakout_level')} strength={payload.get('pa_breakout_strength')}")
            if payload.get("pa_breakdown") == "True":
                print(f"- Breakdown: level={payload.get('pa_breakdown_level')} strength={payload.get('pa_breakout_strength')}")
            print(f"- Patterns: last={payload.get('pa_last_pattern')} dominant_bias={payload.get('pa_dominant_bias')} confluence={payload.get('pa_confluence')}")
        if getattr(args, "execution", False):
            if payload.get("execution_available") == "True":
                spread_q = payload.get("exec_spread_quality")
                slip = payload.get("exec_slippage_pct")
                imb = payload.get("exec_depth_imbalance")
                pieces = []
                if spread_q:
                    pieces.append(f"spread={spread_q}")
                if slip is not None:
                    pieces.append(f"slip={slip}")
                if imb is not None:
                    pieces.append(f"depth_imb={imb}")
                if pieces:
                    print(f"- Execution Summary: {' '.join(pieces)}")
            else:
                reason = payload.get("execution_unavailable_reason") or "unavailable"
                print(f"- Execution Summary: unavailable ({reason})")
        if getattr(args, "quant", False):
            if payload.get("quant_available") == "True":
                pieces = []
                if payload.get("quant_corr") is not None:
                    pieces.append(f"corr={payload.get('quant_corr')}")
                if payload.get("quant_return_zscore") is not None:
                    pieces.append(f"z={payload.get('quant_return_zscore')}")
                if payload.get("quant_vol_regime"):
                    pieces.append(f"regime={payload.get('quant_vol_regime')}")
                if payload.get("quant_mean_reversion_state"):
                    pieces.append(f"mean_state={payload.get('quant_mean_reversion_state')}")
                if pieces:
                    print(f"- Quant Summary: {' '.join(pieces)}")
            else:
                reason = payload.get("quant_unavailable_reason") or "unavailable"
                print(f"- Quant Summary: unavailable ({reason})")
        if getattr(args, "momentum", False):
            print("Momentum")
            print(f"- RSI: {payload.get('rsi')}")
            print(f"- RSI Prev: {payload.get('rsi_prev')}")
            print(f"- RSI Zone: {payload.get('rsi_zone')}")
            print(f"- MACD: {payload.get('macd')}")
            print(f"- MACD Signal: {payload.get('macd_signal')}")
            print(f"- MACD Hist: {payload.get('macd_hist')}")
            print(f"- MACD Bias: {payload.get('macd_bias')}")
            print(f"- Stoch RSI: {payload.get('stoch_rsi')}")
            if payload.get("stoch_rsi_k") is not None or payload.get("stoch_rsi_d") is not None:
                print(f"- Stoch RSI K: {payload.get('stoch_rsi_k')}")
                print(f"- Stoch RSI D: {payload.get('stoch_rsi_d')}")
                print(f"- Stoch RSI Bias: {payload.get('stoch_rsi_bias')}")
            print(f"- Williams %R: {payload.get('williams_r')}")
            print(f"- Williams %R Zone: {payload.get('williams_r_zone')}")
            print(f"- CCI: {payload.get('cci')}")
            print(f"- CCI Zone: {payload.get('cci_zone')}")
            print(f"- ROC: {payload.get('roc')}")
            print(f"- ROC Bias: {payload.get('roc_bias')}")
            print(f"- Composite Signal: {payload.get('composite_signal')}")
            print(f"- RSI Bullish Divergence: {payload.get('rsi_bullish_divergence')}")
            print(f"- RSI Bearish Divergence: {payload.get('rsi_bearish_divergence')}")
        if getattr(args, "trend", False):
            print("Trend")
            print(f"- EMA 20: {payload.get('ema_20')}")
            print(f"- EMA 50: {payload.get('ema_50')}")
            print(f"- EMA 200: {payload.get('ema_200')}")
            print(f"- SMA 20: {payload.get('sma_20')}")
            print(f"- SMA 50: {payload.get('sma_50')}")
            print(f"- SMA 200: {payload.get('sma_200')}")
            print(f"- Crossover: {payload.get('trend_crossover')}")
            print(f"- Crossover Event: {payload.get('trend_crossover_event')}")
            print(f"- Crossover Strength %: {payload.get('trend_crossover_strength_pct')}")
            print(f"- EMA 50/200: {payload.get('ema_50_200_crossover')} ({payload.get('ema_50_200_event')})")
            print(f"- EMA 50/200 Strength %: {payload.get('ema_50_200_strength_pct')}")
            print(f"- SMA 20/50: {payload.get('sma_20_50_crossover')} ({payload.get('sma_20_50_event')})")
            print(f"- SMA 50/200: {payload.get('sma_50_200_crossover')} ({payload.get('sma_50_200_event')})")
            print(f"- ADX: {payload.get('adx')} (+DI={payload.get('adx_pos')}, -DI={payload.get('adx_neg')})")
            print(f"- ADX Strength: {payload.get('adx_trend_strength')}")
            print(f"- Ichimoku Tenkan: {payload.get('ichi_tenkan')}")
            print(f"- Ichimoku Kijun: {payload.get('ichi_kijun')}")
            print(f"- Ichimoku Senkou A: {payload.get('ichi_senkou_a')}")
            print(f"- Ichimoku Senkou B: {payload.get('ichi_senkou_b')}")
            print(f"- Ichimoku Cloud Bias: {payload.get('ichi_cloud_bias')}")
            print(f"- Price vs EMA20 %: {payload.get('price_vs_ema20_pct')}")
            print(f"- Price vs EMA50 %: {payload.get('price_vs_ema50_pct')}")
            print(f"- Price vs EMA200 %: {payload.get('price_vs_ema200_pct')}")
            print(f"- Trend Bias: {payload.get('trend_bias')}")
            if len(snap.candles.closes) < 200:
                print(f"- Warning: need 200 candles for EMA/SMA 200 (have {len(snap.candles.closes)})")
            if getattr(args, "debug", False):
                print("Trend Debug")
                print(f"- EMA20 prev: {payload.get('ema_20_prev')}")
                print(f"- EMA50 prev: {payload.get('ema_50_prev')}")
                print(f"- EMA200 prev: {payload.get('ema_200_prev')}")
                print(f"- SMA20 prev: {payload.get('sma_20_prev')}")
                print(f"- SMA50 prev: {payload.get('sma_50_prev')}")
                print(f"- SMA200 prev: {payload.get('sma_200_prev')}")
        if getattr(args, "volatility", False):
            print("Volatility")
            print(f"- ATR: {payload.get('atr')}")
            print(f"- ATR %: {payload.get('atr_pct')}")
            print(f"- BB Upper: {payload.get('bb_upper')}")
            print(f"- BB Mid: {payload.get('bb_mid')}")
            print(f"- BB Lower: {payload.get('bb_lower')}")
            print(f"- BB Width %: {payload.get('bb_width_pct')}")
            print(f"- BB %B: {payload.get('bb_pct_b')}")
            print(f"- BB Position: {payload.get('bb_position')}")
            print(f"- KC Upper: {payload.get('kc_upper')}")
            print(f"- KC Lower: {payload.get('kc_lower')}")
            print(f"- Squeeze: {payload.get('squeeze')}")
            print(f"- Hist Vol %: {payload.get('hist_vol_pct')}")
            print(f"- Chandelier Long: {payload.get('chandelier_long')}")
            print(f"- Chandelier Short: {payload.get('chandelier_short')}")
            print(f"- Regime: {payload.get('vol_regime')}")
            if payload.get("bb_upper") in (None, "") and len(snap.candles.closes) < 20:
                print(f"- Warning: need 20 candles for BB (have {len(snap.candles.closes)})")
        if getattr(args, "volume", False):
            print("Volume")
            print(f"- Base Vol (last): {payload.get('volume_base_last')}")
            print(f"- Quote Vol (last): {payload.get('volume_quote_last')}")
            print(f"- Quote Vol MA20: {payload.get('volume_quote_avg_20')}")
            print(f"- Quote Vol MA50: {payload.get('volume_quote_avg_50')}")
            print(f"- Quote Vol Std20: {payload.get('volume_quote_std_20')}")
            print(f"- Quote Vol Z20: {payload.get('volume_quote_zscore_20')}")
            print(f"- Trend: {payload.get('volume_quote_trend')}")
            print(f"- Spike: {payload.get('volume_spike')}")
            print(f"- Taker Buy Ratio: {payload.get('taker_buy_ratio')}")
            print(f"- Taker Buy Ratio MA20: {payload.get('taker_buy_ratio_avg20')}")
            print(f"- Buy Pressure: {payload.get('buy_pressure')}")
            print(f"- OBV: {payload.get('obv')}")
            print(f"- OBV Trend: {payload.get('obv_trend')}")
            print(f"- VWAP 20: {payload.get('vwap_20')}")
            print(f"- Price vs VWAP %: {payload.get('price_vs_vwap_pct')}")
            print(f"- Vol-Price Confirmation: {payload.get('vol_price_confirmation')}")
            print(f"- Bid Qty: {payload.get('bid_qty')}")
            print(f"- Ask Qty: {payload.get('ask_qty')}")
            print(f"- Book Imbalance: {payload.get('book_imbalance')}")
            print(f"- Liquidity Zones: {payload.get('liquidity_zones')}")
            print(f"- Buy Wall Price: {payload.get('buy_wall_price')}")
            print(f"- Buy Wall Qty: {payload.get('buy_wall_qty')}")
            print(f"- Sell Wall Price: {payload.get('sell_wall_price')}")
            print(f"- Sell Wall Qty: {payload.get('sell_wall_qty')}")
        if getattr(args, "structure", False):
            print("Structure")
            print(f"- Structure Trend: {payload.get('structure_trend')}")
            print(f"- Range State: {payload.get('range_state')}")
            print(f"- Range High: {payload.get('range_high')}")
            print(f"- Range Low: {payload.get('range_low')}")
            print(f"- Range Width %: {payload.get('range_width_pct')}")
            bos_dir = payload.get("bos_direction") if payload.get("bos") == "True" else "n/a"
            choch_dir = payload.get("choch_direction") if payload.get("choch") == "True" else "n/a"
            print(f"- BOS: {payload.get('bos')} ({bos_dir})")
            print(f"- CHOCH: {payload.get('choch')} ({choch_dir})")
            print(f"- Accumulation/Distribution: {payload.get('accumulation')}")
            print(f"- Price Zone: {payload.get('price_zone')}")
            print(f"- BOS Streak: {payload.get('bos_streak')}")
            print(f"- Last Swing High: {payload.get('last_swing_high')}")
            print(f"- Last Swing Low: {payload.get('last_swing_low')}")
            print(f"- Prev Swing High: {payload.get('prev_swing_high')}")
            print(f"- Prev Swing Low: {payload.get('prev_swing_low')}")
            print(f"- FVG Count: {payload.get('fvg_count')}")
            print(
                f"- Last FVG: {payload.get('last_fvg_direction')} "
                f"[{payload.get('last_fvg_low')}, {payload.get('last_fvg_high')}] "
                f"mitigated={payload.get('last_fvg_mitigated')}"
            )
        if getattr(args, "execution", False):
            print("Execution Summary")
            if payload.get("execution_available") == "True":
                print(
                    f"- Spread: {payload.get('exec_spread_abs')} "
                    f"({payload.get('exec_spread_pct')}) → {payload.get('exec_spread_quality')}"
                )
                print(
                    f"- Slippage ({payload.get('exec_notional_used')} USDT {payload.get('exec_side').upper()}): "
                    f"{payload.get('exec_slippage_pct')}"
                )
                depth_state = payload.get("exec_depth_imbalance")
                print(f"- Market Depth: imbalance={depth_state}")
                print("Execution Details")
                print(f"- Mid Price: {payload.get('exec_mid')}")
                print(f"- Best Bid / Ask: {payload.get('exec_best_bid')} / {payload.get('exec_best_ask')}")
                print("Spread")
                print(f"- Absolute: {payload.get('exec_spread_abs')}")
                print(f"- Relative: {payload.get('exec_spread_pct')}")
                print(f"- Quality: {payload.get('exec_spread_quality')}")
                print("Slippage")
                print("- Model: depth-based")
                print(f"- Notional: {payload.get('exec_notional_used')} USDT")
                print(f"- Avg Fill Price: {payload.get('exec_avg_fill_price')}")
                print(f"- Slippage %: {payload.get('exec_slippage_pct')}")
                print(f"- Effective Spread %: {payload.get('exec_effective_spread_pct')}")
                print(f"- Market Impact %: {payload.get('exec_market_impact_pct')}")
                print(f"- Levels Used: {payload.get('exec_levels_used')}")
                print(f"- Fill Ratio %: {payload.get('exec_fill_ratio_pct')}")
                print("Market Depth")
                print(f"- Top Levels: {payload.get('exec_depth_levels')}")
                print(f"- Bid Depth: {payload.get('exec_bid_depth_notional')}")
                print(f"- Ask Depth: {payload.get('exec_ask_depth_notional')}")
                print(f"- Notional Available: {payload.get('exec_notional_available')}")
                print(f"- Imbalance: {payload.get('exec_depth_imbalance')}")
                print(f"- Depth Spread: {payload.get('exec_depth_spread_pct')}")
            else:
                reason = payload.get("execution_unavailable_reason") or "unavailable"
                print(f"- Execution: unavailable ({reason})")
        if getattr(args, "risk", False):
            print("Risk Summary")
            if payload.get("risk_available") == "True":
                print(f"- Viable: {payload.get('risk_viable')}")
                print(f"- Risk Score: {payload.get('risk_score')}")
                print(f"- Stop Loss: {payload.get('risk_stop_price')} ({payload.get('risk_stop_method')})")
                print(f"- R:R (to TP2): {payload.get('risk_reward_risk_ratio')}")
                print(f"- Position Size: {payload.get('risk_position_size_quote')} ({payload.get('risk_position_size_pct')}%)")
                print("Risk Details")
                print(f"- Entry: {payload.get('risk_entry_price')} ({payload.get('risk_side')})")
                print(f"- Effective Entry: {payload.get('risk_effective_entry')}")
                print(f"- Stop Distance %: {payload.get('risk_stop_distance_pct')}")
                print(f"- Stop ATR Multiple: {payload.get('risk_stop_atr_multiple')}")
                print(f"- Stop Candidates: {payload.get('risk_stop_candidates')}")
                print(f"- TP1: {payload.get('risk_tp1')}")
                print(f"- TP2: {payload.get('risk_tp2')}")
                print(f"- TP3: {payload.get('risk_tp3')}")
                print(f"- TP FVG: {payload.get('risk_tp_fvg')}")
                print(f"- TP Structure: {payload.get('risk_tp_structure')}")
                print(f"- TP Cloud: {payload.get('risk_tp_cloud')}")
                print(f"- Max Loss Quote: {payload.get('risk_max_loss_quote')}")
                print(f"- Risk % Used: {payload.get('risk_pct_used')}")
                print(f"- Caps Applied: {payload.get('risk_caps_applied')}")
                print(f"- Suggested Leverage: {payload.get('risk_suggested_leverage')}")
                print(f"- Liquidation Price: {payload.get('risk_liquidation_price')}")
                print(f"- Liquidation Distance %: {payload.get('risk_liquidation_distance_pct')}")
                print(
                    f"- Flags: wide_stop={payload.get('risk_flag_wide_stop')} "
                    f"concentration_cap={payload.get('risk_flag_concentration_cap')} "
                    f"liquidity_cap={payload.get('risk_flag_liquidity_cap')} "
                    f"liquidation_warning={payload.get('risk_flag_liquidation_warning')} "
                    f"low_adx_warning={payload.get('risk_flag_low_adx_warning')}"
                )
            else:
                reason = payload.get("risk_rejection_reason") or "unavailable"
                print(f"- Risk: unavailable ({reason})")
        if getattr(args, "quant", False):
            if payload.get("quant_available") == "True":
                print("Quant Summary")
                print(f"- Correlation vs {payload.get('quant_benchmark')}: {payload.get('quant_corr')}")
                print(f"- Return Z-Score: {payload.get('quant_return_zscore')}")
                print(f"- Volatility Regime: {payload.get('quant_vol_regime')}")
                print(f"- Mean Reversion State: {payload.get('quant_mean_reversion_state')}")
                print("Quant Details")
                print(f"- Window: {payload.get('quant_window')} bars")
                print(f"- Correlation Method: {payload.get('quant_corr_method')}")
                print("- Series: log returns")
                print(f"- Realized Volatility: {payload.get('quant_realized_vol')}")
                print(f"- Rolling Mean Deviation %: {payload.get('quant_mean_dev_pct')}")
                print(f"- Max Drawdown %: {payload.get('quant_max_drawdown_pct')}")
                print(f"- Beta: {payload.get('quant_beta')}")
                print(f"- Sharpe Ratio: {payload.get('quant_sharpe_ratio')}")
                print(f"- Calmar Ratio: {payload.get('quant_calmar_ratio')}")
                print(f"- Skewness: {payload.get('quant_skewness')}")
                print(f"- Kurtosis: {payload.get('quant_kurtosis')}")
                print(f"- Log Return: {payload.get('quant_log_return')}")
                print(f"- Return Mean: {payload.get('quant_ret_mean')}")
                print(f"- Return Std: {payload.get('quant_ret_std')}")
                print(f"- Price vs EMA %: {payload.get('quant_price_vs_ema_pct')}")
                print(f"- RSI: {payload.get('quant_rsi')}")
                print(f"- MACD Hist: {payload.get('quant_macd_hist')}")
                print(f"- ATR Norm: {payload.get('quant_atr_norm')}")
                print(f"- Volume Z-Score: {payload.get('quant_volume_zscore')}")
                print(f"- Spread %: {payload.get('quant_spread_pct')}")
                print(f"- Range %: {payload.get('quant_range_pct')}")
            else:
                reason = payload.get("quant_unavailable_reason") or "unavailable"
                print(f"- Quant: unavailable ({reason})")
        if getattr(args, "crypto", False):
            print("Crypto")
            print(f"- Futures Market: {payload.get('futures_market')}")
            print(f"- Funding Rate: {payload.get('funding_rate')}")
            print(f"- Next Funding Time: {payload.get('next_funding_time')}")
            print(f"- Open Interest: {payload.get('open_interest')}")
        if cache_ttl_s > 0:
            print(f"- Cache: {'hit' if cache_hit else 'miss'}")

    if getattr(args, "save_snapshot", False) and not cache_hit:
        with connect(db_path) as conn:
            state = StateManager(conn)
            indicators = {
                "momentum_pct": str(payload.get("momentum_pct")),
                "volatility_pct": str(payload.get("volatility_pct")),
            }
            if getattr(args, "momentum", False):
                indicators.update(
                    {
                        "rsi": payload.get("rsi"),
                        "rsi_prev": payload.get("rsi_prev"),
                        "rsi_zone": payload.get("rsi_zone"),
                        "macd": payload.get("macd"),
                        "macd_signal": payload.get("macd_signal"),
                        "macd_hist": payload.get("macd_hist"),
                        "stoch_rsi": payload.get("stoch_rsi"),
                        "stoch_rsi_k": payload.get("stoch_rsi_k"),
                        "stoch_rsi_d": payload.get("stoch_rsi_d"),
                        "stoch_rsi_bias": payload.get("stoch_rsi_bias"),
                        "macd_bias": payload.get("macd_bias"),
                        "williams_r": payload.get("williams_r"),
                        "williams_r_zone": payload.get("williams_r_zone"),
                        "cci": payload.get("cci"),
                        "cci_zone": payload.get("cci_zone"),
                        "roc": payload.get("roc"),
                        "roc_bias": payload.get("roc_bias"),
                        "composite_signal": payload.get("composite_signal"),
                        "rsi_bullish_divergence": payload.get("rsi_bullish_divergence"),
                        "rsi_bearish_divergence": payload.get("rsi_bearish_divergence"),
                    }
                )
            if getattr(args, "trend", False):
                indicators.update(
                    {
                        "ema_20": payload.get("ema_20"),
                        "ema_50": payload.get("ema_50"),
                        "ema_200": payload.get("ema_200"),
                        "sma_20": payload.get("sma_20"),
                        "sma_50": payload.get("sma_50"),
                        "sma_200": payload.get("sma_200"),
                        "trend_crossover": payload.get("trend_crossover"),
                        "trend_crossover_event": payload.get("trend_crossover_event"),
                        "trend_crossover_strength_pct": payload.get("trend_crossover_strength_pct"),
                        "ema_50_200_crossover": payload.get("ema_50_200_crossover"),
                        "ema_50_200_event": payload.get("ema_50_200_event"),
                        "ema_50_200_strength_pct": payload.get("ema_50_200_strength_pct"),
                        "sma_20_50_crossover": payload.get("sma_20_50_crossover"),
                        "sma_20_50_event": payload.get("sma_20_50_event"),
                        "sma_50_200_crossover": payload.get("sma_50_200_crossover"),
                        "sma_50_200_event": payload.get("sma_50_200_event"),
                        "adx": payload.get("adx"),
                        "adx_pos": payload.get("adx_pos"),
                        "adx_neg": payload.get("adx_neg"),
                        "adx_trend_strength": payload.get("adx_trend_strength"),
                        "ichi_tenkan": payload.get("ichi_tenkan"),
                        "ichi_kijun": payload.get("ichi_kijun"),
                        "ichi_senkou_a": payload.get("ichi_senkou_a"),
                        "ichi_senkou_b": payload.get("ichi_senkou_b"),
                        "ichi_cloud_bias": payload.get("ichi_cloud_bias"),
                        "price_vs_ema20_pct": payload.get("price_vs_ema20_pct"),
                        "price_vs_ema50_pct": payload.get("price_vs_ema50_pct"),
                        "price_vs_ema200_pct": payload.get("price_vs_ema200_pct"),
                        "trend_bias": payload.get("trend_bias"),
                    }
                )
            if getattr(args, "volatility", False):
                indicators.update(
                    {
                        "atr": payload.get("atr"),
                        "atr_pct": payload.get("atr_pct"),
                        "bb_upper": payload.get("bb_upper"),
                        "bb_mid": payload.get("bb_mid"),
                        "bb_lower": payload.get("bb_lower"),
                        "bb_width_pct": payload.get("bb_width_pct"),
                        "bb_pct_b": payload.get("bb_pct_b"),
                        "bb_position": payload.get("bb_position"),
                        "kc_upper": payload.get("kc_upper"),
                        "kc_lower": payload.get("kc_lower"),
                        "squeeze": payload.get("squeeze"),
                        "hist_vol_pct": payload.get("hist_vol_pct"),
                        "chandelier_long": payload.get("chandelier_long"),
                        "chandelier_short": payload.get("chandelier_short"),
                        "vol_regime": payload.get("vol_regime"),
                    }
                )
            if getattr(args, "volume", False):
                indicators.update(
                    {
                        "volume_base_last": payload.get("volume_base_last"),
                        "volume_quote_last": payload.get("volume_quote_last"),
                        "volume_quote_avg_20": payload.get("volume_quote_avg_20"),
                        "volume_quote_avg_50": payload.get("volume_quote_avg_50"),
                        "volume_quote_std_20": payload.get("volume_quote_std_20"),
                        "volume_quote_zscore_20": payload.get("volume_quote_zscore_20"),
                        "volume_quote_trend": payload.get("volume_quote_trend"),
                        "volume_spike": payload.get("volume_spike"),
                        "taker_buy_ratio": payload.get("taker_buy_ratio"),
                        "taker_buy_ratio_avg20": payload.get("taker_buy_ratio_avg20"),
                        "buy_pressure": payload.get("buy_pressure"),
                        "sustained_buy_pressure": payload.get("sustained_buy_pressure"),
                        "sustained_sell_pressure": payload.get("sustained_sell_pressure"),
                        "obv": payload.get("obv"),
                        "obv_trend": payload.get("obv_trend"),
                        "vwap_20": payload.get("vwap_20"),
                        "price_vs_vwap_pct": payload.get("price_vs_vwap_pct"),
                        "vol_price_confirmation": payload.get("vol_price_confirmation"),
                        "bid_qty": payload.get("bid_qty"),
                        "ask_qty": payload.get("ask_qty"),
                        "book_imbalance": payload.get("book_imbalance"),
                        "liquidity_zones": payload.get("liquidity_zones"),
                        "buy_wall_price": payload.get("buy_wall_price"),
                        "buy_wall_qty": payload.get("buy_wall_qty"),
                        "sell_wall_price": payload.get("sell_wall_price"),
                        "sell_wall_qty": payload.get("sell_wall_qty"),
                    }
                )
            if getattr(args, "structure", False):
                indicators.update(
                    {
                        "structure_trend": payload.get("structure_trend"),
                        "range_state": payload.get("range_state"),
                        "range_high": payload.get("range_high"),
                        "range_low": payload.get("range_low"),
                        "range_width_pct": payload.get("range_width_pct"),
                        "bos": payload.get("bos"),
                        "bos_direction": payload.get("bos_direction"),
                        "bos_streak": payload.get("bos_streak"),
                        "choch": payload.get("choch"),
                        "choch_direction": payload.get("choch_direction"),
                        "accumulation": payload.get("accumulation"),
                        "last_swing_high": payload.get("last_swing_high"),
                        "last_swing_low": payload.get("last_swing_low"),
                        "prev_swing_high": payload.get("prev_swing_high"),
                        "prev_swing_low": payload.get("prev_swing_low"),
                        "price_zone": payload.get("price_zone"),
                        "fvg_count": payload.get("fvg_count"),
                        "last_fvg_direction": payload.get("last_fvg_direction"),
                        "last_fvg_high": payload.get("last_fvg_high"),
                        "last_fvg_low": payload.get("last_fvg_low"),
                        "last_fvg_mitigated": payload.get("last_fvg_mitigated"),
                    }
                )
            if getattr(args, "price_action", False):
                indicators.update(
                    {
                        "price_action_available": payload.get("price_action_available"),
                        "pa_unavailable_reason": payload.get("pa_unavailable_reason"),
                        "pa_support_level": payload.get("pa_support_level"),
                        "pa_support_strength": payload.get("pa_support_strength"),
                        "pa_support_distance_pct": payload.get("pa_support_distance_pct"),
                        "pa_resistance_level": payload.get("pa_resistance_level"),
                        "pa_resistance_strength": payload.get("pa_resistance_strength"),
                        "pa_resistance_distance_pct": payload.get("pa_resistance_distance_pct"),
                        "pa_structure_type": payload.get("pa_structure_type"),
                        "pa_last_swing_high": payload.get("pa_last_swing_high"),
                        "pa_last_swing_low": payload.get("pa_last_swing_low"),
                        "pa_breakout": payload.get("pa_breakout"),
                        "pa_breakdown": payload.get("pa_breakdown"),
                        "pa_breakout_level": payload.get("pa_breakout_level"),
                        "pa_breakdown_level": payload.get("pa_breakdown_level"),
                        "pa_breakout_strength": payload.get("pa_breakout_strength"),
                        "pa_patterns_json": payload.get("pa_patterns_json"),
                        "pa_last_pattern": payload.get("pa_last_pattern"),
                        "pa_dominant_bias": payload.get("pa_dominant_bias"),
                        "pa_signal_count": payload.get("pa_signal_count"),
                        "pa_confluence": payload.get("pa_confluence"),
                    }
                )
            if getattr(args, "execution", False):
                indicators.update(
                    {
                        "execution_available": payload.get("execution_available"),
                        "execution_unavailable_reason": payload.get("execution_unavailable_reason"),
                        "exec_mid": payload.get("exec_mid"),
                        "exec_best_bid": payload.get("exec_best_bid"),
                        "exec_best_ask": payload.get("exec_best_ask"),
                        "exec_spread_abs": payload.get("exec_spread_abs"),
                        "exec_spread_pct": payload.get("exec_spread_pct"),
                        "exec_spread_quality": payload.get("exec_spread_quality"),
                        "exec_slippage_pct": payload.get("exec_slippage_pct"),
                        "exec_effective_spread_pct": payload.get("exec_effective_spread_pct"),
                        "exec_market_impact_pct": payload.get("exec_market_impact_pct"),
                        "exec_avg_fill_price": payload.get("exec_avg_fill_price"),
                        "exec_notional_used": payload.get("exec_notional_used"),
                        "exec_fill_ratio_pct": payload.get("exec_fill_ratio_pct"),
                        "exec_levels_used": payload.get("exec_levels_used"),
                        "exec_side": payload.get("exec_side"),
                        "exec_depth_levels": payload.get("exec_depth_levels"),
                        "exec_bid_depth_notional": payload.get("exec_bid_depth_notional"),
                        "exec_ask_depth_notional": payload.get("exec_ask_depth_notional"),
                        "exec_notional_available": payload.get("exec_notional_available"),
                        "exec_depth_imbalance": payload.get("exec_depth_imbalance"),
                        "exec_depth_spread_pct": payload.get("exec_depth_spread_pct"),
                    }
                )
            if getattr(args, "risk", False):
                indicators.update(
                    {
                        "risk_available": payload.get("risk_available"),
                        "risk_viable": payload.get("risk_viable"),
                        "risk_rejection_reason": payload.get("risk_rejection_reason"),
                        "risk_score": payload.get("risk_score"),
                        "risk_score_breakdown": payload.get("risk_score_breakdown"),
                        "risk_entry_price": payload.get("risk_entry_price"),
                        "risk_side": payload.get("risk_side"),
                        "risk_effective_entry": payload.get("risk_effective_entry"),
                        "risk_stop_price": payload.get("risk_stop_price"),
                        "risk_stop_method": payload.get("risk_stop_method"),
                        "risk_stop_distance_pct": payload.get("risk_stop_distance_pct"),
                        "risk_stop_atr_multiple": payload.get("risk_stop_atr_multiple"),
                        "risk_stop_candidates": payload.get("risk_stop_candidates"),
                        "risk_tp1": payload.get("risk_tp1"),
                        "risk_tp2": payload.get("risk_tp2"),
                        "risk_tp3": payload.get("risk_tp3"),
                        "risk_tp_fvg": payload.get("risk_tp_fvg"),
                        "risk_tp_structure": payload.get("risk_tp_structure"),
                        "risk_tp_cloud": payload.get("risk_tp_cloud"),
                        "risk_reward_risk_ratio": payload.get("risk_reward_risk_ratio"),
                        "risk_position_size_base": payload.get("risk_position_size_base"),
                        "risk_position_size_quote": payload.get("risk_position_size_quote"),
                        "risk_position_size_pct": payload.get("risk_position_size_pct"),
                        "risk_max_loss_quote": payload.get("risk_max_loss_quote"),
                        "risk_pct_used": payload.get("risk_pct_used"),
                        "risk_caps_applied": payload.get("risk_caps_applied"),
                        "risk_suggested_leverage": payload.get("risk_suggested_leverage"),
                        "risk_liquidation_price": payload.get("risk_liquidation_price"),
                        "risk_liquidation_distance_pct": payload.get("risk_liquidation_distance_pct"),
                        "risk_flag_wide_stop": payload.get("risk_flag_wide_stop"),
                        "risk_flag_concentration_cap": payload.get("risk_flag_concentration_cap"),
                        "risk_flag_liquidity_cap": payload.get("risk_flag_liquidity_cap"),
                        "risk_flag_liquidation_warning": payload.get("risk_flag_liquidation_warning"),
                        "risk_flag_low_adx_warning": payload.get("risk_flag_low_adx_warning"),
                        "price_action_available": payload.get("price_action_available"),
                        "pa_unavailable_reason": payload.get("pa_unavailable_reason"),
                        "pa_support_level": payload.get("pa_support_level"),
                        "pa_support_strength": payload.get("pa_support_strength"),
                        "pa_support_distance_pct": payload.get("pa_support_distance_pct"),
                        "pa_resistance_level": payload.get("pa_resistance_level"),
                        "pa_resistance_strength": payload.get("pa_resistance_strength"),
                        "pa_resistance_distance_pct": payload.get("pa_resistance_distance_pct"),
                        "pa_structure_type": payload.get("pa_structure_type"),
                        "pa_last_swing_high": payload.get("pa_last_swing_high"),
                        "pa_last_swing_low": payload.get("pa_last_swing_low"),
                        "pa_breakout": payload.get("pa_breakout"),
                        "pa_breakdown": payload.get("pa_breakdown"),
                        "pa_breakout_level": payload.get("pa_breakout_level"),
                        "pa_breakdown_level": payload.get("pa_breakdown_level"),
                        "pa_breakout_strength": payload.get("pa_breakout_strength"),
                        "pa_patterns_json": payload.get("pa_patterns_json"),
                        "pa_last_pattern": payload.get("pa_last_pattern"),
                        "pa_dominant_bias": payload.get("pa_dominant_bias"),
                        "pa_signal_count": payload.get("pa_signal_count"),
                        "pa_confluence": payload.get("pa_confluence"),
                    }
                )
            if getattr(args, "quant", False):
                indicators.update(
                    {
                        "quant_available": payload.get("quant_available"),
                        "quant_unavailable_reason": payload.get("quant_unavailable_reason"),
                        "quant_window": payload.get("quant_window"),
                        "quant_benchmark": payload.get("quant_benchmark"),
                        "quant_corr_method": payload.get("quant_corr_method"),
                        "quant_corr": payload.get("quant_corr"),
                        "quant_beta": payload.get("quant_beta"),
                        "quant_return_zscore": payload.get("quant_return_zscore"),
                        "quant_realized_vol": payload.get("quant_realized_vol"),
                        "quant_vol_regime": payload.get("quant_vol_regime"),
                        "quant_mean_dev_pct": payload.get("quant_mean_dev_pct"),
                        "quant_mean_reversion_state": payload.get("quant_mean_reversion_state"),
                        "quant_max_drawdown_pct": payload.get("quant_max_drawdown_pct"),
                        "quant_sharpe_ratio": payload.get("quant_sharpe_ratio"),
                        "quant_calmar_ratio": payload.get("quant_calmar_ratio"),
                        "quant_skewness": payload.get("quant_skewness"),
                        "quant_kurtosis": payload.get("quant_kurtosis"),
                        "quant_log_return": payload.get("quant_log_return"),
                        "quant_ret_mean": payload.get("quant_ret_mean"),
                        "quant_ret_std": payload.get("quant_ret_std"),
                        "quant_price_vs_ema_pct": payload.get("quant_price_vs_ema_pct"),
                        "quant_rsi": payload.get("quant_rsi"),
                        "quant_macd_hist": payload.get("quant_macd_hist"),
                        "quant_atr_norm": payload.get("quant_atr_norm"),
                        "quant_volume_zscore": payload.get("quant_volume_zscore"),
                        "quant_spread_pct": payload.get("quant_spread_pct"),
                        "quant_range_pct": payload.get("quant_range_pct"),
                    }
                )
            if getattr(args, "crypto", False):
                indicators.update(
                    {
                        "futures_market": payload.get("futures_market"),
                        "funding_rate": payload.get("funding_rate"),
                        "next_funding_time": payload.get("next_funding_time"),
                        "open_interest": payload.get("open_interest"),
                    }
                )
            if payload.get("candle_count") is not None:
                indicators["candle_count"] = payload.get("candle_count")
            state.create_market_snapshot(
                symbol=symbol,
                timeframe=timeframe,
                captured_at_utc=utcnow_iso(),
                last_price=str(payload.get("last_price")),
                bid=str(payload.get("bid")) if payload.get("bid") is not None else None,
                ask=str(payload.get("ask")) if payload.get("ask") is not None else None,
                spread_pct=str(payload.get("spread_pct")) if payload.get("spread_pct") is not None else None,
                change_percent=str(change_pct) if change_pct is not None else None,
                volume_quote=str(volume_24h) if volume_24h is not None else None,
                indicators_json=_json.dumps(indicators, separators=(",", ":")),
                condition_summary=cond,
                enabled_flags="basic"
                + (",momentum" if getattr(args, "momentum", False) else "")
                + (",trend" if getattr(args, "trend", False) else "")
                + (",volatility" if getattr(args, "volatility", False) else "")
                + (",volume" if getattr(args, "volume", False) else "")
                + (",structure" if getattr(args, "structure", False) else "")
                + (",price_action" if getattr(args, "price_action", False) else "")
                + (",execution" if getattr(args, "execution", False) else "")
                + (",risk" if getattr(args, "risk", False) else "")
                + (",quant" if getattr(args, "quant", False) else "")
                + (",crypto" if getattr(args, "crypto", False) else ""),
                config_hash=None,
            )
    return 0


def cmd_market_snapshot_list(args: argparse.Namespace) -> int:
    limit = int(getattr(args, "limit", 50))
    symbol = str(getattr(args, "symbol", "") or "").strip().upper()
    timeframe = str(getattr(args, "timeframe", "") or "").strip()
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)
    with connect(db_path) as conn:
        state = StateManager(conn)
        rows = state.list_market_snapshots(
            limit=limit,
            symbol=symbol or None,
            timeframe=timeframe or None,
        )
    print(f"Market snapshots: {len(rows)}")
    if not rows:
        return 0
    print("ID  SYMBOL     TF   LAST_PRICE      COND      FLAGS                AT_UTC")
    for r in rows:
        print(
            f"{str(r.get('id') or ''):>3} "
            f"{str(r.get('symbol') or ''):<9} "
            f"{str(r.get('timeframe') or ''):<4} "
            f"{str(r.get('last_price') or ''):<13} "
            f"{str(r.get('condition_summary') or ''):<9} "
            f"{str(r.get('enabled_flags') or ''):<20} "
            f"{str(r.get('captured_at_utc') or '')}"
        )
    return 0


def cmd_market_snapshot_show(args: argparse.Namespace) -> int:
    snap_id = getattr(args, "id", None)
    if not snap_id:
        print("Missing snapshot id")
        return 2
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)
    with connect(db_path) as conn:
        state = StateManager(conn)
        row = state.get_market_snapshot(snapshot_id=int(snap_id))
    if not row:
        print("(not found)")
        return 0
    print("Market Snapshot")
    for k in (
        "id",
        "symbol",
        "timeframe",
        "captured_at_utc",
        "last_price",
        "bid",
        "ask",
        "spread_pct",
        "change_percent",
        "volume_quote",
        "condition_summary",
        "enabled_flags",
        "config_hash",
    ):
        print(f"- {k}: {row.get(k)}")
    indicators = None
    try:
        if row.get("indicators_json"):
            indicators = _json.loads(str(row.get("indicators_json")))
    except Exception:
        indicators = None
    if indicators:
        print("Indicators")
        for k, v in indicators.items():
            print(f"- {k}: {v}")
    return 0


def _monitor_once_core(args: argparse.Namespace) -> tuple[int, int, int, int, list[int], list[int]]:
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    cfg = load_config(config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)

    limit = int(getattr(args, "limit", 50))
    position_id = getattr(args, "position_id", None)
    verbose = bool(getattr(args, "verbose", False))
    checked = 0
    exit_recommended = 0
    reevaluate = 0
    paused = 0
    checked_positions: list[int] = []
    failed_positions: list[int] = []

    with connect(db_path) as conn:
        state = StateManager(conn)
        if position_id not in (None, ""):
            pos = state.get_position(position_id=int(position_id))
            if not pos:
                print("(not found)")
                return (0, 0, 0, 0, [], [])
            if str(pos.get("status") or "").upper() != "OPEN":
                print("Rejected: position is not OPEN")
                return (0, 0, 0, 0, [], [])
            positions = [pos]
        else:
            positions = state.list_positions(status="OPEN", limit=limit)
        if not positions:
            print("(no open positions)")
            return (0, 0, 0, 0, [], [])

        for pos in positions:
            checked += 1
            pos_id = int(pos.get("id") or 0)
            checked_positions.append(pos_id)
            symbol = str(pos.get("symbol") or "").strip().upper()
            md_env = str(pos.get("market_data_environment") or "mainnet_public")

            # Skip monitoring for paused symbols.
            try:
                if state.is_symbol_paused(symbol=symbol):
                    paused += 1
                    if verbose:
                        print(f"pos_id={pos_id} symbol={symbol} decision=data_unavailable reason=symbol_paused")
                    state.create_monitoring_event(
                        position_id=pos_id,
                        symbol=symbol,
                        entry_price=str(pos.get("entry_price") or "") or None,
                        current_price=None,
                        pnl_percent=None,
                        decision="data_unavailable",
                        exit_reason="symbol_paused",
                        deadline_utc=str(pos.get("deadline_utc") or "") or None,
                        position_status=str(pos.get("status") or ""),
                        error_code="symbol_paused",
                        error_message="symbol paused by reliability policy",
                    )
                    continue
            except Exception:
                pass

            try:
                client = _price_client_for_market_env(
                    cfg=cfg,
                    market_env=md_env,
                    ca_bundle=getattr(args, "ca_bundle", None),
                    insecure=bool(getattr(args, "insecure", False)),
                )

                # Prefer server time for deadline comparisons.
                now_dt = datetime.now(UTC)
                try:
                    ms = client.get_server_time_ms()
                    now_dt = datetime.fromtimestamp(ms / 1000.0, tz=UTC)
                except Exception:
                    pass

                current_price = _d_position(client.get_ticker_price(symbol=symbol), "current_price")
                entry_price = _d_position(pos.get("entry_price"), "entry_price")
                qty = _d_position(pos.get("quantity"), "net_position_qty")

                market_value = current_price * qty
                cost_basis = entry_price * qty
                unrealized = market_value - cost_basis
                pnl_pct = (unrealized / cost_basis * Decimal("100")) if cost_basis > 0 else Decimal("0")

                decision = "hold"
                exit_reason = None

                # Deadline exit
                deadline_utc_s = str(pos.get("deadline_utc") or "").strip()
                if deadline_utc_s:
                    try:
                        deadline_dt = datetime.fromisoformat(deadline_utc_s.replace("Z", "+00:00")).astimezone(UTC)
                        if now_dt >= deadline_dt:
                            decision = "exit_recommended"
                            exit_reason = "deadline_reached"
                    except Exception:
                        pass

                # Stop-loss / take-profit (only if not already exit-triggered)
                if decision == "hold":
                    try:
                        sl = _d_position(pos.get("stop_loss_price"), "stop_loss_price")
                        if sl > 0 and current_price <= sl:
                            decision = "exit_recommended"
                            exit_reason = "stop_loss_hit"
                    except Exception:
                        pass
                if decision == "hold":
                    try:
                        tp = _d_position(pos.get("profit_target_price"), "profit_target_price")
                        if tp > 0 and current_price >= tp:
                            decision = "exit_recommended"
                            exit_reason = "target_reached"
                    except Exception:
                        pass

                # Re-evaluation triggers (only if not exit)
                if decision == "hold":
                    # Soft deadline (within 10% of remaining time or 10 minutes, whichever is larger).
                    try:
                        opened_s = str(pos.get("opened_at_utc") or "").strip()
                        if deadline_utc_s and opened_s:
                            opened_dt = datetime.fromisoformat(opened_s.replace("Z", "+00:00")).astimezone(UTC)
                            deadline_dt = datetime.fromisoformat(deadline_utc_s.replace("Z", "+00:00")).astimezone(UTC)
                            total = (deadline_dt - opened_dt).total_seconds()
                            remaining = (deadline_dt - now_dt).total_seconds()
                            if total > 0 and remaining > 0:
                                soft_window = max(600.0, total * 0.10)
                                if remaining <= soft_window:
                                    decision = "reevaluate"
                                    exit_reason = "soft_deadline_reached"
                    except Exception:
                        pass

                if decision == "hold":
                    # Profit threshold near target (>= 90% of target price).
                    try:
                        tp = _d_position(pos.get("profit_target_price"), "profit_target_price")
                        if tp > 0 and current_price >= (tp * Decimal("0.90")):
                            decision = "reevaluate"
                            exit_reason = "profit_threshold_reached"
                    except Exception:
                        pass

                if decision == "hold":
                    # Drawdown warning near stop-loss (<= 110% of stop-loss price).
                    try:
                        sl = _d_position(pos.get("stop_loss_price"), "stop_loss_price")
                        if sl > 0 and current_price <= (sl * Decimal("1.10")):
                            decision = "reevaluate"
                            exit_reason = "drawdown_warning"
                    except Exception:
                        pass

                if decision == "exit_recommended":
                    exit_recommended += 1
                elif decision == "reevaluate":
                    reevaluate += 1

                if verbose:
                    print(
                        f"pos_id={pos_id} symbol={symbol} md_env={md_env} price={current_price} "
                        f"qty={qty} entry={entry_price} pnl_pct={pnl_pct} decision={decision}"
                    )

                state.create_monitoring_event(
                    position_id=pos_id,
                    symbol=symbol,
                    entry_price=str(entry_price),
                    current_price=str(current_price),
                    pnl_percent=str(pnl_pct),
                    decision=decision,
                    exit_reason=exit_reason,
                    deadline_utc=deadline_utc_s or None,
                    position_status=str(pos.get("status") or ""),
                    error_code=None,
                    error_message=None,
                )
                state.update_position_last_monitored(position_id=pos_id, at_utc=utcnow_iso())
            except Exception as e:
                paused += 1
                failed_positions.append(pos_id)
                if verbose:
                    print(f"pos_id={pos_id} symbol={symbol} md_env={md_env} decision=data_unavailable error={e}")
                state.create_monitoring_event(
                    position_id=pos_id,
                    symbol=symbol,
                    entry_price=str(pos.get("entry_price") or "") or None,
                    current_price=None,
                    pnl_percent=None,
                    decision="data_unavailable",
                    exit_reason="monitoring_fetch_failed",
                    deadline_utc=str(pos.get("deadline_utc") or "") or None,
                    position_status=str(pos.get("status") or ""),
                    error_code="monitoring_fetch_failed",
                    error_message=str(e),
                )

    return (checked, exit_recommended, reevaluate, paused, checked_positions, failed_positions)


def cmd_monitor_once(args: argparse.Namespace) -> int:
    checked, exit_recommended, reevaluate, paused, _, _ = _monitor_once_core(args)
    if checked == 0 and exit_recommended == 0 and reevaluate == 0 and paused == 0:
        return 0
    print(f"OK checked={checked} exit_recommended={exit_recommended} reevaluate={reevaluate} paused={paused}")
    return 0


def _resolve_monitor_interval_seconds(*, args: argparse.Namespace, cfg: AppConfig) -> tuple[int, str]:
    fallback = 60
    cli_val = getattr(args, "interval_seconds", None)
    if cli_val not in (None, ""):
        try:
            v = int(cli_val)
            if v > 0:
                return v, "cli"
        except Exception:
            pass
    cfg_val = getattr(cfg, "trading_monitoring_interval_seconds", None)
    try:
        if cfg_val is not None and int(cfg_val) > 0:
            return int(cfg_val), "config"
    except Exception:
        pass
    return fallback, "fallback"


def cmd_monitor_loop(args: argparse.Namespace) -> int:
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    cfg = load_config(config_path)

    interval, source = _resolve_monitor_interval_seconds(args=args, cfg=cfg)
    duration = getattr(args, "duration_seconds", None)
    if interval <= 0:
        print("Invalid interval_seconds")
        return 2
    end_at = None
    if duration not in (None, ""):
        try:
            duration_s = int(duration)
        except Exception:
            print("Invalid duration_seconds")
            return 2
        if duration_s <= 0:
            print("Invalid duration_seconds")
            return 2
        end_at = time.monotonic() + duration_s

    extra = f", duration_seconds={int(duration)}" if end_at is not None else ""
    print(f"Monitoring loop started (interval_seconds={interval}, source={source}{extra}). Ctrl-C to stop.")

    consecutive_failures = 0
    try:
        while True:
            try:
                with connect(ensure_db_initialized(config_path=config_path, db_path=paths.db_path)) as conn:
                    sys_state = StateManager(conn).get_system_state() or {}
                    if bool(sys_state.get("automation_paused") or 0):
                        print("Monitoring paused (global automation_paused=true).")
                        return 0
            except Exception:
                pass
            if end_at is not None and time.monotonic() >= end_at:
                break
            checked, exit_recommended, reevaluate, paused, checked_positions, failed_positions = _monitor_once_core(args)
            if checked == 0 and exit_recommended == 0 and reevaluate == 0 and paused == 0:
                # No positions to monitor.
                time.sleep(interval)
                continue

            had_failure = len(failed_positions) > 0
            if had_failure:
                consecutive_failures += 1
                multiplier = 2 if consecutive_failures == 1 else 5
                next_delay = interval * multiplier
                print(f"monitoring_fetch_failed count={consecutive_failures} backoff_multiplier={multiplier} next_retry={next_delay}s")
                # Persist backoff event for failed positions.
                try:
                    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)
                    with connect(db_path) as conn:
                        state = StateManager(conn)
                        for pid in failed_positions:
                            try:
                                pos = state.get_position(position_id=int(pid))
                                if not pos:
                                    continue
                                state.create_monitoring_event(
                                    position_id=int(pid),
                                    symbol=str(pos.get("symbol") or "").strip().upper(),
                                    entry_price=str(pos.get("entry_price") or "") or None,
                                    current_price=None,
                                    pnl_percent=None,
                                    decision="data_unavailable",
                                    exit_reason="monitoring_backoff_applied",
                                    deadline_utc=str(pos.get("deadline_utc") or "") or None,
                                    position_status=str(pos.get("status") or ""),
                                    error_code="monitoring_backoff_applied",
                                    error_message=f"backoff_multiplier={multiplier}",
                                )
                            except Exception:
                                continue
                except Exception:
                    pass
                sleep_s = next_delay
            else:
                if consecutive_failures > 0:
                    print("monitoring_recovered")
                    # Persist recovery event for checked positions.
                    try:
                        db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)
                        with connect(db_path) as conn:
                            state = StateManager(conn)
                            for pid in checked_positions:
                                try:
                                    pos = state.get_position(position_id=int(pid))
                                    if not pos:
                                        continue
                                    state.create_monitoring_event(
                                        position_id=int(pid),
                                        symbol=str(pos.get("symbol") or "").strip().upper(),
                                        entry_price=str(pos.get("entry_price") or "") or None,
                                        current_price=None,
                                        pnl_percent=None,
                                        decision="hold",
                                        exit_reason="monitoring_recovered",
                                        deadline_utc=str(pos.get("deadline_utc") or "") or None,
                                        position_status=str(pos.get("status") or ""),
                                        error_code="monitoring_recovered",
                                        error_message=None,
                                    )
                                except Exception:
                                    continue
                    except Exception:
                        pass
                consecutive_failures = 0
                sleep_s = interval

            if end_at is None:
                time.sleep(sleep_s)
            else:
                remaining = end_at - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(sleep_s, remaining))
    except KeyboardInterrupt:
        print("Stopped")
        return 0
    print("Done")
    return 0


def cmd_monitor_events_list(args: argparse.Namespace) -> int:
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)
    limit = int(getattr(args, "limit", 50))
    with connect(db_path) as conn:
        rows = StateManager(conn).list_monitoring_events(limit=limit)
    if not rows:
        print("(no monitoring events)")
        return 0
    print(f"Monitoring events: {len(rows)}")
    print(f"{'EVT_ID':>6} {'POS_ID':>6} {'SYMBOL':<10} {'DECISION':<18} {'REASON':<20} {'PNL%':<10} {'AT_UTC':<20}")
    for r in rows:
        print(
            f"{int(r.get('monitoring_event_id') or 0):>6} "
            f"{int(r.get('position_id') or 0):>6} "
            f"{str(r.get('symbol') or '-'): <10} "
            f"{str(r.get('decision') or '-'): <18} "
            f"{str(r.get('exit_reason') or '-'): <20} "
            f"{str(r.get('pnl_percent') or '-'): <10} "
            f"{str(r.get('created_at_utc') or '-'): <20}"
        )
    return 0


def cmd_reliability_status(args: argparse.Namespace) -> int:
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)
    with connect(db_path) as conn:
        state = StateManager(conn)
        s = state.get_system_state()
    if not s:
        print("(no system state)")
        return 2
    pauses = []
    try:
        with connect(db_path) as conn:
            pauses = StateManager(conn).list_active_pauses()
    except Exception:
        pauses = []
    print("Reliability Status")
    print(f"- automation_paused: {bool(s.get('automation_paused') or 0)}")
    print(f"- pause_reason: {s.get('pause_reason')}")
    print(f"- paused_at_utc: {s.get('paused_at_utc')}")
    print(f"- last_reconciliation_status: {s.get('last_reconciliation_status')}")
    print(f"- last_successful_sync_time_utc: {s.get('last_successful_sync_time_utc')}")
    if pauses:
        loop_n = len([p for p in pauses if p.get("scope_type") == "loop"])
        sym_n = len([p for p in pauses if p.get("scope_type") == "symbol"])
        print(f"- scoped_pauses: loop={loop_n} symbol={sym_n}")
    return 0


def cmd_reliability_events_list(args: argparse.Namespace) -> int:
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)
    limit = int(getattr(args, "limit", 50))
    with connect(db_path) as conn:
        rows = StateManager(conn).list_reconciliation_events(limit=limit)
    if not rows:
        print("(no reconciliation events)")
        return 0
    print(f"Reconciliation events: {len(rows)}")
    print(f"{'EVT_ID':>6} {'TYPE':<20} {'STATUS':<24} {'AT_UTC':<20} SUMMARY")
    for r in rows:
        summary = str(r.get("summary") or "")
        if len(summary) > 80:
            summary = summary[:77] + "..."
        print(
            f"{int(r.get('reconciliation_event_id') or 0):>6} "
            f"{str(r.get('event_type') or '-'): <20} "
            f"{str(r.get('status') or '-'): <24} "
            f"{str(r.get('created_at_utc') or '-'): <20} "
            f"{summary}"
        )
    return 0


def cmd_reliability_reconcile(args: argparse.Namespace) -> int:
    client = _client_from_args(args)
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)

    def _dec(v: object) -> Decimal:
        try:
            return Decimal(str(v))
        except Exception:
            return Decimal("0")

    with connect(db_path) as conn:
        state = StateManager(conn)
        balances_before = {str(r.get("asset") or "").upper(): r for r in state.list_balances(include_zero=True)}
        open_before = state.list_open_orders()
        open_before_ids = {str(o.get("exchange_order_id") or "") for o in open_before if o.get("exchange_order_id")}
        positions = state.list_positions(status="OPEN", limit=200)

    # Sync from exchange (source of truth).
    with connect(db_path) as conn:
        sync_balances(client=client, conn=conn)
        sync_open_orders(client=client, conn=conn, symbol=None)

    events = 0
    warnings = 0
    critical = 0
    critical_symbols: set[str] = set()
    paused_loops: set[int] = set()

    with connect(db_path) as conn:
        state = StateManager(conn)
        balances_after = {str(r.get("asset") or "").upper(): r for r in state.list_balances(include_zero=True)}
        open_after = state.list_open_orders()
        open_after_ids = {str(o.get("exchange_order_id") or "") for o in open_after if o.get("exchange_order_id")}

        # Balance mismatches (warning only unless critical rules below trigger pause).
        for asset in sorted(set(balances_before.keys()) | set(balances_after.keys())):
            b = balances_before.get(asset, {})
            a = balances_after.get(asset, {})
            before_total = _dec(b.get("free")) + _dec(b.get("locked"))
            after_total = _dec(a.get("free")) + _dec(a.get("locked"))
            if before_total != after_total:
                events += 1
                warnings += 1
                state.create_reconciliation_event(
                    event_type="balance_mismatch",
                    status="warning",
                    summary=f"{asset} {before_total} -> {after_total}",
                    details={"asset": asset, "before": str(before_total), "after": str(after_total)},
                )

        # Unknown open orders (external) - warning only.
        for o in open_after:
            if str(o.get("order_source") or "") == "external":
                events += 1
                warnings += 1
                state.create_reconciliation_event(
                    event_type="unknown_order",
                    status="warning",
                    summary=f"external open order {o.get('exchange_order_id')} {o.get('symbol')}",
                    details={"order_id": o.get("exchange_order_id"), "symbol": o.get("symbol"), "source": "external"},
                )

        # Missing expected orders (were open, now not open) - warning.
        for o in open_before:
            oid = str(o.get("exchange_order_id") or "")
            if not oid:
                continue
            src = str(o.get("order_source") or "")
            if src not in ("manual", "execution"):
                continue
            if oid not in open_after_ids:
                events += 1
                warnings += 1
                state.create_reconciliation_event(
                    event_type="missing_order",
                    status="warning",
                    summary=f"{src} order disappeared {oid} {o.get('symbol')}",
                    details={"order_id": oid, "symbol": o.get("symbol"), "source": src},
                )
                # If this is a loop-managed manual order, pause that loop only.
                if src == "manual":
                    try:
                        leg = conn.execute(
                            "SELECT loop_id FROM loop_legs WHERE binance_order_id = ? LIMIT 1",
                            (oid,),
                        ).fetchone()
                        if leg and leg[0]:
                            paused_loops.add(int(leg[0]))
                    except Exception:
                        pass

        # Position mismatch vs balances
        for p in positions:
            base_asset = str(p.get("base_asset") or "")
            if not base_asset:
                continue
            qty = _dec(p.get("quantity"))
            b = balances_after.get(base_asset, {})
            bal = _dec(b.get("free")) + _dec(b.get("locked"))
            if bal + Decimal("0.00000001") < qty:
                events += 1
                critical += 1
                critical_symbols.add(str(p.get("symbol") or "").upper())
                state.create_reconciliation_event(
                    event_type="position_mismatch",
                    status="manual_intervention_required",
                    summary=f"{p.get('symbol')} qty {qty} > balance {bal}",
                    details={"position_id": p.get("id"), "symbol": p.get("symbol"), "qty": str(qty), "balance": str(bal)},
                )
                state.close_position_external(position_id=int(p.get("id") or 0), reason="position_mismatch")

        # Uncertain executions
        cur = conn.execute(
            "SELECT COUNT(*) AS n FROM executions WHERE local_status IN ('uncertain_submitted','retry_submitted','submitting')"
        )
        n_uncertain = int(cur.fetchone()["n"])
        if n_uncertain > 0:
            events += 1
            critical += 1
            state.create_reconciliation_event(
                event_type="uncertain_execution_recovery",
                status="manual_intervention_required",
                summary=f"{n_uncertain} uncertain executions need reconcile",
                details={"count": n_uncertain},
            )

        # Determine overall status
        if events == 0:
            status = "no_change"
        elif critical > 0:
            status = "manual_intervention_required"
        else:
            status = "reconciled_with_warning"

        state.update_reconciliation_status(status=status)

        # Apply pause policy:
        # - Global pause only for critical execution mismatch / uncertain state.
        # - Scoped pauses for loop/symbol mismatches.
        if n_uncertain > 0:
            state.set_automation_paused(paused=True, reason="execution_uncertain", status=status)
        elif len(critical_symbols) > 1:
            state.set_automation_paused(paused=True, reason="multiple_position_mismatch", status=status)

        # Scoped pauses (loop_id / symbol).
        for loop_id in paused_loops:
            state.set_pause(scope_type="loop", scope_key=str(loop_id), reason="missing_order")
        for sym in critical_symbols:
            state.set_pause(scope_type="symbol", scope_key=sym, reason="position_mismatch")

        # Auto-resume scoped pauses on healthy reconciliation.
        if status in ("no_change", "reconciled_with_warning"):
            state.clear_all_scoped_pauses()

    print("Reconciliation Summary")
    print(f"- Status: {status}")
    print(f"- Events: {events}")
    print(f"- Warnings: {warnings}")
    print(f"- Critical: {critical}")
    if status == "manual_intervention_required":
        print("- Action: automation paused")
    return 0


def cmd_reliability_resume(args: argparse.Namespace) -> int:
    if not getattr(args, "i_am_human", False):
        print("Missing required flag: --i-am-human")
        return 2
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)
    scope_global = bool(getattr(args, "global_pause", False))
    scope_symbol = getattr(args, "symbol", None)
    scope_loop = getattr(args, "loop_id", None)

    if not (scope_global or scope_symbol or scope_loop):
        print("Specify one of --global, --symbol, or --loop-id")
        return 2

    with connect(db_path) as conn:
        state = StateManager(conn)
        sys_state = state.get_system_state() or {}
        status = str(sys_state.get("last_reconciliation_status") or "")
        if status in ("manual_intervention_required", ""):
            print("Rejected: system not healthy. Run `cryptogent reliability reconcile` first.")
            return 2

        if scope_global:
            state.set_automation_paused(paused=False, reason=None, status=status)
            state.create_reconciliation_event(
                event_type="resume",
                status="ok",
                summary="global automation resumed",
                details={"scope": "global"},
            )
            print("Automation resumed (global).")
            return 0

        if scope_symbol:
            sym = str(scope_symbol).strip().upper()
            state.clear_pause(scope_type="symbol", scope_key=sym)
            state.create_reconciliation_event(
                event_type="resume",
                status="ok",
                summary=f"symbol automation resumed {sym}",
                details={"scope": "symbol", "symbol": sym},
            )
            print(f"Automation resumed for symbol {sym}.")
            return 0

        if scope_loop:
            lid = int(scope_loop)
            state.clear_pause(scope_type="loop", scope_key=str(lid))
            state.create_reconciliation_event(
                event_type="resume",
                status="ok",
                summary=f"loop automation resumed {lid}",
                details={"scope": "loop", "loop_id": lid},
            )
            print(f"Automation resumed for loop {lid}.")
            return 0

    return 0


def _manual_client_order_id() -> str:
    rand = secrets.token_hex(2)
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"cg_manual_{ts}_{rand}"


def _manual_require_human(args: argparse.Namespace) -> None:
    if not getattr(args, "i_am_human", False):
        raise ValueError("Missing required flag: --i-am-human")
    if not getattr(args, "dry_run", False):
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            raise ValueError("Manual direct order mode requires an interactive TTY (or use --dry-run).")


def _manual_env_label(client: BinanceSpotClient) -> str:
    base = str(getattr(client, "base_url", "") or "").lower()
    return "testnet" if "testnet.binance.vision" in base else "mainnet"


def _manual_preview_common(
    *,
    env: str,
    base_url: str,
    dry_run: bool,
    symbol: str,
    side: str,
    order_type: str,
    time_in_force: str | None,
    limit_price: str | None,
    quantity: str | None,
    quote_order_qty: str | None,
    rules,
    free_quote: Decimal | None,
    free_base: Decimal | None,
) -> None:
    print("Manual Order Preview")
    print(f"- Environment: {env}")
    print(f"- Base URL: {base_url}")
    print(f"- Dry run: {'yes' if dry_run else 'no'}")
    print(f"- Symbol: {symbol}")
    print(f"- Side: {side}")
    print(f"- Type: {order_type}")
    if time_in_force:
        print(f"- Time-in-force: {time_in_force}")
    if limit_price:
        print(f"- Limit price: {limit_price}")
    if quantity:
        print(f"- Quantity: {quantity}")
    if quote_order_qty:
        print(f"- Quote order qty: {quote_order_qty}")
    print(f"- Rule base_asset: {rules.base_asset}")
    print(f"- Rule quote_asset: {rules.quote_asset}")
    if rules.lot_size:
        print(f"- Rule minQty: {rules.lot_size.min_qty} stepSize: {rules.lot_size.step_size}")
    if rules.min_notional:
        print(f"- Rule minNotional: {rules.min_notional.min_notional}")
    if rules.price_filter:
        print(f"- Rule tickSize: {rules.price_filter.tick_size}")
    if free_quote is not None:
        print(f"- Free {rules.quote_asset}: {free_quote}")
    if free_base is not None:
        print(f"- Free {rules.base_asset}: {free_base}")


def _manual_get_free_balances(*, acct: dict, quote_asset: str, base_asset: str) -> tuple[Decimal, Decimal]:
    balances = acct.get("balances", [])
    free_quote = Decimal("0")
    free_base = Decimal("0")
    if isinstance(balances, list):
        for b in balances:
            if not isinstance(b, dict):
                continue
            asset = str(b.get("asset") or "").strip().upper()
            if asset == quote_asset:
                free_quote = _d_position(b.get("free") or "0", "account.free_quote")
            if asset == base_asset:
                free_base = _d_position(b.get("free") or "0", "account.free_base")
    return free_quote, free_base


def _manual_submit_with_idempotency(
    *,
    client: BinanceSpotClient,
    state: StateManager,
    manual_order_id: int,
    symbol: str,
    client_order_id: str,
    submit_fn,
    submit_kwargs: dict,
) -> tuple[dict, int]:
    # First attempt.
    try:
        return submit_fn(**submit_kwargs), 0
    except BinanceAPIError as e:
        if e.status != 0:
            raise
        state.update_manual_order(
            manual_order_id=manual_order_id,
            local_status="uncertain_submitted",
            raw_status=None,
            binance_order_id=None,
            retry_count=0,
            executed_quantity=None,
            avg_fill_price=None,
            total_quote_value=None,
            fee_breakdown_json=None,
            message=str(e),
            details_json=_json.dumps({"reason": str(e), "stage": "submit"}, separators=(",", ":")),
        )

    # Reconcile, then retry once with same client_order_id.
    try:
        order = client.get_order_by_client_order_id(symbol=symbol, client_order_id=client_order_id)
        return order, 0
    except BinanceAPIError as e:
        if e.code != -2013:
            raise

    order = submit_fn(**submit_kwargs)
    state.update_manual_order(
        manual_order_id=manual_order_id,
        local_status="retry_submitted",
        raw_status=None,
        binance_order_id=None,
        retry_count=1,
        executed_quantity=None,
        avg_fill_price=None,
        total_quote_value=None,
        fee_breakdown_json=None,
        message="submitted_after_retry",
        details_json=None,
    )
    return order, 1


def _manual_finalize_from_order(*, state: StateManager, manual_order_id: int, order: dict, retry_count: int) -> None:
    from cryptogent.execution.result_parser import parse_fills

    raw_status = str(order.get("status") or "") or None
    order_id = str(order.get("orderId") or "") or None
    fills = None
    try:
        fills = parse_fills(order)
    except Exception:
        fills = None

    local_status = "submitted"
    if raw_status in ("NEW",):
        local_status = "open"
    elif raw_status == "FILLED":
        local_status = "filled"
    elif raw_status == "PARTIALLY_FILLED":
        local_status = "partially_filled"
    elif raw_status in ("CANCELED", "CANCELLED"):
        local_status = "cancelled"
    elif raw_status in ("EXPIRED",):
        local_status = "expired"

    fee_breakdown_json = None
    executed_quantity = None
    avg_fill_price = None
    total_quote_value = None
    if fills:
        fee_breakdown_json = _json.dumps(fills.commission_breakdown, separators=(",", ":")) if fills.commission_breakdown else None
        executed_quantity = str(fills.executed_qty)
        avg_fill_price = str(fills.avg_fill_price) if fills.avg_fill_price is not None else None
        total_quote_value = str(fills.total_quote_spent)

    state.update_manual_order(
        manual_order_id=manual_order_id,
        local_status=local_status,
        raw_status=raw_status,
        binance_order_id=order_id,
        retry_count=retry_count,
        executed_quantity=executed_quantity,
        avg_fill_price=avg_fill_price,
        total_quote_value=total_quote_value,
        fee_breakdown_json=fee_breakdown_json,
        message="submitted",
        details_json=_json.dumps({"raw_status": raw_status, "source": "exchange"}, separators=(",", ":")),
    )


def _manual_post_sync(*, client: BinanceSpotClient, conn, symbol: str) -> tuple[str | None, str | None]:
    bal_status = None
    oo_status = None
    try:
        bal = sync_balances(client=client, conn=conn)
        bal_status = getattr(bal, "status", None)
    except Exception:
        bal_status = None
    try:
        oo = sync_open_orders(client=client, conn=conn, symbol=symbol)
        oo_status = getattr(oo, "status", None)
    except Exception:
        oo_status = None
    return bal_status, oo_status


def cmd_trade_manual_list(args: argparse.Namespace) -> int:
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)
    limit = int(getattr(args, "limit", 20))
    with connect(db_path) as conn:
        rows = StateManager(conn).list_manual_orders(limit=limit)
    if not rows:
        print("(no manual orders)")
        return 0
    print(f"Manual orders: {len(rows)}")
    print(f"{'ID':>5} {'DRY':>3} {'ENV':<7} {'SYMBOL':<10} {'SIDE':<4} {'TYPE':<12} {'STATUS':<18} {'QTY':<12} {'QUOTE_QTY':<12} {'LMT':<12} {'ORDER_ID':<10}")
    for r in rows:
        print(
            f"{int(r.get('manual_order_id') or 0):>5} "
            f"{int(r.get('dry_run') or 0):>3} "
            f"{str(r.get('execution_environment') or '-'): <7} "
            f"{str(r.get('symbol') or '-'): <10} "
            f"{str(r.get('side') or '-'): <4} "
            f"{str(r.get('order_type') or '-'): <12} "
            f"{str(r.get('local_status') or '-'): <18} "
            f"{str(r.get('quantity') or '-'): <12} "
            f"{str(r.get('quote_order_qty') or '-'): <12} "
            f"{str(r.get('limit_price') or '-'): <12} "
            f"{str(r.get('binance_order_id') or '-'): <10}"
        )
    return 0


def cmd_trade_manual_show(args: argparse.Namespace) -> int:
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)
    with connect(db_path) as conn:
        row = StateManager(conn).get_manual_order(manual_order_id=int(args.manual_order_id))
    if not row:
        print("(not found)")
        return 2
    for k, v in row.items():
        print(f"{k}={v}")
    return 0


def cmd_trade_manual_buy_market(args: argparse.Namespace) -> int:
    _manual_require_human(args)
    client = _client_from_args(args)
    env = _manual_env_label(client)

    symbol = str(args.symbol or "").strip().upper()
    quote_qty = _d_position(args.quote_qty, "quote_qty")
    dry_run = bool(getattr(args, "dry_run", False))

    info = client.get_symbol_info(symbol=symbol)
    if not info:
        print("(symbol not found)")
        return 2
    rules = parse_symbol_rules(info)
    if rules.status != "TRADING":
        print(f"Rejected: symbol not TRADING (status={rules.status})")
        return 2

    acct = client.get_account()
    free_quote, free_base = _manual_get_free_balances(acct=acct, quote_asset=rules.quote_asset, base_asset=rules.base_asset)
    last_price = _d_position(client.get_ticker_price(symbol=symbol), "last_price")

    # Exchange rules: ensure the quote asset matches and estimate notional.
    pre = precheck_market_buy(rules=rules, budget_asset=rules.quote_asset, budget_amount=quote_qty, last_price=last_price)
    if not pre.ok:
        print(f"Rejected: {pre.error}")
        return 2
    if quote_qty > free_quote:
        print("Rejected: insufficient free quote balance")
        return 2

    _manual_preview_common(
        env=env,
        base_url=client.base_url,
        dry_run=dry_run,
        symbol=symbol,
        side="BUY",
        order_type="MARKET",
        time_in_force=None,
        limit_price=None,
        quantity=None,
        quote_order_qty=str(quote_qty),
        rules=rules,
        free_quote=free_quote,
        free_base=free_base,
    )
    print(f"- Live price: {last_price}")
    if pre.estimated_qty is not None:
        print(f"- Est. qty: {pre.estimated_qty}")
    if pre.notional is not None:
        print(f"- Est. notional: {pre.notional} {rules.quote_asset}")

    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)

    client_order_id = _manual_client_order_id()
    with connect(db_path) as conn:
        state = StateManager(conn)
        details = {"live_price": str(last_price), "free_quote": str(free_quote), "free_base": str(free_base)}
        manual_order_id = state.create_manual_order(
            dry_run=dry_run,
            execution_environment=env,
            base_url=str(client.base_url),
            symbol=symbol,
            side="BUY",
            order_type="MARKET_BUY",
            time_in_force=None,
            limit_price=None,
            quote_order_qty=str(quote_qty),
            quantity=None,
            client_order_id=client_order_id,
            message="preview",
            details_json=_json.dumps(details, separators=(",", ":")),
        )
        state.append_audit(
            level="INFO",
            event="manual_order_preview",
            details={"manual_order_id": manual_order_id, "symbol": symbol, "side": "BUY", "type": "MARKET_BUY", "env": env},
        )

        if dry_run:
            state.update_manual_order(
                manual_order_id=manual_order_id,
                local_status="dry_run",
                raw_status=None,
                binance_order_id=None,
                retry_count=0,
                executed_quantity=None,
                avg_fill_price=None,
                total_quote_value=None,
                fee_breakdown_json=None,
                message="dry_run_only",
                details_json=None,
            )
            print(f"DRY RUN: manual_order_id={manual_order_id} client_order_id={client_order_id}")
            return 0

        if not _prompt_yes_no("Submit to exchange now?", default=False):
            state.update_manual_order(
                manual_order_id=manual_order_id,
                local_status="cancelled",
                raw_status=None,
                binance_order_id=None,
                retry_count=0,
                executed_quantity=None,
                avg_fill_price=None,
                total_quote_value=None,
                fee_breakdown_json=None,
                message="cancelled_by_user",
                details_json=None,
            )
            print("Cancelled")
            return 2

        state.update_manual_order(
            manual_order_id=manual_order_id,
            local_status="submitting",
            raw_status=None,
            binance_order_id=None,
            retry_count=0,
            executed_quantity=None,
            avg_fill_price=None,
            total_quote_value=None,
            fee_breakdown_json=None,
            message="submitting",
            details_json=None,
        )

        try:
            order, retry_count = _manual_submit_with_idempotency(
                client=client,
                state=state,
                manual_order_id=manual_order_id,
                symbol=symbol,
                client_order_id=client_order_id,
                submit_fn=client.create_order_market_buy_quote,
                submit_kwargs={"symbol": symbol, "quote_order_qty": str(quote_qty), "client_order_id": client_order_id},
            )
        except Exception as e:
            state.update_manual_order(
                manual_order_id=manual_order_id,
                local_status="failed",
                raw_status=None,
                binance_order_id=None,
                retry_count=0,
                executed_quantity=None,
                avg_fill_price=None,
                total_quote_value=None,
                fee_breakdown_json=None,
                message=str(e),
                details_json=None,
            )
            print(f"ERROR: {e}")
            return 2

        _manual_finalize_from_order(state=state, manual_order_id=manual_order_id, order=order, retry_count=retry_count)
        state.append_audit(
            level="INFO",
            event="manual_order_submitted",
            details={"manual_order_id": manual_order_id, "symbol": symbol, "side": "BUY", "type": "MARKET_BUY", "env": env},
        )
        bal_status, oo_status = _manual_post_sync(client=client, conn=conn, symbol=symbol)

    print(f"OK manual_order_id={manual_order_id}")
    if bal_status:
        print(f"- Post-sync balances: {bal_status}")
    if oo_status:
        print(f"- Post-sync open orders: {oo_status}")
    return 0


def _trade_manual_reconcile_once(args: argparse.Namespace, *, quiet: bool = False) -> tuple[int, dict]:
    client = _client_from_args(args)
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)

    manual_order_id = getattr(args, "manual_order_id", None)
    limit = int(getattr(args, "limit", 50))
    updated = 0
    errors = 0
    status_counts: dict[str, int] = {}
    pre_status_counts: dict[str, int] = {}
    open_orders_seen: int | None = None
    manual_tracked: int = 0
    with connect(db_path) as conn:
        state = StateManager(conn)
        if manual_order_id not in (None, ""):
            rows = []
            row = state.get_manual_order(manual_order_id=int(manual_order_id))
            if row:
                rows = [row]
        else:
            rows = state.list_manual_orders_for_reconcile(limit=limit)

        for r in rows:
            ls = str(r.get("local_status") or "").strip()
            if ls:
                pre_status_counts[ls] = pre_status_counts.get(ls, 0) + 1

        if not rows:
            if not quiet:
                print("(no manual orders to reconcile)")
            # Still do a best-effort open-orders sync so the cache stays fresh.
            try:
                sync_open_orders(client=client, conn=conn, symbol=None)
            except Exception:
                pass
            return 0, {"updated": 0, "errors": 0, "status_counts": {}, "open_orders_seen": None}

        # Small fixed pause between per-order reconciliations to reduce rate-limit risk.
        # Intentionally not user-configurable.
        per_item_pause_s = 0.5
        for i, r in enumerate(rows, start=1):
            manual_tracked = i
            mid = int(r.get("manual_order_id") or 0)
            symbol = str(r.get("symbol") or "").strip().upper()
            client_order_id = str(r.get("client_order_id") or "").strip()
            if not (symbol and client_order_id):
                continue
            try:
                order = client.get_order_by_client_order_id(symbol=symbol, client_order_id=client_order_id)
            except BinanceAPIError as e:
                errors += 1
                state.update_manual_order(
                    manual_order_id=mid,
                    local_status=str(r.get("local_status") or "open"),
                    raw_status=str(r.get("raw_status") or "") or None,
                    binance_order_id=str(r.get("binance_order_id") or "") or None,
                    retry_count=int(r.get("retry_count") or 0),
                    executed_quantity=str(r.get("executed_quantity") or "") or None,
                    avg_fill_price=str(r.get("avg_fill_price") or "") or None,
                    total_quote_value=str(r.get("total_quote_value") or "") or None,
                    fee_breakdown_json=str(r.get("fee_breakdown_json") or "") or None,
                    message=f"reconcile_failed:{e}",
                    details_json=None,
                )
                continue

            raw_status = str(order.get("status") or "").upper()
            local_status = "submitted"
            if raw_status in ("NEW",):
                local_status = "open"
            elif raw_status == "FILLED":
                local_status = "filled"
            elif raw_status == "PARTIALLY_FILLED":
                local_status = "partially_filled"
            elif raw_status in ("CANCELED", "CANCELLED"):
                local_status = "cancelled"
            elif raw_status in ("EXPIRED",):
                local_status = "expired"
            status_counts[local_status] = status_counts.get(local_status, 0) + 1

            _manual_finalize_from_order(state=state, manual_order_id=mid, order=order, retry_count=int(r.get("retry_count") or 0))
            updated += 1
            if i < len(rows):
                time.sleep(per_item_pause_s)

        # Best-effort post-sync: refresh cached balances + open orders.
        try:
            sync_balances(client=client, conn=conn)
        except Exception:
            pass
        try:
            oo = sync_open_orders(client=client, conn=conn, symbol=None)
            try:
                open_orders_seen = int(oo.open_orders_seen)
            except Exception:
                open_orders_seen = None
        except Exception:
            pass

    stats = {
        "updated": updated,
        "errors": errors,
        "status_counts": status_counts,
        "pre_status_counts": pre_status_counts,
        "open_orders_seen": open_orders_seen,
        "manual_tracked": manual_tracked,
        "manual_total": len(rows),
    }
    if not quiet:
        print(f"OK reconciled updated={updated} errors={errors}")
    return (0 if errors == 0 else 2), stats


def cmd_trade_manual_reconcile(args: argparse.Namespace) -> int:
    if not getattr(args, "loop", False):
        rc, _ = _trade_manual_reconcile_once(args, quiet=False)
        return rc
    interval = int(getattr(args, "interval_seconds", 60))
    duration = getattr(args, "duration_seconds", None)
    end_at = None
    if duration not in (None, ""):
        end_at = time.monotonic() + int(duration)
    print("Manual reconcile loop started. Press Ctrl-B or Ctrl-C to stop.")
    with _cbreak_stdin():
        try:
            while True:
                rc, stats = _trade_manual_reconcile_once(args, quiet=True)
                pre = stats.get("pre_status_counts") or {}
                open_n = int(pre.get("open", 0))
                partial_n = int(pre.get("partially_filled", 0))
                sc = stats.get("status_counts") or {}
                filled_n = int(sc.get("filled", 0))
                cancelled_n = int(sc.get("cancelled", 0))
                expired_n = int(sc.get("expired", 0))
                tracked_i = int(stats.get("manual_tracked", 0) or 0)
                total_i = int(stats.get("manual_total", 0) or 0)
                oo_seen = stats.get("open_orders_seen")
                oo_s = f" oo={oo_seen}" if oo_seen is not None else ""
                line = (
                    f"manual reconcile: tracked_open={tracked_i}/{total_i} open={open_n} filled={filled_n} partial={partial_n} "
                    f"cancelled={cancelled_n} expired={expired_n} updated={stats.get('updated', 0)} "
                    f"errors={stats.get('errors', 0)}{oo_s}"
                )
                if sys.stdout.isatty():
                    sys.stdout.write("\r\x1b[2K" + line)
                    sys.stdout.flush()
                else:
                    print(line)
                active_n = open_n + partial_n
                if active_n == 0:
                    if sys.stdout.isatty():
                        print()
                    print("Stopped (no active manual orders)")
                    return 0
                if end_at is not None and time.monotonic() >= end_at:
                    if sys.stdout.isatty():
                        print()
                    print("Done")
                    return rc
                if _sleep_with_ctrl_b(seconds=float(interval), end_at=end_at, base_line=line, show_countdown=True):
                    if sys.stdout.isatty():
                        print()
                    print("Stopped")
                    return rc
        except KeyboardInterrupt:
            if sys.stdout.isatty():
                print()
            print("Stopped")
            return 0


def cmd_trade_reconcile_all(args: argparse.Namespace) -> int:
    if getattr(args, "auto_cancel_expired", None) is None:
        if sys.stdin.isatty() and sys.stdout.isatty():
            ans = _prompt_yes_no("Auto-cancel expired LIMIT orders on Binance?", default=False)
            setattr(args, "auto_cancel_expired", True if ans else False)
        else:
            setattr(args, "auto_cancel_expired", False)
    """
    Reconcile both Phase 7 executions and manual orders.

    This is a convenience wrapper; it runs:
      1) trade reconcile (executions)
      2) trade manual reconcile (manual_orders)
    """

    def _once(*, quiet: bool) -> tuple[int, dict, dict]:
        rc1, s1 = _trade_reconcile_once(args, quiet=quiet)
        rc2, s2 = _trade_manual_reconcile_once(args, quiet=quiet)
        return (0 if (rc1 == 0 and rc2 == 0) else 2), s1, s2

    if not getattr(args, "loop", False):
        rc, _, _ = _once(quiet=False)
        return rc

    interval = int(getattr(args, "interval_seconds", 60))
    duration = getattr(args, "duration_seconds", None)
    end_at = None
    if duration not in (None, ""):
        end_at = time.monotonic() + int(duration)

    print("Reconcile-all loop started. Press Ctrl-B or Ctrl-C to stop.")
    # Small fixed pause between per-order reconciliations to reduce rate-limit risk.
    # Intentionally not user-configurable.
    per_order_pause_s = 0.5

    with _cbreak_stdin():
        try:
            while True:
                # One "tick" = reconcile everything tracked + all currently open orders, then sleep interval.
                rc, trade_stats, manual_stats = _once(quiet=True)
                updated_n = int(trade_stats.get("updated", 0)) + int(manual_stats.get("updated", 0))
                errors_n = int(trade_stats.get("errors", 0)) + int(manual_stats.get("errors", 0))

                paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
                config_path = ensure_default_config(paths.config_path)
                db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)
                with connect(db_path) as conn:
                    state = StateManager(conn)
                    rows = state.list_open_orders_for_reconcile(limit=200)

                open_orders = [
                    (str(r.get("symbol") or "").strip().upper(), str(r.get("exchange_order_id") or "").strip())
                    for r in rows
                    if str(r.get("symbol") or "").strip() and str(r.get("exchange_order_id") or "").strip()
                ]
                open_total = len(open_orders)
                if open_total == 0:
                    if sys.stdout.isatty():
                        sys.stdout.write("\r\x1b[2K")
                        sys.stdout.flush()
                        print()
                    print("Stopped (no open orders)")
                    return 0

                # Reconcile all currently open orders one-by-one in this tick.
                client = _client_from_args(args)
                for i, (symbol, order_id) in enumerate(open_orders, start=1):
                    try:
                        order = client.get_order_by_order_id(symbol=symbol, order_id=order_id)
                        from cryptogent.util.time import ms_to_utc_iso

                        def _iso_ms(v: object) -> str:
                            try:
                                j = int(v)  # ms
                            except Exception:
                                j = 0
                            return ms_to_utc_iso(j) if j else utcnow_iso()

                        row = OrderRow(
                            exchange_order_id=str(order.get("orderId")) if order.get("orderId") is not None else order_id,
                            symbol=str(order.get("symbol") or symbol),
                            side=str(order.get("side") or ""),
                            type=str(order.get("type") or ""),
                            status=str(order.get("status") or ""),
                            time_in_force=str(order.get("timeInForce")) if order.get("timeInForce") is not None else None,
                            price=str(order.get("price")) if order.get("price") is not None else None,
                            quantity=str(order.get("origQty") or "0"),
                            filled_quantity=str(order.get("executedQty") or "0"),
                            executed_quantity=str(order.get("executedQty") or "0"),
                            created_at_utc=_iso_ms(order.get("time")),
                            updated_at_utc=_iso_ms(order.get("updateTime") or order.get("time")),
                        )
                        with connect(db_path) as conn:
                            StateManager(conn).upsert_orders([row])
                    except Exception:
                        errors_n += 1

                    line = f"reconcile-all: open={open_total} tracked_open={i} updated={updated_n} errors={errors_n}"
                    if sys.stdout.isatty():
                        sys.stdout.write("\r\x1b[2K" + line)
                        sys.stdout.flush()
                    else:
                        print(line)
                    # Small fixed sleep between orders (Ctrl-B stops immediately).
                    if _sleep_with_ctrl_b(seconds=per_order_pause_s, end_at=end_at, base_line=None, show_countdown=False):
                        if sys.stdout.isatty():
                            print()
                        print("Stopped")
                        return rc

                # End-of-tick status (shows tracked_open=open_total even when open_total=0).
                end_line = f"reconcile-all: open={open_total} tracked_open={open_total} updated={updated_n} errors={errors_n}"
                if sys.stdout.isatty():
                    sys.stdout.write("\r\x1b[2K" + end_line)
                    sys.stdout.flush()
                else:
                    print(end_line)

                if end_at is not None and time.monotonic() >= end_at:
                    if sys.stdout.isatty():
                        print()
                    print("Done")
                    return rc

                # Show countdown/spinner during sleep as a liveness indicator.
                if _sleep_with_ctrl_b(seconds=float(interval), end_at=end_at, base_line=end_line, show_countdown=True):
                    if sys.stdout.isatty():
                        print()
                    print("Stopped")
                    return rc
        except KeyboardInterrupt:
            if sys.stdout.isatty():
                print()
            print("Stopped")
            return 0


def cmd_trade_manual_cancel(args: argparse.Namespace) -> int:
    _manual_require_human(args)
    client = _client_from_args(args)
    env = _manual_env_label(client)

    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)

    with connect(db_path) as conn:
        state = StateManager(conn)
        row = state.get_manual_order(manual_order_id=int(args.manual_order_id))
        if not row:
            print("(not found)")
            return 2
        if int(row.get("dry_run") or 0) == 1:
            print("Rejected: cannot cancel a dry-run manual order")
            return 2
        local_status = str(row.get("local_status") or "")
        order_type = str(row.get("order_type") or "").strip().upper()
        if order_type not in ("LIMIT_BUY", "LIMIT_SELL"):
            print("Rejected: only LIMIT manual orders can be cancelled")
            return 2
        if local_status not in ("open", "submitted", "partially_filled", "uncertain_submitted", "retry_submitted", "submitting"):
            print(f"Rejected: not cancellable in status={local_status}")
            return 2

        symbol = str(row.get("symbol") or "").strip().upper()
        client_order_id = str(row.get("client_order_id") or "").strip()
        if not (symbol and client_order_id):
            print("Rejected: missing symbol/client_order_id")
            return 2

        print("Cancel Manual Order Preview")
        print(f"- Manual order id: {row.get('manual_order_id')}")
        print(f"- Environment: {env}")
        print(f"- Base URL: {client.base_url}")
        print(f"- Symbol: {symbol}")
        print(f"- Type: {order_type}")
        print(f"- Client order id: {client_order_id}")
        if not _prompt_yes_no("Cancel on exchange now?", default=False):
            print("Cancelled")
            return 2

        try:
            resp = client.cancel_order_by_client_order_id(symbol=symbol, client_order_id=client_order_id)
        except BinanceAPIError as e:
            state.update_manual_order(
                manual_order_id=int(args.manual_order_id),
                local_status=local_status,
                raw_status=str(row.get("raw_status") or "") or None,
                binance_order_id=str(row.get("binance_order_id") or "") or None,
                retry_count=int(row.get("retry_count") or 0),
                executed_quantity=str(row.get("executed_quantity") or "") or None,
                avg_fill_price=str(row.get("avg_fill_price") or "") or None,
                total_quote_value=str(row.get("total_quote_value") or "") or None,
                fee_breakdown_json=str(row.get("fee_breakdown_json") or "") or None,
                message=f"cancel_failed:{e}",
                details_json=None,
            )
            print(f"ERROR: {e}")
            return 2

        _manual_finalize_from_order(state=state, manual_order_id=int(args.manual_order_id), order=resp, retry_count=int(row.get("retry_count") or 0))
        state.append_audit(
            level="INFO",
            event="manual_order_cancelled",
            details={"manual_order_id": int(args.manual_order_id), "symbol": symbol, "type": order_type, "env": env},
        )

        # Post-sync (best-effort): refresh cached balances + open orders.
        try:
            sync_balances(client=client, conn=conn)
        except Exception:
            pass
        try:
            sync_open_orders(client=client, conn=conn, symbol=symbol)
        except Exception:
            pass

    print("Cancelled (requested)")
    return 0


def cmd_orders_cancel(args: argparse.Namespace) -> int:
    """
    Cancel an open order by exchange order id (manual/execution only).
    External orders are never cancelled.
    """
    client = _client_from_args(args)
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)
    order_id = str(getattr(args, "order_id", "") or "").strip()
    if not order_id:
        print("Missing --order-id")
        return 2

    with connect(db_path) as conn:
        state = StateManager(conn)
        row = conn.execute(
            """
            SELECT exchange_order_id, order_source, symbol, type, status
            FROM orders
            WHERE exchange_order_id = ?
            ORDER BY updated_at_utc DESC
            LIMIT 1
            """,
            (order_id,),
        ).fetchone()
        if not row:
            print("(order not found)")
            return 2
        row = dict(row)
        order_source = str(row.get("order_source") or "external")
        status = str(row.get("status") or "")
        symbol = str(row.get("symbol") or "").strip().upper()
        order_type = str(row.get("type") or "").strip().upper()

        if status not in ("NEW", "PARTIALLY_FILLED"):
            print(f"Not cancellable (status={status})")
            return 2
        if order_source == "external":
            print("Rejected: external orders cannot be cancelled by CryptoGent")
            return 2
        if order_source == "manual" and not getattr(args, "i_am_human", False):
            print("Missing required flag: --i-am-human (manual orders only)")
            return 2
        if order_type == "MARKET":
            print("Rejected: MARKET orders cannot be cancelled")
            return 2
        if not symbol:
            print("Missing symbol for order")
            return 2

        # Locate client_order_id from manual/execution records.
        client_order_id = None
        manual_row = None
        loop_leg = None
        exec_row = None
        if order_source == "manual":
            manual_row = conn.execute(
                "SELECT * FROM manual_orders WHERE binance_order_id = ? ORDER BY updated_at_utc DESC LIMIT 1",
                (order_id,),
            ).fetchone()
            if manual_row:
                manual_row = dict(manual_row)
                client_order_id = str(manual_row.get("client_order_id") or "").strip() or None
            if not client_order_id:
                loop_leg = conn.execute(
                    "SELECT * FROM loop_legs WHERE binance_order_id = ? ORDER BY updated_at_utc DESC LIMIT 1",
                    (order_id,),
                ).fetchone()
                if loop_leg:
                    loop_leg = dict(loop_leg)
                    client_order_id = str(loop_leg.get("client_order_id") or "").strip() or None
        elif order_source == "execution":
            exec_row = state.get_execution_by_binance_order_id(binance_order_id=order_id)
            if exec_row:
                client_order_id = str(exec_row.get("client_order_id") or "").strip() or None

        if not client_order_id:
            print("Missing client_order_id for this open order")
            return 2

        if order_source == "manual":
            print("Cancel Manual Order Preview")
            print(f"- Order id: {order_id}")
            print(f"- Symbol: {symbol}")
            print(f"- Type: {order_type}")
            print(f"- Client order id: {client_order_id}")
            if not _prompt_yes_no("Cancel on exchange now?", default=False):
                print("No changes made.")
                return 2

        try:
            client.cancel_order_by_client_order_id(symbol=symbol, client_order_id=client_order_id)
        except BinanceAPIError as e:
            print(f"ERROR: {e}")
            return 2

        # Reconcile to update local rows.
        try:
            order = client.get_order_by_client_order_id(symbol=symbol, client_order_id=client_order_id)
        except Exception:
            order = None

        if order is not None:
            raw_status = str(order.get("status") or "") or None
            # Update manual order / loop leg / execution based on source.
            if manual_row:
                _manual_finalize_from_order(state=state, manual_order_id=int(manual_row.get("manual_order_id") or 0), order=order, retry_count=int(manual_row.get("retry_count") or 0))
            if loop_leg:
                _loop_finalize_leg_from_order(state=state, leg_id=int(loop_leg.get("leg_id") or 0), order=order, retry_count=int(loop_leg.get("retry_count") or 0))
            if exec_row:
                from cryptogent.execution.result_parser import parse_fills

                fills = None
                try:
                    fills = parse_fills(order)
                except Exception:
                    fills = None
                local_status = "submitted"
                if raw_status in ("NEW",):
                    local_status = "open"
                elif raw_status == "FILLED":
                    local_status = "filled"
                elif raw_status == "PARTIALLY_FILLED":
                    local_status = "partially_filled"
                elif raw_status in ("CANCELED", "CANCELLED"):
                    local_status = "cancelled"
                elif raw_status in ("EXPIRED",):
                    local_status = "expired"
                state.update_execution(
                    execution_id=int(exec_row.get("execution_id") or 0),
                    local_status=local_status,
                    raw_status=raw_status,
                    binance_order_id=str(order.get("orderId") or "") or None,
                    executed_quantity=str(fills.executed_qty) if fills else None,
                    avg_fill_price=str(fills.avg_fill_price) if fills and fills.avg_fill_price is not None else None,
                    total_quote_spent=str(fills.total_quote_spent) if fills else None,
                    commission_total=str(fills.commission_total) if fills and fills.commission_total is not None else None,
                    commission_asset=(fills.commission_asset if fills else None),
                    fills_count=(fills.fills_count if fills else None),
                    retry_count=int(exec_row.get("retry_count") or 0),
                    message="cancel_requested",
                    details_json=None,
                    submitted_at_utc=str(exec_row.get("submitted_at_utc") or "") or None,
                    reconciled_at_utc=utcnow_iso(),
                )

        # Best-effort sync to refresh open orders/balances.
        try:
            sync_open_orders(client=client, conn=conn, symbol=symbol)
        except Exception:
            pass
        try:
            sync_balances(client=client, conn=conn)
        except Exception:
            pass
        try:
            state.recompute_locked_qty_for_open_positions()
        except Exception:
            pass

    print("Cancel requested on exchange.")
    return 0


def cmd_trade_manual_buy_limit(args: argparse.Namespace) -> int:
    _manual_require_human(args)
    client = _client_from_args(args)
    env = _manual_env_label(client)

    symbol = str(args.symbol or "").strip().upper()
    quote_qty = _d_position(args.quote_qty, "quote_qty")
    limit_price_raw = _d_position(args.limit_price, "limit_price")
    dry_run = bool(getattr(args, "dry_run", False))

    info = client.get_symbol_info(symbol=symbol)
    if not info:
        print("(symbol not found)")
        return 2
    rules = parse_symbol_rules(info)
    if rules.status != "TRADING":
        print(f"Rejected: symbol not TRADING (status={rules.status})")
        return 2
    if not (rules.lot_size and rules.min_notional and rules.price_filter):
        print("Rejected: missing required symbol filters (LOT_SIZE/MIN_NOTIONAL/PRICE_FILTER)")
        return 2

    acct = client.get_account()
    free_quote, free_base = _manual_get_free_balances(acct=acct, quote_asset=rules.quote_asset, base_asset=rules.base_asset)

    tick = rules.price_filter.tick_size
    step = rules.lot_size.step_size
    limit_price = quantize_down(limit_price_raw, tick)
    if limit_price <= 0:
        print("Rejected: invalid limit price after tick rounding")
        return 2
    est_qty_raw = quote_qty / limit_price
    qty = quantize_down(est_qty_raw, step)

    notional = qty * limit_price
    if quote_qty > free_quote:
        print("Rejected: insufficient free quote balance")
        return 2
    if qty <= 0:
        print("Rejected: quantity rounded to zero")
        return 2
    if qty < rules.lot_size.min_qty:
        print("Rejected: qty below minQty")
        return 2
    if notional < rules.min_notional.min_notional:
        print("Rejected: minNotional failed")
        return 2

    _manual_preview_common(
        env=env,
        base_url=client.base_url,
        dry_run=dry_run,
        symbol=symbol,
        side="BUY",
        order_type="LIMIT",
        time_in_force="GTC",
        limit_price=str(limit_price),
        quantity=str(qty),
        quote_order_qty=str(quote_qty),
        rules=rules,
        free_quote=free_quote,
        free_base=free_base,
    )
    print(f"- Est. notional: {notional} {rules.quote_asset}")

    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)

    client_order_id = _manual_client_order_id()
    with connect(db_path) as conn:
        state = StateManager(conn)
        details = {"free_quote": str(free_quote), "free_base": str(free_base)}
        manual_order_id = state.create_manual_order(
            dry_run=dry_run,
            execution_environment=env,
            base_url=str(client.base_url),
            symbol=symbol,
            side="BUY",
            order_type="LIMIT_BUY",
            time_in_force="GTC",
            limit_price=str(limit_price),
            quote_order_qty=str(quote_qty),
            quantity=str(qty),
            client_order_id=client_order_id,
            message="preview",
            details_json=_json.dumps(details, separators=(",", ":")),
        )
        state.append_audit(
            level="INFO",
            event="manual_order_preview",
            details={"manual_order_id": manual_order_id, "symbol": symbol, "side": "BUY", "type": "LIMIT_BUY", "env": env},
        )

        if dry_run:
            state.update_manual_order(
                manual_order_id=manual_order_id,
                local_status="dry_run",
                raw_status=None,
                binance_order_id=None,
                retry_count=0,
                executed_quantity=None,
                avg_fill_price=None,
                total_quote_value=None,
                fee_breakdown_json=None,
                message="dry_run_only",
                details_json=None,
            )
            print(f"DRY RUN: manual_order_id={manual_order_id} client_order_id={client_order_id}")
            return 0

        if not _prompt_yes_no("Submit to exchange now?", default=False):
            state.update_manual_order(
                manual_order_id=manual_order_id,
                local_status="cancelled",
                raw_status=None,
                binance_order_id=None,
                retry_count=0,
                executed_quantity=None,
                avg_fill_price=None,
                total_quote_value=None,
                fee_breakdown_json=None,
                message="cancelled_by_user",
                details_json=None,
            )
            print("Cancelled")
            return 2

        state.update_manual_order(
            manual_order_id=manual_order_id,
            local_status="submitting",
            raw_status=None,
            binance_order_id=None,
            retry_count=0,
            executed_quantity=None,
            avg_fill_price=None,
            total_quote_value=None,
            fee_breakdown_json=None,
            message="submitting",
            details_json=None,
        )

        try:
            order, retry_count = _manual_submit_with_idempotency(
                client=client,
                state=state,
                manual_order_id=manual_order_id,
                symbol=symbol,
                client_order_id=client_order_id,
                submit_fn=client.create_order_limit_buy,
                submit_kwargs={
                    "symbol": symbol,
                    "price": str(limit_price),
                    "quantity": str(qty),
                    "client_order_id": client_order_id,
                    "time_in_force": "GTC",
                },
            )
        except Exception as e:
            state.update_manual_order(
                manual_order_id=manual_order_id,
                local_status="failed",
                raw_status=None,
                binance_order_id=None,
                retry_count=0,
                executed_quantity=None,
                avg_fill_price=None,
                total_quote_value=None,
                fee_breakdown_json=None,
                message=str(e),
                details_json=None,
            )
            print(f"ERROR: {e}")
            return 2
        _manual_finalize_from_order(state=state, manual_order_id=manual_order_id, order=order, retry_count=retry_count)
        state.append_audit(
            level="INFO",
            event="manual_order_submitted",
            details={"manual_order_id": manual_order_id, "symbol": symbol, "side": "BUY", "type": "LIMIT_BUY", "env": env},
        )
        bal_status, oo_status = _manual_post_sync(client=client, conn=conn, symbol=symbol)

    print(f"OK manual_order_id={manual_order_id}")
    if bal_status:
        print(f"- Post-sync balances: {bal_status}")
    if oo_status:
        print(f"- Post-sync open orders: {oo_status}")
    return 0


def cmd_trade_manual_sell_market(args: argparse.Namespace) -> int:
    _manual_require_human(args)
    client = _client_from_args(args)
    env = _manual_env_label(client)

    symbol = str(args.symbol or "").strip().upper()
    base_qty_raw = _d_position(args.base_qty, "base_qty")
    dry_run = bool(getattr(args, "dry_run", False))

    info = client.get_symbol_info(symbol=symbol)
    if not info:
        print("(symbol not found)")
        return 2
    rules = parse_symbol_rules(info)
    if rules.status != "TRADING":
        print(f"Rejected: symbol not TRADING (status={rules.status})")
        return 2
    if not (rules.lot_size and rules.min_notional):
        print("Rejected: missing required symbol filters (LOT_SIZE/MIN_NOTIONAL)")
        return 2

    acct = client.get_account()
    free_quote, free_base = _manual_get_free_balances(acct=acct, quote_asset=rules.quote_asset, base_asset=rules.base_asset)
    live_price = _d_position(client.get_ticker_price(symbol=symbol), "live_price")

    qty = quantize_down(base_qty_raw, rules.lot_size.step_size)
    if qty <= 0:
        print("Rejected: quantity rounded to zero")
        return 2
    if qty < rules.lot_size.min_qty:
        print("Rejected: qty below minQty")
        return 2
    if qty > free_base:
        print("Rejected: insufficient free base balance")
        return 2
    if (qty * live_price) < rules.min_notional.min_notional:
        print("Rejected: minNotional failed")
        return 2

    _manual_preview_common(
        env=env,
        base_url=client.base_url,
        dry_run=dry_run,
        symbol=symbol,
        side="SELL",
        order_type="MARKET",
        time_in_force=None,
        limit_price=None,
        quantity=str(qty),
        quote_order_qty=None,
        rules=rules,
        free_quote=free_quote,
        free_base=free_base,
    )
    print(f"- Live price: {live_price}")
    print(f"- Est. proceeds: {(qty * live_price)} {rules.quote_asset}")

    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)

    client_order_id = _manual_client_order_id()
    with connect(db_path) as conn:
        state = StateManager(conn)
        details = {"live_price": str(live_price), "free_quote": str(free_quote), "free_base": str(free_base)}
        manual_order_id = state.create_manual_order(
            dry_run=dry_run,
            execution_environment=env,
            base_url=str(client.base_url),
            symbol=symbol,
            side="SELL",
            order_type="MARKET_SELL",
            time_in_force=None,
            limit_price=None,
            quote_order_qty=None,
            quantity=str(qty),
            client_order_id=client_order_id,
            message="preview",
            details_json=_json.dumps(details, separators=(",", ":")),
        )
        state.append_audit(
            level="INFO",
            event="manual_order_preview",
            details={"manual_order_id": manual_order_id, "symbol": symbol, "side": "SELL", "type": "MARKET_SELL", "env": env},
        )

        if dry_run:
            state.update_manual_order(
                manual_order_id=manual_order_id,
                local_status="dry_run",
                raw_status=None,
                binance_order_id=None,
                retry_count=0,
                executed_quantity=None,
                avg_fill_price=None,
                total_quote_value=None,
                fee_breakdown_json=None,
                message="dry_run_only",
                details_json=None,
            )
            print(f"DRY RUN: manual_order_id={manual_order_id} client_order_id={client_order_id}")
            return 0

        if not _prompt_yes_no("Submit to exchange now?", default=False):
            state.update_manual_order(
                manual_order_id=manual_order_id,
                local_status="cancelled",
                raw_status=None,
                binance_order_id=None,
                retry_count=0,
                executed_quantity=None,
                avg_fill_price=None,
                total_quote_value=None,
                fee_breakdown_json=None,
                message="cancelled_by_user",
                details_json=None,
            )
            print("Cancelled")
            return 2

        state.update_manual_order(
            manual_order_id=manual_order_id,
            local_status="submitting",
            raw_status=None,
            binance_order_id=None,
            retry_count=0,
            executed_quantity=None,
            avg_fill_price=None,
            total_quote_value=None,
            fee_breakdown_json=None,
            message="submitting",
            details_json=None,
        )

        try:
            order, retry_count = _manual_submit_with_idempotency(
                client=client,
                state=state,
                manual_order_id=manual_order_id,
                symbol=symbol,
                client_order_id=client_order_id,
                submit_fn=client.create_order_market_sell_qty,
                submit_kwargs={"symbol": symbol, "quantity": str(qty), "client_order_id": client_order_id},
            )
        except Exception as e:
            state.update_manual_order(
                manual_order_id=manual_order_id,
                local_status="failed",
                raw_status=None,
                binance_order_id=None,
                retry_count=0,
                executed_quantity=None,
                avg_fill_price=None,
                total_quote_value=None,
                fee_breakdown_json=None,
                message=str(e),
                details_json=None,
            )
            print(f"ERROR: {e}")
            return 2
        _manual_finalize_from_order(state=state, manual_order_id=manual_order_id, order=order, retry_count=retry_count)
        state.append_audit(
            level="INFO",
            event="manual_order_submitted",
            details={"manual_order_id": manual_order_id, "symbol": symbol, "side": "SELL", "type": "MARKET_SELL", "env": env},
        )
        bal_status, oo_status = _manual_post_sync(client=client, conn=conn, symbol=symbol)

    print(f"OK manual_order_id={manual_order_id}")
    if bal_status:
        print(f"- Post-sync balances: {bal_status}")
    if oo_status:
        print(f"- Post-sync open orders: {oo_status}")
    return 0


def cmd_trade_manual_sell_limit(args: argparse.Namespace) -> int:
    _manual_require_human(args)
    client = _client_from_args(args)
    env = _manual_env_label(client)

    symbol = str(args.symbol or "").strip().upper()
    base_qty_raw = _d_position(args.base_qty, "base_qty")
    limit_price_raw = _d_position(args.limit_price, "limit_price")
    dry_run = bool(getattr(args, "dry_run", False))

    info = client.get_symbol_info(symbol=symbol)
    if not info:
        print("(symbol not found)")
        return 2
    rules = parse_symbol_rules(info)
    if rules.status != "TRADING":
        print(f"Rejected: symbol not TRADING (status={rules.status})")
        return 2
    if not (rules.lot_size and rules.min_notional and rules.price_filter):
        print("Rejected: missing required symbol filters (LOT_SIZE/MIN_NOTIONAL/PRICE_FILTER)")
        return 2

    acct = client.get_account()
    free_quote, free_base = _manual_get_free_balances(acct=acct, quote_asset=rules.quote_asset, base_asset=rules.base_asset)

    limit_price = quantize_down(limit_price_raw, rules.price_filter.tick_size)
    qty = quantize_down(base_qty_raw, rules.lot_size.step_size)
    notional = qty * limit_price

    if qty <= 0:
        print("Rejected: quantity rounded to zero")
        return 2
    if limit_price <= 0:
        print("Rejected: invalid limit price after tick rounding")
        return 2
    if qty < rules.lot_size.min_qty:
        print("Rejected: qty below minQty")
        return 2
    if qty > free_base:
        print("Rejected: insufficient free base balance")
        return 2
    if notional < rules.min_notional.min_notional:
        print("Rejected: minNotional failed")
        return 2

    _manual_preview_common(
        env=env,
        base_url=client.base_url,
        dry_run=dry_run,
        symbol=symbol,
        side="SELL",
        order_type="LIMIT",
        time_in_force="GTC",
        limit_price=str(limit_price),
        quantity=str(qty),
        quote_order_qty=None,
        rules=rules,
        free_quote=free_quote,
        free_base=free_base,
    )
    print(f"- Est. proceeds: {notional} {rules.quote_asset}")

    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)

    client_order_id = _manual_client_order_id()
    with connect(db_path) as conn:
        state = StateManager(conn)
        details = {"free_quote": str(free_quote), "free_base": str(free_base)}
        manual_order_id = state.create_manual_order(
            dry_run=dry_run,
            execution_environment=env,
            base_url=str(client.base_url),
            symbol=symbol,
            side="SELL",
            order_type="LIMIT_SELL",
            time_in_force="GTC",
            limit_price=str(limit_price),
            quote_order_qty=None,
            quantity=str(qty),
            client_order_id=client_order_id,
            message="preview",
            details_json=_json.dumps(details, separators=(",", ":")),
        )
        state.append_audit(
            level="INFO",
            event="manual_order_preview",
            details={"manual_order_id": manual_order_id, "symbol": symbol, "side": "SELL", "type": "LIMIT_SELL", "env": env},
        )

        if dry_run:
            state.update_manual_order(
                manual_order_id=manual_order_id,
                local_status="dry_run",
                raw_status=None,
                binance_order_id=None,
                retry_count=0,
                executed_quantity=None,
                avg_fill_price=None,
                total_quote_value=None,
                fee_breakdown_json=None,
                message="dry_run_only",
                details_json=None,
            )
            print(f"DRY RUN: manual_order_id={manual_order_id} client_order_id={client_order_id}")
            return 0

        if not _prompt_yes_no("Submit to exchange now?", default=False):
            state.update_manual_order(
                manual_order_id=manual_order_id,
                local_status="cancelled",
                raw_status=None,
                binance_order_id=None,
                retry_count=0,
                executed_quantity=None,
                avg_fill_price=None,
                total_quote_value=None,
                fee_breakdown_json=None,
                message="cancelled_by_user",
                details_json=None,
            )
            print("Cancelled")
            return 2

        state.update_manual_order(
            manual_order_id=manual_order_id,
            local_status="submitting",
            raw_status=None,
            binance_order_id=None,
            retry_count=0,
            executed_quantity=None,
            avg_fill_price=None,
            total_quote_value=None,
            fee_breakdown_json=None,
            message="submitting",
            details_json=None,
        )

        try:
            order, retry_count = _manual_submit_with_idempotency(
                client=client,
                state=state,
                manual_order_id=manual_order_id,
                symbol=symbol,
                client_order_id=client_order_id,
                submit_fn=client.create_order_limit_sell,
                submit_kwargs={
                    "symbol": symbol,
                    "price": str(limit_price),
                    "quantity": str(qty),
                    "client_order_id": client_order_id,
                    "time_in_force": "GTC",
                },
            )
        except Exception as e:
            state.update_manual_order(
                manual_order_id=manual_order_id,
                local_status="failed",
                raw_status=None,
                binance_order_id=None,
                retry_count=0,
                executed_quantity=None,
                avg_fill_price=None,
                total_quote_value=None,
                fee_breakdown_json=None,
                message=str(e),
                details_json=None,
            )
            print(f"ERROR: {e}")
            return 2
        _manual_finalize_from_order(state=state, manual_order_id=manual_order_id, order=order, retry_count=retry_count)
        state.append_audit(
            level="INFO",
            event="manual_order_submitted",
            details={"manual_order_id": manual_order_id, "symbol": symbol, "side": "SELL", "type": "LIMIT_SELL", "env": env},
        )
        bal_status, oo_status = _manual_post_sync(client=client, conn=conn, symbol=symbol)

    print(f"OK manual_order_id={manual_order_id}")
    if bal_status:
        print(f"- Post-sync balances: {bal_status}")
    if oo_status:
        print(f"- Post-sync open orders: {oo_status}")
    return 0


def _loop_client_order_id(*, loop_id: int, leg_role: str) -> str:
    """
    Binance constraint: newClientOrderId must match ^[a-zA-Z0-9-_]{1,36}$.
    Keep this short and deterministic-ish for reconciliation.
    """
    rand = secrets.token_hex(2)  # 4 chars
    ts = datetime.now(UTC).strftime("%y%m%d%H%M%S")  # 12 chars
    role = str(leg_role or "").strip().lower()
    role_code = {
        "buy_entry": "be",
        "sell_tp": "st",
        "buy_rebuy": "br",
        "sell_sl": "sx",
        "sell_cleanup": "sc",
    }.get(role, "x")
    # Example: cgl1be260318121530a7k2 (<= 36 chars)
    cid = f"cgl{int(loop_id)}{role_code}{ts}{rand}"
    return cid[:36]


def _parse_offset_signed_default_negative(raw: str, *, name: str) -> Decimal:
    """
    Locked rule: rebuy offset may be below or above last sell.
    - If user provides no sign, default is "dip" (negative).
    - If user provides +/-, honor it.
    """
    s = str(raw or "").strip()
    if not s:
        raise ValueError(f"Missing {name}")
    if s[0] not in "+-":
        s = "-" + s
    return _d_position(s, name)


def _parse_offset_positive(raw: str, *, name: str) -> Decimal:
    s = str(raw or "").strip()
    if not s:
        raise ValueError(f"Missing {name}")
    d = _d_position(s, name)
    if d <= 0:
        raise ValueError(f"{name} must be > 0")
    return d


def _loop_preview(
    *,
    env: str,
    base_url: str,
    dry_run: bool,
    symbol: str,
    quote_qty: Decimal,
    entry_order_type: str,
    entry_limit_price: Decimal | None,
    take_profit_kind: str,
    take_profit_value: Decimal,
    rebuy_kind: str | None,
    rebuy_value: Decimal | None,
    stop_loss_kind: str | None,
    stop_loss_value: Decimal | None,
    stop_loss_action: str,
    cleanup_policy: str,
    max_cycles: int,
    rules: SymbolRules,
    free_quote: Decimal | None,
    free_base: Decimal | None,
) -> None:
    print("Manual Loop Preview")
    print(f"- Environment: {env}")
    print(f"- Base URL: {base_url}")
    print(f"- Dry run: {'yes' if dry_run else 'no'}")
    print(f"- Symbol: {symbol}")
    print(f"- Quote qty: {quote_qty} {rules.quote_asset}")
    print(f"- Entry: {entry_order_type}")
    if entry_limit_price is not None:
        print(f"- Entry limit price: {entry_limit_price}")
    print(f"- Take-profit: {take_profit_kind}={take_profit_value}")
    if rebuy_kind and rebuy_value is not None:
        print(f"- Rebuy: {rebuy_kind}={rebuy_value} (signed; default dip when no sign)")
    else:
        print(f"- Rebuy: (none)")
    if stop_loss_kind and stop_loss_value is not None:
        print(f"- Stop-loss: {stop_loss_kind}={stop_loss_value} (ref=last BUY avg)")
        print(f"- Stop-loss action: {stop_loss_action}")
    else:
        print(f"- Stop-loss: (none)")
        print(f"- Stop-loss action: {stop_loss_action}")
    print(f"- Cleanup policy: {cleanup_policy}")
    print(f"- Max cycles: {max_cycles} (0=infinite)")
    if rules.lot_size:
        print(f"- Rule minQty: {rules.lot_size.min_qty} stepSize: {rules.lot_size.step_size}")
    if rules.min_notional:
        print(f"- Rule minNotional: {rules.min_notional.min_notional}")
    if rules.price_filter:
        print(f"- Rule tickSize: {rules.price_filter.tick_size}")
    if free_quote is not None:
        print(f"- Free {rules.quote_asset}: {free_quote}")
    if free_base is not None:
        print(f"- Free {rules.base_asset}: {free_base}")


def _loop_finalize_leg_from_order(*, state: StateManager, leg_id: int, order: dict, retry_count: int) -> None:
    raw_status = str(order.get("status") or "").strip().upper() or None
    order_id = order.get("orderId")
    binance_order_id = str(order_id) if order_id not in (None, "") else None
    # Map exchange status to local status.
    local_status = "submitted"
    if raw_status == "NEW":
        local_status = "open"
    elif raw_status == "PARTIALLY_FILLED":
        local_status = "partially_filled"
    elif raw_status == "FILLED":
        local_status = "filled"
    elif raw_status in ("CANCELED", "CANCELLED"):
        local_status = "cancelled"
    elif raw_status == "EXPIRED":
        local_status = "expired"
    elif raw_status == "REJECTED":
        local_status = "failed"

    fills = None
    try:
        fills = parse_fills(order)
    except Exception:
        fills = None
    fee_json = None
    executed_qty = None
    avg_price = None
    total_quote = None
    filled_at = None
    if fills:
        fee_json = _json.dumps(fills.commission_breakdown, separators=(",", ":"))
        executed_qty = str(fills.executed_qty)
        avg_price = str(fills.avg_fill_price) if fills.avg_fill_price is not None else None
        total_quote = str(fills.total_quote_spent)
        if local_status == "filled":
            filled_at = utcnow_iso()

    state.update_loop_leg(
        leg_id=leg_id,
        local_status=local_status,
        raw_status=raw_status,
        binance_order_id=binance_order_id,
        retry_count=int(retry_count),
        executed_quantity=executed_qty,
        avg_fill_price=avg_price,
        total_quote_value=total_quote,
        fee_breakdown_json=fee_json,
        message="reconciled",
        reconciled_at_utc=utcnow_iso(),
        filled_at_utc=filled_at,
    )


def _loop_submit_with_idempotency(
    *,
    client: BinanceSpotClient,
    state: StateManager,
    leg_id: int,
    symbol: str,
    client_order_id: str,
    submit_fn,
    submit_kwargs: dict,
) -> tuple[dict, int]:
    try:
        return submit_fn(**submit_kwargs), 0
    except BinanceAPIError as e:
        if e.status != 0:
            raise
        state.update_loop_leg(
            leg_id=leg_id,
            local_status="uncertain_submitted",
            raw_status=None,
            binance_order_id=None,
            retry_count=0,
            executed_quantity=None,
            avg_fill_price=None,
            total_quote_value=None,
            fee_breakdown_json=None,
            message=str(e),
        )
        # Reconcile by client order id.
        order = client.get_order_by_client_order_id(symbol=symbol, client_order_id=client_order_id)
        return order, 0


def cmd_trade_manual_loop_create(args: argparse.Namespace) -> int:
    """
    Stores a reusable manual loop configuration (preset). No exchange side effects.
    """
    symbol = str(getattr(args, "symbol", "") or "").strip().upper()
    if not symbol:
        print("Rejected: missing --symbol")
        return 2
    quote_qty = _d_position(getattr(args, "quote_qty", None), "quote_qty")
    if quote_qty <= 0:
        print("Rejected: --quote-qty must be > 0")
        return 2

    entry_order_type = str(getattr(args, "entry_type", "BUY_MARKET") or "BUY_MARKET").strip().upper()
    if entry_order_type not in ("BUY_MARKET", "BUY_LIMIT"):
        print("Rejected: invalid --entry-type")
        return 2
    entry_limit_price: Decimal | None = None
    if entry_order_type == "BUY_LIMIT":
        raw = str(getattr(args, "entry_limit_price", "") or "").strip()
        if not raw:
            print("Rejected: BUY_LIMIT requires --entry-limit-price")
            return 2
        entry_limit_price = _d_position(raw, "entry_limit_price")
        if entry_limit_price <= 0:
            print("Rejected: invalid entry limit price")
            return 2

    tp_abs = getattr(args, "take_profit_abs", None)
    tp_pct = getattr(args, "take_profit_pct", None)
    if (tp_abs in (None, "")) == (tp_pct in (None, "")):
        print("Rejected: provide exactly one of --take-profit-abs or --take-profit-pct")
        return 2
    if tp_abs not in (None, ""):
        take_profit_kind = "abs"
        take_profit_value = _parse_offset_positive(str(tp_abs), name="take_profit_abs")
    else:
        take_profit_kind = "pct"
        take_profit_value = _parse_offset_positive(str(tp_pct), name="take_profit_pct")

    rebuy_abs = getattr(args, "rebuy_abs", None)
    rebuy_pct = getattr(args, "rebuy_pct", None)
    if rebuy_abs not in (None, "") and rebuy_pct not in (None, ""):
        print("Rejected: provide only one of --rebuy-abs or --rebuy-pct")
        return 2
    rebuy_kind: str | None = None
    rebuy_value: Decimal | None = None
    if rebuy_abs not in (None, ""):
        rebuy_kind = "abs"
        rebuy_value = _parse_offset_signed_default_negative(str(rebuy_abs), name="rebuy_abs")
    elif rebuy_pct not in (None, ""):
        rebuy_kind = "pct"
        rebuy_value = _parse_offset_signed_default_negative(str(rebuy_pct), name="rebuy_pct")

    sl_abs = getattr(args, "stop_loss_abs", None)
    sl_pct = getattr(args, "stop_loss_pct", None)
    if sl_abs not in (None, "") and sl_pct not in (None, ""):
        print("Rejected: provide only one of --stop-loss-abs or --stop-loss-pct")
        return 2
    stop_loss_kind: str | None = None
    stop_loss_value: Decimal | None = None
    if sl_abs not in (None, ""):
        stop_loss_kind = "abs"
        stop_loss_value = _parse_offset_positive(str(sl_abs), name="stop_loss_abs")
    elif sl_pct not in (None, ""):
        stop_loss_kind = "pct"
        stop_loss_value = _parse_offset_positive(str(sl_pct), name="stop_loss_pct")

    stop_loss_action = str(getattr(args, "stop_loss_action", "stop_only") or "stop_only").strip().lower()
    if stop_loss_action not in ("stop_only", "stop_and_exit"):
        print("Rejected: invalid --stop-loss-action (stop_only|stop_and_exit)")
        return 2

    cleanup_policy = str(getattr(args, "cleanup_policy", "cancel-open-and-exit") or "cancel-open-and-exit").strip().lower()
    if cleanup_policy not in ("cancel-open", "none", "cancel-open-and-exit"):
        print("Rejected: invalid --cleanup-policy (cancel-open|none|cancel-open-and-exit)")
        return 2

    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)
    name = str(getattr(args, "name", "") or "").strip() or None
    notes = str(getattr(args, "notes", "") or "").strip() or None

    with connect(db_path) as conn:
        state = StateManager(conn)
        preset_id = state.create_loop_preset(
            name=name,
            notes=notes,
            symbol=symbol,
            quote_qty=str(quote_qty),
            entry_order_type=entry_order_type,
            entry_limit_price=str(entry_limit_price) if entry_limit_price is not None else None,
            take_profit_kind=take_profit_kind,
            take_profit_value=str(take_profit_value),
            rebuy_kind=rebuy_kind,
            rebuy_value=str(rebuy_value) if rebuy_value is not None else None,
            stop_loss_kind=stop_loss_kind,
            stop_loss_value=str(stop_loss_value) if stop_loss_value is not None else None,
            stop_loss_action=stop_loss_action,
            cleanup_policy=cleanup_policy,
        )
    print(f"OK preset_id={preset_id}")
    return 0


def cmd_trade_manual_loop_start(args: argparse.Namespace) -> int:
    _manual_require_human(args)
    client = _client_from_args(args)
    env = _manual_env_label(client)
    dry_run = bool(getattr(args, "dry_run", False))

    # Optional: start from a stored preset id.
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)

    preset_id = getattr(args, "id", None)
    preset: dict | None = None
    if preset_id not in (None, "", 0, "0"):
        with connect(db_path) as conn:
            preset = StateManager(conn).get_loop_preset(preset_id=int(preset_id))
        if not preset:
            print("(preset not found)")
            return 2

    if preset:
        symbol = str(preset.get("symbol") or "").strip().upper()
        quote_qty = _d_position(preset.get("quote_qty"), "quote_qty")
    else:
        symbol = str(getattr(args, "symbol", "") or "").strip().upper()
        if not symbol:
            print("Rejected: missing --symbol (or use --id)")
            return 2
        quote_qty = _d_position(getattr(args, "quote_qty", None), "quote_qty")
    if quote_qty <= 0:
        print("Rejected: --quote-qty must be > 0 (or preset quote_qty invalid)")
        return 2

    entry_order_type = str((preset.get("entry_order_type") if preset else getattr(args, "entry_type", "BUY_MARKET")) or "BUY_MARKET").strip().upper()
    if entry_order_type not in ("BUY_MARKET", "BUY_LIMIT"):
        print("Rejected: invalid --entry-type")
        return 2
    entry_limit_price: Decimal | None = None
    if entry_order_type == "BUY_LIMIT":
        raw = str((preset.get("entry_limit_price") if preset else getattr(args, "entry_limit_price", "")) or "").strip()
        if not raw:
            print("Rejected: BUY_LIMIT requires --entry-limit-price")
            return 2
        entry_limit_price = _d_position(raw, "entry_limit_price")
        if entry_limit_price <= 0:
            print("Rejected: invalid entry limit price")
            return 2

    tp_abs = (preset.get("take_profit_value") if (preset and str(preset.get("take_profit_kind") or "") == "abs") else getattr(args, "take_profit_abs", None))
    tp_pct = (preset.get("take_profit_value") if (preset and str(preset.get("take_profit_kind") or "") == "pct") else getattr(args, "take_profit_pct", None))
    if (tp_abs in (None, "")) == (tp_pct in (None, "")):
        print("Rejected: provide exactly one of --take-profit-abs or --take-profit-pct")
        return 2
    if tp_abs not in (None, ""):
        take_profit_kind = "abs"
        take_profit_value = _parse_offset_positive(str(tp_abs), name="take_profit_abs")
    else:
        take_profit_kind = "pct"
        take_profit_value = _parse_offset_positive(str(tp_pct), name="take_profit_pct")

    rebuy_abs = (preset.get("rebuy_value") if (preset and str(preset.get("rebuy_kind") or "") == "abs") else getattr(args, "rebuy_abs", None))
    rebuy_pct = (preset.get("rebuy_value") if (preset and str(preset.get("rebuy_kind") or "") == "pct") else getattr(args, "rebuy_pct", None))
    if rebuy_abs not in (None, "") and rebuy_pct not in (None, ""):
        print("Rejected: provide only one of --rebuy-abs or --rebuy-pct")
        return 2
    rebuy_kind: str | None = None
    rebuy_value: Decimal | None = None
    if rebuy_abs not in (None, ""):
        rebuy_kind = "abs"
        rebuy_value = _parse_offset_signed_default_negative(str(rebuy_abs), name="rebuy_abs")
    elif rebuy_pct not in (None, ""):
        rebuy_kind = "pct"
        rebuy_value = _parse_offset_signed_default_negative(str(rebuy_pct), name="rebuy_pct")

    sl_abs = (preset.get("stop_loss_value") if (preset and str(preset.get("stop_loss_kind") or "") == "abs") else getattr(args, "stop_loss_abs", None))
    sl_pct = (preset.get("stop_loss_value") if (preset and str(preset.get("stop_loss_kind") or "") == "pct") else getattr(args, "stop_loss_pct", None))
    if sl_abs not in (None, "") and sl_pct not in (None, ""):
        print("Rejected: provide only one of --stop-loss-abs or --stop-loss-pct")
        return 2
    stop_loss_kind: str | None = None
    stop_loss_value: Decimal | None = None
    if sl_abs not in (None, ""):
        stop_loss_kind = "abs"
        stop_loss_value = _parse_offset_positive(str(sl_abs), name="stop_loss_abs")
    elif sl_pct not in (None, ""):
        stop_loss_kind = "pct"
        stop_loss_value = _parse_offset_positive(str(sl_pct), name="stop_loss_pct")

    # stop-loss action: preset default, start can override.
    start_sla = getattr(args, "stop_loss_action", None)
    preset_sla = (preset.get("stop_loss_action") if preset else None)
    stop_loss_action = str(start_sla if start_sla not in (None, "") else (preset_sla if preset_sla not in (None, "") else "stop_only")).strip().lower()
    if stop_loss_action not in ("stop_only", "stop_and_exit"):
        print("Rejected: invalid --stop-loss-action (stop_only|stop_and_exit)")
        return 2

    start_cp = getattr(args, "cleanup_policy", None)
    preset_cp = (preset.get("cleanup_policy") if preset else None)
    cleanup_policy = str(start_cp if start_cp not in (None, "") else (preset_cp if preset_cp not in (None, "") else "cancel-open-and-exit")).strip().lower()
    if cleanup_policy not in ("cancel-open", "none", "cancel-open-and-exit"):
        print("Rejected: invalid --cleanup-policy (cancel-open|none|cancel-open-and-exit)")
        return 2

    max_cycles = int(getattr(args, "max_cycles", 1))
    if max_cycles < 0:
        print("Rejected: --max-cycles must be >= 0")
        return 2
    if max_cycles == 0 or max_cycles > 1:
        if not (rebuy_kind and rebuy_value is not None):
            print("Rejected: rebuy offset is required when --max-cycles is 0 or > 1")
            return 2

    info = client.get_symbol_info(symbol=symbol)
    if not info:
        print("(symbol not found)")
        return 2
    rules = parse_symbol_rules(info)
    if rules.status != "TRADING":
        print(f"Rejected: symbol not TRADING (status={rules.status})")
        return 2
    if not (rules.lot_size and rules.min_notional and rules.price_filter):
        print("Rejected: missing required symbol filters (LOT_SIZE/MIN_NOTIONAL/PRICE_FILTER)")
        return 2

    # Live account read for preview/balance check (even in dry-run; no side effects).
    acct = client.get_account()
    free_quote, free_base = _manual_get_free_balances(acct=acct, quote_asset=rules.quote_asset, base_asset=rules.base_asset)
    if free_quote < quote_qty and not dry_run:
        print(f"Rejected: insufficient free {rules.quote_asset}")
        return 2

    _loop_preview(
        env=env,
        base_url=str(client.base_url),
        dry_run=dry_run,
        symbol=symbol,
        quote_qty=quote_qty,
        entry_order_type=entry_order_type,
        entry_limit_price=entry_limit_price,
        take_profit_kind=take_profit_kind,
        take_profit_value=take_profit_value,
        rebuy_kind=rebuy_kind,
        rebuy_value=rebuy_value,
        stop_loss_kind=stop_loss_kind,
        stop_loss_value=stop_loss_value,
        stop_loss_action=stop_loss_action,
        cleanup_policy=cleanup_policy,
        max_cycles=max_cycles,
        rules=rules,
        free_quote=free_quote,
        free_base=free_base,
    )

    # Ensure every session links to a preset: if starting "direct", auto-create a preset first.
    auto_preset_id: int | None = None
    if not preset:
        with connect(db_path) as conn:
            state = StateManager(conn)
            auto_preset_id = state.create_loop_preset(
                name=None,
                notes="auto_created_from_start",
                symbol=symbol,
                quote_qty=str(quote_qty),
                entry_order_type=entry_order_type,
                entry_limit_price=str(entry_limit_price) if entry_limit_price is not None else None,
                take_profit_kind=take_profit_kind,
                take_profit_value=str(take_profit_value),
                rebuy_kind=rebuy_kind,
                rebuy_value=str(rebuy_value) if rebuy_value is not None else None,
                stop_loss_kind=stop_loss_kind,
                stop_loss_value=str(stop_loss_value) if stop_loss_value is not None else None,
                stop_loss_action=stop_loss_action,
                cleanup_policy=cleanup_policy,
            )
            state.append_audit(
                level="INFO",
                event="loop_preset_auto_created",
                details={"preset_id": auto_preset_id, "symbol": symbol},
            )
        preset_id = auto_preset_id

    with connect(db_path) as conn:
        state = StateManager(conn)
        loop_status = "dry_run" if dry_run else "running"
        loop_id = state.create_loop_session(
            dry_run=dry_run,
            status=loop_status,
            execution_environment=env,
            base_url=str(client.base_url),
            preset_id=int(preset_id) if preset_id not in (None, "", 0, "0") else None,
            symbol=symbol,
            quote_qty=str(quote_qty),
            entry_order_type=entry_order_type,
            entry_limit_price=str(entry_limit_price) if entry_limit_price is not None else None,
            take_profit_kind=take_profit_kind,
            take_profit_value=str(take_profit_value),
            rebuy_kind=rebuy_kind,
            rebuy_value=str(rebuy_value) if rebuy_value is not None else None,
            stop_loss_kind=stop_loss_kind,
            stop_loss_value=str(stop_loss_value) if stop_loss_value is not None else None,
            stop_loss_action=stop_loss_action,
            cleanup_policy=cleanup_policy,
            max_cycles=max_cycles,
            state="waiting_buy_fill",
            pnl_quote_asset=rules.quote_asset,
        )
        state.append_loop_event(
            loop_id=loop_id,
            event_type="loop_created",
            preset_id=int(preset_id) if preset_id not in (None, "", 0, "0") else None,
            symbol=symbol,
            message="loop_created",
            details={
                "quote_qty": str(quote_qty),
                "entry": entry_order_type,
                "take_profit": {"kind": take_profit_kind, "value": str(take_profit_value)},
                "rebuy": {"kind": rebuy_kind, "value": str(rebuy_value) if rebuy_value is not None else None},
                "stop_loss": {"kind": stop_loss_kind, "value": str(stop_loss_value) if stop_loss_value is not None else None},
                "max_cycles": max_cycles,
                "env": env,
                "dry_run": 1 if dry_run else 0,
                "preset_auto_created": True if auto_preset_id else False,
            },
        )

        state.append_loop_event(
            loop_id=loop_id,
            event_type="loop_started",
            preset_id=int(preset_id) if preset_id not in (None, "", 0, "0") else None,
            symbol=symbol,
            message="loop_started",
            details={"loop_id": loop_id, "preset_id": int(preset_id) if preset_id not in (None, "", 0, "0") else None},
        )

        if dry_run:
            print(f"OK loop_id={loop_id} status=dry_run")
            return 0

        if not getattr(args, "yes", False):
            if not _prompt_yes_no("Start loop now? (submits entry BUY)", default=False):
                state.update_loop_session(loop_id=loop_id, status="stopped", stopped_at_utc=utcnow_iso(), last_warning="cancelled_by_user")
                state.append_loop_event(loop_id=loop_id, event_type="loop_stopped", details={"reason": "cancelled_by_user"})
                print("Cancelled")
                return 2

        entry_qty_s: str | None = None
        entry_limit_px: Decimal | None = entry_limit_price
        if entry_order_type == "BUY_LIMIT":
            entry_limit_px = quantize_down(entry_limit_price or Decimal("0"), rules.price_filter.tick_size)
            raw_qty = quote_qty / entry_limit_px
            qty = quantize_down(raw_qty, rules.lot_size.step_size)
            if qty <= 0:
                state.update_loop_session(loop_id=loop_id, status="error", last_error="entry_qty_rounded_to_zero")
                print("Rejected: quantity rounded to zero")
                return 2
            if qty < rules.lot_size.min_qty:
                state.update_loop_session(loop_id=loop_id, status="error", last_error="entry_qty_below_minQty")
                print("Rejected: qty below minQty")
                return 2
            if qty * entry_limit_px < rules.min_notional.min_notional:
                state.update_loop_session(loop_id=loop_id, status="error", last_error="entry_minNotional_failed")
                print("Rejected: minNotional failed")
                return 2
            entry_qty_s = str(qty)

        client_order_id = _loop_client_order_id(loop_id=loop_id, leg_role="buy_entry")
        leg_id = state.create_loop_leg(
            loop_id=loop_id,
            cycle_index=1,
            leg_role="buy_entry",
            side="BUY",
            order_type="MARKET_BUY" if entry_order_type == "BUY_MARKET" else "LIMIT_BUY",
            time_in_force="GTC" if entry_order_type == "BUY_LIMIT" else None,
            limit_price=str(entry_limit_px) if entry_limit_px is not None else None,
            quote_order_qty=str(quote_qty),
            quantity=entry_qty_s,
            client_order_id=client_order_id,
            message="created",
        )
        state.update_loop_session(loop_id=loop_id, last_buy_leg_id=leg_id)
        state.update_loop_leg(
            leg_id=leg_id,
            local_status="submitting",
            raw_status=None,
            binance_order_id=None,
            retry_count=0,
            executed_quantity=None,
            avg_fill_price=None,
            total_quote_value=None,
            fee_breakdown_json=None,
            message="submitting",
            submitted_at_utc=utcnow_iso(),
        )

        # Submit entry order.
        try:
            if entry_order_type == "BUY_MARKET":
                order, retry_count = _loop_submit_with_idempotency(
                    client=client,
                    state=state,
                    leg_id=leg_id,
                    symbol=symbol,
                    client_order_id=client_order_id,
                    submit_fn=client.create_order_market_buy_quote,
                    submit_kwargs={"symbol": symbol, "quote_order_qty": str(quote_qty), "client_order_id": client_order_id},
                )
            else:
                if entry_limit_px is None or entry_qty_s is None:
                    raise ValueError("missing entry limit sizing")
                order, retry_count = _loop_submit_with_idempotency(
                    client=client,
                    state=state,
                    leg_id=leg_id,
                    symbol=symbol,
                    client_order_id=client_order_id,
                    submit_fn=client.create_order_limit_buy,
                    submit_kwargs={
                        "symbol": symbol,
                        "price": str(entry_limit_px),
                        "quantity": str(entry_qty_s),
                        "client_order_id": client_order_id,
                        "time_in_force": "GTC",
                    },
                )
        except Exception as e:
            state.update_loop_leg(
                leg_id=leg_id,
                local_status="failed",
                raw_status=None,
                binance_order_id=None,
                retry_count=0,
                executed_quantity=None,
                avg_fill_price=None,
                total_quote_value=None,
                fee_breakdown_json=None,
                message=str(e),
            )
            state.update_loop_session(loop_id=loop_id, status="error", last_error=str(e))
            state.append_loop_event(loop_id=loop_id, event_type="entry_submit_failed", details={"error": str(e)})
            print(f"ERROR: {e}")
            return 2

        _loop_finalize_leg_from_order(state=state, leg_id=leg_id, order=order, retry_count=retry_count)
        state.append_loop_event(
            loop_id=loop_id,
            event_type="entry_order_submitted",
            preset_id=int(preset_id) if preset_id not in (None, "", 0, "0") else None,
            symbol=symbol,
            side="BUY",
            cycle_number=1,
            client_order_id=client_order_id,
            binance_order_id=str(order.get("orderId") or "") or None,
            price=str(entry_limit_px) if entry_limit_px is not None else None,
            quantity=entry_qty_s if entry_qty_s is not None else str(quote_qty),
            message="submitted",
            details={"leg_id": leg_id, "order_type": "MARKET_BUY" if entry_order_type == "BUY_MARKET" else "LIMIT_BUY"},
        )

        # Best-effort post sync (balances + open orders).
        try:
            sync_balances(client=client, conn=conn)
        except Exception:
            pass
        try:
            sync_open_orders(client=client, conn=conn, symbol=symbol)
        except Exception:
            pass

    print(f"OK loop_id={loop_id} status=running")

    # Normal UX: run the loop runner immediately so users don't need to call reconcile manually.
    if bool(getattr(args, "no_run", False)):
        return 0
    # Hand off to the reconcile runner (advanced command) in loop mode.
    runner_args = argparse.Namespace(
        config=args.config,
        db=args.db,
        ca_bundle=getattr(args, "ca_bundle", None),
        insecure=getattr(args, "insecure", False),
        testnet=getattr(args, "testnet", False),
        base_url=getattr(args, "base_url", None),
        i_am_human=True,
        loop_id=loop_id,
        loop=True,
        interval_seconds=int(getattr(args, "interval_seconds", 6)),
        duration_seconds=getattr(args, "duration_seconds", None),
    )
    return cmd_trade_manual_loop_reconcile(runner_args)


def cmd_trade_manual_loop_list(args: argparse.Namespace) -> int:
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)
    limit = int(getattr(args, "limit", 20))
    with connect(db_path) as conn:
        rows = StateManager(conn).list_loop_sessions(limit=limit)
    if not rows:
        print("(no loop sessions)")
        return 0
    print(f"Loop sessions: {len(rows)}")
    print(f"{'ID':>4} {'DRY':>3} {'ENV':<7} {'SYMBOL':<10} {'STATUS':<10} {'CYC':>3} {'MAX':>3} {'QUOTE_QTY':>10} {'PNL':>14} {'ASSET':<5} {'ERR':<10}")
    for r in rows:
        pnl = r.get("cumulative_realized_pnl_quote") or "-"
        asset = r.get("pnl_quote_asset") or "-"
        err = str(r.get("last_error") or "")
        if len(err) > 10:
            err = err[:7] + "..."
        print(
            f"{int(r.get('loop_id') or 0):>4} {int(r.get('dry_run') or 0):>3} {str(r.get('execution_environment') or '-'): <7} "
            f"{str(r.get('symbol') or '-'): <10} {str(r.get('status') or '-'): <10} "
            f"{int(r.get('cycles_completed') or 0):>3} {int(r.get('max_cycles') or 0):>3} "
            f"{str(r.get('quote_qty') or '-'):>10} {str(pnl):>14} {str(asset): <5} {err: <10}"
        )
    return 0


def _resolve_loop_id(*, state: StateManager, loop_id: int | None) -> int | None:
    if loop_id not in (None, 0, "0"):
        return int(loop_id)
    row = state.get_latest_loop_session(status="running")
    if row:
        return int(row["loop_id"])
    row = state.get_latest_loop_session()
    if row:
        return int(row["loop_id"])
    return None


def cmd_trade_manual_loop_status(args: argparse.Namespace) -> int:
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)
    with connect(db_path) as conn:
        state = StateManager(conn)
        loop_id = _resolve_loop_id(state=state, loop_id=getattr(args, "loop_id", None))
        if not loop_id:
            print("(no loop sessions)")
            return 2
        loop = state.get_loop_session(loop_id=loop_id)
        if not loop:
            print("(loop not found)")
            return 2
        last_leg = state.get_latest_loop_leg(loop_id=loop_id)
    print("Loop Status")
    print(f"- Loop ID: {loop_id}")
    print(f"- Status: {loop.get('status')}")
    print(f"- State: {loop.get('state')}")
    print(f"- Symbol: {loop.get('symbol')}")
    print(f"- Env: {loop.get('execution_environment')}")
    print(f"- Quote qty: {loop.get('quote_qty')}")
    print(f"- Cycles: {loop.get('cycles_completed')}/{loop.get('max_cycles')}")
    if loop.get("last_buy_avg_price"):
        print(f"- Last BUY avg: {loop.get('last_buy_avg_price')}")
    if loop.get("last_sell_avg_price"):
        print(f"- Last SELL avg: {loop.get('last_sell_avg_price')}")
    if loop.get("cumulative_realized_pnl_quote"):
        print(f"- Cum realized PnL: {loop.get('cumulative_realized_pnl_quote')} {loop.get('pnl_quote_asset')}")
    if loop.get("last_warning"):
        print(f"- Warning: {loop.get('last_warning')}")
    if loop.get("last_error"):
        print(f"- Error: {loop.get('last_error')}")
    if last_leg:
        print(f"- Active leg: leg_id={last_leg.get('leg_id')} role={last_leg.get('leg_role')} type={last_leg.get('order_type')} status={last_leg.get('local_status')}")
        if last_leg.get("binance_order_id"):
            print(f"- Binance order id: {last_leg.get('binance_order_id')}")
        if last_leg.get("client_order_id"):
            print(f"- Client order id: {last_leg.get('client_order_id')}")
        if last_leg.get("limit_price"):
            print(f"- Limit price: {last_leg.get('limit_price')}")
        if last_leg.get("quantity"):
            print(f"- Quantity: {last_leg.get('quantity')}")
        if last_leg.get("quote_order_qty"):
            print(f"- Quote qty: {last_leg.get('quote_order_qty')}")
    return 0


def _loop_apply_cleanup_policy(
    *,
    client: BinanceSpotClient,
    state: StateManager,
    loop: dict,
    rules: SymbolRules,
    reason: str,
    exit_already_done: bool = False,
    allow_exit: bool = True,
) -> None:
    """
    Order Cleanup Policy (configurable, default cancel-open-and-exit):
      - cancel-open: cancel open loop-created orders
      - none: do nothing
      - cancel-open-and-exit: cancel open loop orders, then MARKET SELL remaining base balance
    """
    loop_id = int(loop.get("loop_id") or loop.get("id") or 0)
    if loop_id <= 0:
        return
    policy = str(loop.get("cleanup_policy") or "cancel-open").strip().lower()
    if policy not in ("cancel-open", "none", "cancel-open-and-exit"):
        policy = "cancel-open"

    preset_ref = None
    try:
        pid = loop.get("preset_id")
        if pid not in (None, "", 0, "0"):
            preset_ref = int(pid)
    except Exception:
        preset_ref = None

    symbol = str(loop.get("symbol") or "").strip().upper()

    def _ev(event_type: str, **kw) -> None:
        try:
            state.append_loop_event(loop_id=loop_id, event_type=event_type, preset_id=preset_ref, symbol=symbol, details=kw)
        except Exception:
            return

    if policy == "none":
        _ev("cleanup_policy_none", reason=reason)
        return

    # Cancel all open loop legs that are cancelable (LIMIT types).
    open_legs = state.list_loop_legs_open(loop_id=loop_id, limit=500)
    cancelled = 0
    for leg in open_legs:
        ot = str(leg.get("order_type") or "")
        coid = str(leg.get("client_order_id") or "").strip()
        if not coid:
            continue
        if "LIMIT" not in ot:
            continue
        try:
            client.cancel_order_by_client_order_id(symbol=symbol, client_order_id=coid)
            state.update_loop_leg(
                leg_id=int(leg.get("leg_id") or 0),
                local_status="cancelled",
                raw_status="CANCELED",
                binance_order_id=str(leg.get("binance_order_id") or "") or None,
                retry_count=int(leg.get("retry_count") or 0),
                executed_quantity=leg.get("executed_quantity"),
                avg_fill_price=leg.get("avg_fill_price"),
                total_quote_value=leg.get("total_quote_value"),
                fee_breakdown_json=leg.get("fee_breakdown_json"),
                message=f"cleanup_cancelled:{reason}",
            )
            cancelled += 1
            _ev("cleanup_cancelled_open_order", leg_id=int(leg.get("leg_id") or 0), client_order_id=coid, order_type=ot)
        except Exception as e:
            _ev("cleanup_cancel_failed", leg_id=int(leg.get("leg_id") or 0), client_order_id=coid, error=str(e))

    _ev("cleanup_cancel_open_done", cancelled=cancelled, policy=policy, reason=reason)

    if policy != "cancel-open-and-exit" or exit_already_done or (not allow_exit):
        return

    # Exit the remaining position using a MARKET SELL (quote slippage accepted).
    try:
        acct = client.get_account()
        _, free_base = _manual_get_free_balances(acct=acct, quote_asset=rules.quote_asset, base_asset=rules.base_asset)
        live_price = _d_position(client.get_ticker_price(symbol=symbol), "ticker_price")
        qty = quantize_down(free_base, rules.lot_size.step_size) if rules.lot_size else free_base
        if not rules.lot_size or not rules.min_notional:
            _ev("cleanup_exit_skipped", reason="missing_symbol_filters")
            return
        if qty <= 0 or qty < rules.lot_size.min_qty:
            _ev("cleanup_exit_skipped", reason="qty_below_minQty", qty=str(qty), free_base=str(free_base))
            return
        if qty * live_price < rules.min_notional.min_notional:
            _ev("cleanup_exit_skipped", reason="minNotional_failed", qty=str(qty), price=str(live_price))
            return

        client_order_id = _loop_client_order_id(loop_id=loop_id, leg_role="sell_cleanup")
        leg_id = state.create_loop_leg(
            loop_id=loop_id,
            cycle_index=int(loop.get("cycles_completed") or 0) + 1,
            leg_role="sell_cleanup_exit",
            side="SELL",
            order_type="MARKET_SELL",
            time_in_force=None,
            limit_price=None,
            quote_order_qty=None,
            quantity=str(qty),
            client_order_id=client_order_id,
            message="created",
        )
        state.update_loop_leg(
            leg_id=leg_id,
            local_status="submitting",
            raw_status=None,
            binance_order_id=None,
            retry_count=0,
            executed_quantity=None,
            avg_fill_price=None,
            total_quote_value=None,
            fee_breakdown_json=None,
            message="submitting_cleanup_exit",
            submitted_at_utc=utcnow_iso(),
        )
        order, rc = _loop_submit_with_idempotency(
            client=client,
            state=state,
            leg_id=leg_id,
            symbol=symbol,
            client_order_id=client_order_id,
            submit_fn=client.create_order_market_sell_qty,
            submit_kwargs={"symbol": symbol, "quantity": str(qty), "client_order_id": client_order_id},
        )
        _loop_finalize_leg_from_order(state=state, leg_id=leg_id, order=order, retry_count=rc)
        _ev("cleanup_exit_submitted", leg_id=leg_id, qty=str(qty), price=str(live_price), policy=policy, reason=reason)
    except Exception as e:
        _ev("cleanup_exit_failed", error=str(e), policy=policy, reason=reason)


def cmd_trade_manual_loop_stop(args: argparse.Namespace) -> int:
    _manual_require_human(args)
    client = _client_from_args(args)
    env = _manual_env_label(client)

    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)
    with connect(db_path) as conn:
        state = StateManager(conn)
        loop_id = _resolve_loop_id(state=state, loop_id=getattr(args, "loop_id", None))
        if not loop_id:
            print("(no loop sessions)")
            return 2
        loop = state.get_loop_session(loop_id=loop_id)
        if not loop:
            print("(loop not found)")
            return 2
        if str(loop.get("execution_environment") or "").strip().lower() != env:
            print(f"Rejected: environment mismatch loop={loop.get('execution_environment')} runtime={env}")
            return 2
        # Capture current state so we can print actions taken during stop/cleanup.
        try:
            cur = conn.execute("SELECT COALESCE(MAX(leg_id), 0) FROM loop_legs WHERE loop_id = ?", (int(loop_id),))
            row = cur.fetchone()
            before_max_leg_id = int((row[0] if row else 0) or 0)
        except Exception:
            before_max_leg_id = 0
        try:
            open_before = state.list_loop_legs_open(loop_id=loop_id, limit=500)
        except Exception:
            open_before = []
        leg = state.get_latest_loop_leg(loop_id=loop_id)
        if not leg:
            state.update_loop_session(loop_id=loop_id, status="stopped", stopped_at_utc=utcnow_iso())
            state.append_loop_event(loop_id=loop_id, event_type="loop_stopped", details={"reason": "no_legs"})
            print("Stopped")
            return 0
        symbol = str(loop.get("symbol") or "").strip().upper()
        if not _prompt_yes_no("Stop loop now and apply cleanup policy?", default=False):
            print("Cancelled")
            return 2

        # Apply cleanup policy (cancel loop-created open orders; optionally exit).
        info = client.get_symbol_info(symbol=symbol)
        if info:
            rules = parse_symbol_rules(info)
            if rules.lot_size and rules.min_notional and rules.price_filter:
                _loop_apply_cleanup_policy(client=client, state=state, loop=loop, rules=rules, reason="manual_stop")

        state.update_loop_session(loop_id=loop_id, status="stopped", stopped_at_utc=utcnow_iso())
        state.append_loop_event(
            loop_id=loop_id,
            event_type="loop_stopped",
            preset_id=int(loop.get("preset_id")) if loop.get("preset_id") not in (None, "", 0, "0") else None,
            symbol=symbol,
            message="user_stop",
            details={"reason": "user_stop"},
        )
        # Best-effort sync.
        try:
            sync_balances(client=client, conn=conn)
        except Exception:
            pass
        try:
            sync_open_orders(client=client, conn=conn, symbol=symbol)
        except Exception:
            pass
        # Print actions taken during stop (derived from loop_legs).
        cancelled = 0
        cancel_failed = 0
        cancelled_details: list[str] = []
        for b in open_before:
            try:
                bid = int(b.get("leg_id") or 0)
                if bid <= 0:
                    continue
                after = state.get_loop_leg(leg_id=bid) or {}
                before_status = str(b.get("local_status") or "")
                after_status = str(after.get("local_status") or before_status)
                if before_status != after_status:
                    if after_status == "cancelled":
                        cancelled += 1
                        cancelled_details.append(f"leg_id={bid} client_order_id={b.get('client_order_id')}")
                    elif after_status == "failed":
                        cancel_failed += 1
            except Exception:
                continue

        # Detect cleanup exit leg if created.
        exit_leg = None
        try:
            cur = conn.execute(
                """
                SELECT leg_id, side, order_type, local_status, client_order_id, binance_order_id, quantity, message
                FROM loop_legs
                WHERE loop_id = ?
                  AND leg_id > ?
                  AND leg_role IN ('sell_cleanup_exit')
                ORDER BY leg_id ASC
                LIMIT 1
                """,
                (int(loop_id), int(before_max_leg_id)),
            )
            r = cur.fetchone()
            if r:
                # sqlite Row supports index access; convert to dict-like fields.
                exit_leg = {
                    "leg_id": r[0],
                    "side": r[1],
                    "order_type": r[2],
                    "local_status": r[3],
                    "client_order_id": r[4],
                    "binance_order_id": r[5],
                    "quantity": r[6],
                    "message": r[7],
                }
        except Exception:
            exit_leg = None

        print("Actions")
        print(f"- cleanup: open_before={len(open_before)} cancelled={cancelled} failed={cancel_failed}")
        for d in cancelled_details[:10]:
            print(f"- cleanup: cancelled {d}")
        if len(cancelled_details) > 10:
            print(f"- cleanup: cancelled …(+{len(cancelled_details) - 10} more)")
        if exit_leg:
            print(
                f"- cleanup: exit_leg leg_id={exit_leg.get('leg_id')} status={exit_leg.get('local_status')} qty={exit_leg.get('quantity')} order_id={exit_leg.get('binance_order_id')}"
            )
        print("- loop: stopped (user_stop)")
    print(f"OK stopped loop_id={loop_id}")
    return 0


def _offset_apply_price(*, base_price: Decimal, kind: str, value: Decimal) -> Decimal:
    if kind == "abs":
        return base_price + value
    if kind == "pct":
        return base_price * (Decimal("1") + (value / Decimal("100")))
    raise ValueError("invalid offset kind")


def _offset_apply_price_take_profit(*, buy_price: Decimal, kind: str, value: Decimal) -> Decimal:
    # Always above buy price.
    if kind == "abs":
        return buy_price + value
    if kind == "pct":
        return buy_price * (Decimal("1") + (value / Decimal("100")))
    raise ValueError("invalid take-profit kind")


def cmd_trade_manual_loop_reconcile(args: argparse.Namespace) -> int:
    _manual_require_human(args)
    client = _client_from_args(args)
    env = _manual_env_label(client)
    loop_flag = bool(getattr(args, "loop", False))
    interval = int(getattr(args, "interval_seconds", 60))
    duration = getattr(args, "duration_seconds", None)
    if interval <= 0:
        print("Invalid interval_seconds")
        return 2
    end_at = None
    if duration not in (None, ""):
        try:
            duration_s = int(duration)
        except Exception:
            print("Invalid duration_seconds")
            return 2
        if duration_s <= 0:
            print("Invalid duration_seconds")
            return 2
        end_at = time.monotonic() + duration_s

    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)

    def _run_once(conn) -> tuple[int, int, int, int]:
        state = StateManager(conn)
        loop_id = _resolve_loop_id(state=state, loop_id=getattr(args, "loop_id", None))
        if not loop_id:
            return (0, 0, 0, 0)
        loop = state.get_loop_session(loop_id=loop_id)
        if not loop:
            return (0, 0, 0, 0)
        if str(loop.get("execution_environment") or "").strip().lower() != env:
            raise ValueError(f"environment mismatch loop={loop.get('execution_environment')} runtime={env}")
        # Global pause or loop-level pause halts loop runner.
        try:
            sys_state = state.get_system_state() or {}
            if bool(sys_state.get("automation_paused") or 0):
                state.append_loop_event(loop_id=loop_id, event_type="loop_paused", details={"reason": "global_pause"})
                return (loop_id, 0, 1, 0)
            if state.is_loop_paused(loop_id=loop_id):
                state.append_loop_event(loop_id=loop_id, event_type="loop_paused", details={"reason": "loop_pause"})
                return (loop_id, 0, 1, 0)
        except Exception:
            pass
        if str(loop.get("status") or "") not in ("running",):
            return (loop_id, 0, 0, 0)

        symbol = str(loop.get("symbol") or "").strip().upper()
        preset_ref = None
        try:
            pid = loop.get("preset_id")
            if pid not in (None, "", 0, "0"):
                preset_ref = int(pid)
        except Exception:
            preset_ref = None

        def _ev(
            event_type: str,
            *,
            side: str | None = None,
            cycle_number: int | None = None,
            client_order_id: str | None = None,
            binance_order_id: str | None = None,
            price: str | None = None,
            quantity: str | None = None,
            message: str | None = None,
            details: dict | None = None,
        ) -> None:
            try:
                state.append_loop_event(
                    loop_id=loop_id,
                    event_type=event_type,
                    preset_id=preset_ref,
                    symbol=symbol,
                    side=side,
                    cycle_number=cycle_number,
                    client_order_id=client_order_id,
                    binance_order_id=binance_order_id,
                    price=price,
                    quantity=quantity,
                    message=message,
                    details=details,
                )
            except Exception:
                return

        info = client.get_symbol_info(symbol=symbol)
        if not info:
            state.update_loop_session(loop_id=loop_id, status="error", last_error="symbol_not_found")
            _ev("reconcile_error", message="symbol_not_found", details={"error": "symbol_not_found"})
            return (loop_id, 0, 1, 1)
        rules = parse_symbol_rules(info)
        if not (rules.lot_size and rules.min_notional and rules.price_filter):
            state.update_loop_session(loop_id=loop_id, status="error", last_error="missing_symbol_filters")
            _ev("reconcile_error", message="missing_symbol_filters", details={"error": "missing_symbol_filters"})
            return (loop_id, 0, 1, 1)

        # Track the exact leg implied by the loop state (even if it already filled during submission).
        track_leg_id = None
        st = str(loop.get("state") or "").strip().lower()
        if st == "waiting_buy_fill":
            track_leg_id = loop.get("last_buy_leg_id")
        elif st == "waiting_sell_fill":
            track_leg_id = loop.get("last_sell_leg_id")
        if track_leg_id not in (None, "", 0, "0"):
            leg = state.get_loop_leg(leg_id=int(track_leg_id))
        else:
            leg = state.get_latest_loop_leg(loop_id=loop_id)
        if not leg:
            return (loop_id, 0, 0, 0)
        leg_id = int(leg.get("leg_id") or 0)
        client_order_id = str(leg.get("client_order_id") or "").strip()
        if not client_order_id:
            state.update_loop_session(loop_id=loop_id, status="error", last_error="missing_client_order_id")
            return (loop_id, 0, 1, 1)

        # Stop-loss check (based on last BUY avg price).
        sl_kind = str(loop.get("stop_loss_kind") or "").strip().lower()
        sl_val = loop.get("stop_loss_value")
        last_buy_avg_s = str(loop.get("last_buy_avg_price") or "").strip()
        if sl_kind in ("abs", "pct") and sl_val not in (None, "") and last_buy_avg_s:
            try:
                last_buy_avg = _d_position(last_buy_avg_s, "last_buy_avg_price")
                sl_value = _d_position(str(sl_val), "stop_loss_value")
                live_price = _d_position(client.get_ticker_price(symbol=symbol), "ticker_price")
                if sl_kind == "abs":
                    stop_px = last_buy_avg - sl_value
                else:
                    stop_px = last_buy_avg * (Decimal("1") - (sl_value / Decimal("100")))
                if live_price <= stop_px:
                    # Stop loop and cancel current open limit order (best-effort).
                    try:
                        client.cancel_order_by_client_order_id(symbol=symbol, client_order_id=client_order_id)
                    except Exception:
                        pass
                    sla = str(loop.get("stop_loss_action") or "stop_only").strip().lower()
                    # Optional exit: market sell the held asset (configurable).
                    if sla == "stop_and_exit":
                        # Determine sellable quantity (Binance free balance is source of truth).
                        acct2 = client.get_account()
                        free_q2, free_b2 = _manual_get_free_balances(
                            acct=acct2, quote_asset=rules.quote_asset, base_asset=rules.base_asset
                        )
                        # Prefer the loop's last buy net qty if available; clamp to free balance.
                        target_qty = free_b2
                        try:
                            if loop.get("last_buy_executed_qty") not in (None, ""):
                                target_qty = min(target_qty, _d_position(str(loop.get("last_buy_executed_qty")), "last_buy_executed_qty"))
                        except Exception:
                            target_qty = free_b2
                        sell_qty = quantize_down(target_qty, rules.lot_size.step_size)
                        if sell_qty > 0 and sell_qty >= rules.lot_size.min_qty:
                            # Create a SELL MARKET leg (protective exit) and submit.
                            sell_client_id = _loop_client_order_id(loop_id=loop_id, leg_role="sell_sl")
                            sell_leg_id = state.create_loop_leg(
                                loop_id=loop_id,
                                cycle_index=int(loop.get("cycles_completed") or 0) + 1,
                                leg_role="sell_sl_exit",
                                side="SELL",
                                order_type="MARKET_SELL",
                                time_in_force=None,
                                limit_price=None,
                                quote_order_qty=None,
                                quantity=str(sell_qty),
                                client_order_id=sell_client_id,
                                message="created",
                            )
                            state.update_loop_leg(
                                leg_id=sell_leg_id,
                                local_status="submitting",
                                raw_status=None,
                                binance_order_id=None,
                                retry_count=0,
                                executed_quantity=None,
                                avg_fill_price=None,
                                total_quote_value=None,
                                fee_breakdown_json=None,
                                message="submitting_stop_loss_exit",
                                submitted_at_utc=utcnow_iso(),
                            )
                            try:
                                order_exit, rc_exit = _loop_submit_with_idempotency(
                                    client=client,
                                    state=state,
                                    leg_id=sell_leg_id,
                                    symbol=symbol,
                                    client_order_id=sell_client_id,
                                    submit_fn=client.create_order_market_sell_qty,
                                    submit_kwargs={"symbol": symbol, "quantity": str(sell_qty), "client_order_id": sell_client_id},
                                )
                                _loop_finalize_leg_from_order(state=state, leg_id=sell_leg_id, order=order_exit, retry_count=rc_exit)
                                _ev(
                                    "stop_loss_exit_submitted",
                                    side="SELL",
                                    cycle_number=int(loop.get("cycles_completed") or 0) + 1,
                                    client_order_id=sell_client_id,
                                    binance_order_id=str(order_exit.get("orderId") or "") or None,
                                    quantity=str(sell_qty),
                                    message="submitted",
                                    details={"leg_id": sell_leg_id, "free_base": str(free_b2), "free_quote": str(free_q2)},
                                )
                            except Exception as e:
                                state.update_loop_session(loop_id=loop_id, status="error", last_error=f"stop_loss_exit_failed:{e}")
                                _ev("stop_loss_exit_failed", message=str(e), details={"error": str(e)})

                    # Apply cleanup policy for stop-loss, without overriding stop-loss action.
                    # - stop_only: cancel-open only (no exit)
                    # - stop_and_exit: exit already performed above (skip exit here)
                    try:
                        _loop_apply_cleanup_policy(
                            client=client,
                            state=state,
                            loop=loop,
                            rules=rules,
                            reason="stop_loss",
                            exit_already_done=(sla == "stop_and_exit"),
                            allow_exit=(sla == "stop_and_exit"),
                        )
                    except Exception:
                        pass

                    state.update_loop_session(loop_id=loop_id, status="stopped", stopped_at_utc=utcnow_iso(), last_warning="stop_loss_triggered")
                    _ev(
                        "stop_loss_triggered",
                        message=f"stop_loss_triggered action={sla}",
                        details={"live_price": str(live_price), "stop_price": str(stop_px), "ref_buy_avg": str(last_buy_avg)},
                    )
                    return (loop_id, 0, 1, 0)
            except Exception:
                pass

        updated = 0
        prev_local = str(leg.get("local_status") or "")
        # Refresh from exchange only when the leg is non-terminal.
        if str(leg.get("local_status") or "") in ("created", "submitting", "submitted", "open", "partially_filled", "uncertain_submitted", "retry_submitted"):
            order = client.get_order_by_client_order_id(symbol=symbol, client_order_id=client_order_id)
            _loop_finalize_leg_from_order(state=state, leg_id=leg_id, order=order, retry_count=int(leg.get("retry_count") or 0))
            updated = 1
            leg = state.get_loop_leg(leg_id=leg_id) or leg
            new_local = str(leg.get("local_status") or "")
            if new_local != prev_local:
                _ev(
                    "order_status_changed",
                    side=str(leg.get("side") or "").strip().upper() or None,
                    cycle_number=int(leg.get("cycle_index") or 0) if str(leg.get("cycle_index") or "").strip() else None,
                    client_order_id=client_order_id,
                    binance_order_id=str(leg.get("binance_order_id") or "") or None,
                    price=str(leg.get("limit_price") or "") or None,
                    quantity=str(leg.get("quantity") or leg.get("quote_order_qty") or "") or None,
                    message=f"{prev_local}->{new_local}",
                    details={"leg_id": leg_id, "prev": prev_local, "new": new_local},
                )
                if new_local == "partially_filled":
                    _ev("partial_fill_detected", message="partial_fill_detected", details={"leg_id": leg_id})
                if new_local == "filled":
                    _ev("order_filled", message="filled", details={"leg_id": leg_id})

        if str(leg.get("local_status") or "") != "filled":
            if updated:
                _ev(
                    "reconcile_tick",
                    side=str(leg.get("side") or "").strip().upper() or None,
                    cycle_number=int(leg.get("cycle_index") or 0) if str(leg.get("cycle_index") or "").strip() else None,
                    client_order_id=client_order_id,
                    binance_order_id=str(leg.get("binance_order_id") or "") or None,
                    message=f"tick status={leg.get('local_status')}",
                    details={"leg_id": leg_id, "status": str(leg.get("local_status") or "")},
                )
            return (loop_id, updated, 0, 0)

        # Build fill-like values from persisted leg fields (no extra network required).
        fills = None
        try:
            # Reuse parser for consistent semantics when we have FULL payload; otherwise reconstruct.
            if "order" in locals():
                fills = parse_fills(order)  # type: ignore[name-defined]
        except Exception:
            fills = None
        if fills is None:
            ex_qty = _d_position(str(leg.get("executed_quantity") or "0"), "executed_quantity")
            total_quote = _d_position(str(leg.get("total_quote_value") or "0"), "total_quote_value")
            avg = None
            try:
                avg_s = str(leg.get("avg_fill_price") or "").strip()
                if avg_s:
                    avg = _d_position(avg_s, "avg_fill_price")
                elif ex_qty > 0:
                    avg = total_quote / ex_qty
            except Exception:
                avg = (total_quote / ex_qty) if ex_qty > 0 else None
            fee_breakdown = {}
            try:
                fb = _json.loads(str(leg.get("fee_breakdown_json") or "{}"))
                if isinstance(fb, dict):
                    fee_breakdown = {str(k).strip().upper(): str(v) for k, v in fb.items()}
            except Exception:
                fee_breakdown = {}

            class _F:
                executed_qty = ex_qty
                total_quote_spent = total_quote
                avg_fill_price = avg
                commission_total = None
                commission_asset = None
                commission_breakdown = fee_breakdown

            fills = _F()  # type: ignore[assignment]

        fee_breakdown = fills.commission_breakdown or {}
        base_fee = Decimal("0")
        quote_fee = Decimal("0")
        try:
            bf = fee_breakdown.get(rules.base_asset)
            if bf not in (None, ""):
                base_fee = _d_position(bf, "base_fee")
        except Exception:
            base_fee = Decimal("0")
        try:
            qf = fee_breakdown.get(rules.quote_asset)
            if qf not in (None, ""):
                quote_fee = _d_position(qf, "quote_fee")
        except Exception:
            quote_fee = Decimal("0")

        if str(leg.get("side") or "").upper() == "BUY":
            # Compute fee-aware avg entry price (Average Cost).
            net_qty = fills.executed_qty - base_fee
            if net_qty <= 0:
                state.update_loop_session(loop_id=loop_id, status="error", last_error="net_qty_nonpositive_after_fee")
                return (loop_id, updated, 1, 1)
            cost_basis_quote = fills.total_quote_spent + quote_fee
            avg_entry = cost_basis_quote / net_qty
            state.update_loop_session(
                loop_id=loop_id,
                last_buy_leg_id=leg_id,
                last_buy_avg_price=str(avg_entry),
                last_buy_executed_qty=str(net_qty),
                state="waiting_sell_fill",
            )
            # Build and submit SELL LIMIT.
            tp_kind = str(loop.get("take_profit_kind") or "")
            tp_value = _d_position(str(loop.get("take_profit_value") or "0"), "take_profit_value")
            sell_target = _offset_apply_price_take_profit(buy_price=avg_entry, kind=tp_kind, value=tp_value)
            sell_px = quantize_down(sell_target, rules.price_filter.tick_size)
            sell_qty = quantize_down(net_qty, rules.lot_size.step_size)
            if sell_qty <= 0 or sell_qty < rules.lot_size.min_qty:
                state.update_loop_session(loop_id=loop_id, status="stopped", stopped_at_utc=utcnow_iso(), last_warning="sell_qty_below_minQty")
                _ev(
                    "sell_not_possible",
                    side="SELL",
                    cycle_number=int(loop.get("cycles_completed") or 0) + 1,
                    message="sell_qty_below_minQty",
                    details={"net_qty": str(net_qty), "sell_qty": str(sell_qty)},
                )
                return (loop_id, updated, 1, 0)
            if sell_qty * sell_px < rules.min_notional.min_notional:
                state.update_loop_session(loop_id=loop_id, status="stopped", stopped_at_utc=utcnow_iso(), last_warning="sell_minNotional_failed")
                _ev(
                    "sell_not_possible",
                    side="SELL",
                    cycle_number=int(loop.get("cycles_completed") or 0) + 1,
                    message="sell_minNotional_failed",
                    details={"sell_qty": str(sell_qty), "sell_px": str(sell_px)},
                )
                return (loop_id, updated, 1, 0)
            sell_client_id = _loop_client_order_id(loop_id=loop_id, leg_role="sell_tp")
            sell_leg_id = state.create_loop_leg(
                loop_id=loop_id,
                cycle_index=int(loop.get("cycles_completed") or 0) + 1,
                leg_role="sell_tp",
                side="SELL",
                order_type="LIMIT_SELL",
                time_in_force="GTC",
                limit_price=str(sell_px),
                quote_order_qty=None,
                quantity=str(sell_qty),
                client_order_id=sell_client_id,
                message="created",
            )
            state.update_loop_session(loop_id=loop_id, last_sell_leg_id=sell_leg_id)
            state.update_loop_leg(
                leg_id=sell_leg_id,
                local_status="submitting",
                raw_status=None,
                binance_order_id=None,
                retry_count=0,
                executed_quantity=None,
                avg_fill_price=None,
                total_quote_value=None,
                fee_breakdown_json=None,
                message="submitting",
                submitted_at_utc=utcnow_iso(),
            )
            order2, rc2 = _loop_submit_with_idempotency(
                client=client,
                state=state,
                leg_id=sell_leg_id,
                symbol=symbol,
                client_order_id=sell_client_id,
                submit_fn=client.create_order_limit_sell,
                submit_kwargs={"symbol": symbol, "price": str(sell_px), "quantity": str(sell_qty), "client_order_id": sell_client_id, "time_in_force": "GTC"},
            )
            _loop_finalize_leg_from_order(state=state, leg_id=sell_leg_id, order=order2, retry_count=rc2)
            _ev(
                "sell_order_submitted",
                side="SELL",
                cycle_number=int(loop.get("cycles_completed") or 0) + 1,
                client_order_id=sell_client_id,
                binance_order_id=str(order2.get("orderId") or "") or None,
                price=str(sell_px),
                quantity=str(sell_qty),
                message="submitted",
                details={"leg_id": sell_leg_id, "order_type": "LIMIT_SELL"},
            )
            return (loop_id, updated + 1, 0, 0)

        # SELL filled.
        sell_avg = fills.avg_fill_price or (fills.total_quote_spent / fills.executed_qty if fills.executed_qty > 0 else None)
        if sell_avg is None:
            sell_avg = Decimal("0")
        state.update_loop_session(loop_id=loop_id, last_sell_leg_id=leg_id, last_sell_avg_price=str(sell_avg), last_sell_executed_qty=str(fills.executed_qty))

        # Realized PnL for this cycle (quote asset).
        warnings: list[str] = []
        proceeds_quote = fills.total_quote_spent
        realized = None
        try:
            buy_avg_s = str(loop.get("last_buy_avg_price") or "").strip()
            buy_avg = _d_position(buy_avg_s, "last_buy_avg_price")
            quote_fee_sell = Decimal("0")
            try:
                qf2 = fee_breakdown.get(rules.quote_asset)
                if qf2 not in (None, ""):
                    quote_fee_sell = _d_position(qf2, "sell_quote_fee")
            except Exception:
                quote_fee_sell = Decimal("0")
            realized = proceeds_quote - (fills.executed_qty * buy_avg) - quote_fee_sell
            # Non-base/non-quote fee warning.
            for a in fee_breakdown.keys():
                a = str(a or "").strip().upper()
                if a and a not in (rules.base_asset, rules.quote_asset):
                    warnings.append("realized_pnl_excludes_non_quote_fee_conversion")
                    warnings.append(f"fee_asset_non_base_non_quote:{a}")
                    break
        except Exception as e:
            warnings.append(f"realized_pnl_error:{e}")

        cum = Decimal("0")
        try:
            if loop.get("cumulative_realized_pnl_quote") not in (None, ""):
                cum = _d_position(str(loop.get("cumulative_realized_pnl_quote")), "cumulative_realized_pnl_quote")
        except Exception:
            cum = Decimal("0")
        if realized is not None:
            cum = cum + realized
        cycles_completed = int(loop.get("cycles_completed") or 0) + 1
        state.update_loop_session(
            loop_id=loop_id,
            cycles_completed=cycles_completed,
            cumulative_realized_pnl_quote=str(cum),
            state="waiting_buy_fill",
            last_warning=";".join(warnings) if warnings else None,
        )
        _ev(
            "cycle_completed",
            cycle_number=cycles_completed,
            message="cycle_completed",
            details={"cycle": cycles_completed, "realized_pnl": str(realized) if realized is not None else None, "warnings": warnings},
        )

        max_cycles_i = int(loop.get("max_cycles") or 0)
        if max_cycles_i != 0 and cycles_completed >= max_cycles_i:
            state.update_loop_session(loop_id=loop_id, status="completed", stopped_at_utc=utcnow_iso(), state="completed")
            _ev("loop_completed", message="loop_completed", details={"cycles_completed": cycles_completed})
            # Apply cleanup policy on completion (cancel open loop-created orders; optionally exit).
            try:
                loop2 = state.get_loop_session(loop_id=loop_id) or loop
                _loop_apply_cleanup_policy(client=client, state=state, loop=loop2, rules=rules, reason="max_cycles_completed")
            except Exception:
                pass
            # Best-effort sync after cleanup.
            try:
                sync_balances(client=client, conn=conn)
            except Exception:
                pass
            try:
                sync_open_orders(client=client, conn=conn, symbol=symbol)
            except Exception:
                pass
            return (loop_id, updated, 0, 0)

        # Submit next BUY LIMIT from rebuy offset (locked: rebuy from last sell fill price).
        rk = str(loop.get("rebuy_kind") or "").strip().lower()
        rv = loop.get("rebuy_value")
        if rk not in ("abs", "pct") or rv in (None, ""):
            state.update_loop_session(loop_id=loop_id, status="completed", stopped_at_utc=utcnow_iso(), state="completed", last_warning="rebuy_offset_missing")
            return (loop_id, updated, 0, 0)

        rebuy_val = _parse_offset_signed_default_negative(str(rv), name="rebuy_value")
        if rk == "abs":
            target_buy = sell_avg + rebuy_val
        else:
            target_buy = sell_avg * (Decimal("1") + (rebuy_val / Decimal("100")))
        if target_buy <= 0:
            state.update_loop_session(loop_id=loop_id, status="error", last_error="rebuy_target_nonpositive")
            return (loop_id, updated, 1, 1)
        buy_px = quantize_down(target_buy, rules.price_filter.tick_size)
        raw_qty = _d_position(str(loop.get("quote_qty") or "0"), "quote_qty") / buy_px
        buy_qty = quantize_down(raw_qty, rules.lot_size.step_size)
        if buy_qty <= 0 or buy_qty < rules.lot_size.min_qty:
            state.update_loop_session(loop_id=loop_id, status="stopped", stopped_at_utc=utcnow_iso(), last_warning="rebuy_qty_below_minQty")
            return (loop_id, updated, 1, 0)
        if buy_qty * buy_px < rules.min_notional.min_notional:
            state.update_loop_session(loop_id=loop_id, status="stopped", stopped_at_utc=utcnow_iso(), last_warning="rebuy_minNotional_failed")
            return (loop_id, updated, 1, 0)
        buy_client_id = _loop_client_order_id(loop_id=loop_id, leg_role="buy_rebuy")
        buy_leg_id = state.create_loop_leg(
            loop_id=loop_id,
            cycle_index=cycles_completed + 1,
            leg_role="buy_rebuy",
            side="BUY",
            order_type="LIMIT_BUY",
            time_in_force="GTC",
            limit_price=str(buy_px),
            quote_order_qty=str(loop.get("quote_qty")),
            quantity=str(buy_qty),
            client_order_id=buy_client_id,
            message="created",
        )
        state.update_loop_session(loop_id=loop_id, last_buy_leg_id=buy_leg_id)
        state.update_loop_leg(
            leg_id=buy_leg_id,
            local_status="submitting",
            raw_status=None,
            binance_order_id=None,
            retry_count=0,
            executed_quantity=None,
            avg_fill_price=None,
            total_quote_value=None,
            fee_breakdown_json=None,
            message="submitting",
            submitted_at_utc=utcnow_iso(),
        )
        order3, rc3 = _loop_submit_with_idempotency(
            client=client,
            state=state,
            leg_id=buy_leg_id,
            symbol=symbol,
            client_order_id=buy_client_id,
            submit_fn=client.create_order_limit_buy,
            submit_kwargs={"symbol": symbol, "price": str(buy_px), "quantity": str(buy_qty), "client_order_id": buy_client_id, "time_in_force": "GTC"},
        )
        _loop_finalize_leg_from_order(state=state, leg_id=buy_leg_id, order=order3, retry_count=rc3)
        _ev(
            "rebuy_order_submitted",
            side="BUY",
            cycle_number=cycles_completed + 1,
            client_order_id=buy_client_id,
            binance_order_id=str(order3.get("orderId") or "") or None,
            price=str(buy_px),
            quantity=str(buy_qty),
            message="submitted",
            details={"leg_id": buy_leg_id, "order_type": "LIMIT_BUY"},
        )
        return (loop_id, updated + 1, 0, 0)

    def _line(loop_id: int, updated: int, errors: int, stopped: int) -> str:
        base = f"loop reconcile: loop_id={loop_id} updated={updated} errors={errors}"
        if stopped:
            base += " stopped=1"
        return base

    if not loop_flag:
        with connect(db_path) as conn:
            try:
                loop_id, updated, stopped, errors = _run_once(conn)
            except Exception as e:
                print(f"ERROR: {e}")
                return 2
        if loop_id == 0:
            print("(no loop sessions)")
            return 2
        print(_line(loop_id, updated, errors, stopped))
        return 0

    print("Manual loop reconcile started. Ctrl-B stops the loop (cleanup policy). Ctrl-C stops the runner only.")
    last_event_id = 0
    try:
        while True:
            with connect(db_path) as conn:
                try:
                    loop_id, updated, stopped, errors = _run_once(conn)
                except Exception as e:
                    sys.stdout.write("\r\x1b[2K" + f"loop reconcile: ERROR {e}")
                    sys.stdout.flush()
                    return 2
                # Print meaningful new actions (fills/submissions/cycle transitions/etc.) as separate lines.
                try:
                    state = StateManager(conn)
                    if loop_id:
                        evs = state.list_loop_events_since(loop_id=loop_id, after_event_id=last_event_id, limit=200)
                    else:
                        evs = []
                except Exception:
                    evs = []
                important_types = {
                    "entry_order_submitted",
                    "sell_order_submitted",
                    "rebuy_order_submitted",
                    "order_status_changed",
                    "partial_fill_detected",
                    "order_filled",
                    "cycle_completed",
                    "loop_completed",
                    "stop_loss_triggered",
                    "stop_loss_exit_submitted",
                    "stop_loss_exit_failed",
                    "open_order_cancelled",
                    "open_order_cancel_failed",
                    "reconcile_error",
                }
                printable = [e for e in evs if str(e.get("event_type") or "") in important_types]
                if evs:
                    try:
                        last_event_id = int(evs[-1].get("loop_event_id") or last_event_id)
                    except Exception:
                        pass
                if printable:
                    sys.stdout.write("\n")
                    for e in printable:
                        et = str(e.get("event_type") or "")
                        side = str(e.get("side") or "")
                        cyc = e.get("cycle_number")
                        price = e.get("price")
                        qty = e.get("quantity")
                        msg = str(e.get("message") or "")
                        parts = [f"event={et}"]
                        if side:
                            parts.append(f"side={side}")
                        if cyc not in (None, "", 0, "0"):
                            parts.append(f"cycle={cyc}")
                        if price not in (None, ""):
                            parts.append(f"price={price}")
                        if qty not in (None, ""):
                            parts.append(f"qty={qty}")
                        if msg:
                            parts.append(f"msg={msg}")
                        print(" ".join(parts))
            if loop_id == 0:
                print("\n(no loop sessions)")
                return 2
            base_line = _line(loop_id, updated, errors, stopped)
            sys.stdout.write("\r\x1b[2K" + base_line)
            sys.stdout.flush()
            if stopped:
                print("\nStopped (loop paused or not running)")
                return 0
            stop_req = _sleep_with_ctrl_b(seconds=float(interval), end_at=end_at, base_line=base_line, show_countdown=True)
            if stop_req:
                # Ctrl-B: force-stop the loop and apply cleanup policy (no extra confirmation).
                try:
                    with connect(db_path) as conn:
                        state = StateManager(conn)
                        loop = state.get_loop_session(loop_id=loop_id) if loop_id else None
                        if loop and str(loop.get("status") or "") == "running":
                            symbol = str(loop.get("symbol") or "").strip().upper()
                            info = client.get_symbol_info(symbol=symbol)
                            if info:
                                rules = parse_symbol_rules(info)
                                _loop_apply_cleanup_policy(client=client, state=state, loop=loop, rules=rules, reason="force_stop_ctrl_b")
                            state.update_loop_session(loop_id=loop_id, status="stopped", stopped_at_utc=utcnow_iso(), last_warning="force_stopped")
                            state.append_loop_event(loop_id=loop_id, event_type="loop_force_stopped", message="ctrl_b", details={"reason": "ctrl_b"})
                            try:
                                sync_balances(client=client, conn=conn)
                            except Exception:
                                pass
                            try:
                                sync_open_orders(client=client, conn=conn, symbol=symbol)
                            except Exception:
                                pass
                except Exception:
                    pass
                print("\nStopped (loop force-stopped; cleanup applied)")
                return 0
            if end_at is not None and time.monotonic() >= end_at:
                print("\nDone")
                return 0
    except KeyboardInterrupt:
        # Ctrl-C: stop the runner only; leave the loop session unchanged.
        print("\nStopped (runner only; loop still running)")
        return 0


def cmd_trade_manual_loop_preset_list(args: argparse.Namespace) -> int:
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)
    limit = int(getattr(args, "limit", 50))
    with connect(db_path) as conn:
        rows = StateManager(conn).list_loop_presets(limit=limit)
    if not rows:
        print("(no loop presets)")
        return 0
    print(f"Loop presets: {len(rows)}")
    print(f"{'ID':>4} {'AT_UTC':<20} {'NAME':<16} {'SYMBOL':<10} {'QUOTE_QTY':>10} {'ENTRY':<9}")
    for r in rows:
        name = str(r.get("name") or "")
        if len(name) > 16:
            name = name[:13] + "..."
        print(
            f"{int(r.get('preset_id') or 0):>4} "
            f"{str(r.get('created_at_utc') or '-'): <20} "
            f"{name: <16} "
            f"{str(r.get('symbol') or '-'): <10} "
            f"{str(r.get('quote_qty') or '-'):>10} "
            f"{str(r.get('entry_order_type') or '-'): <9}"
        )
    return 0


def cmd_trade_manual_loop_preset_show(args: argparse.Namespace) -> int:
    preset_id = int(getattr(args, "preset_id", 0))
    if preset_id <= 0:
        print("Invalid preset id")
        return 2
    paths = ConfigPaths.from_cli(config_path=args.config, db_path=args.db)
    config_path = ensure_default_config(paths.config_path)
    db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)
    with connect(db_path) as conn:
        preset = StateManager(conn).get_loop_preset(preset_id=preset_id)
    if not preset:
        print("(preset not found)")
        return 2
    print("Loop Preset")
    print(f"- Preset ID: {preset_id}")
    if preset.get("name"):
        print(f"- Name: {preset.get('name')}")
    if preset.get("notes"):
        print(f"- Notes: {preset.get('notes')}")
    print(f"- Symbol: {preset.get('symbol')}")
    print(f"- Quote qty: {preset.get('quote_qty')}")
    print(f"- Entry type: {preset.get('entry_order_type')}")
    if preset.get("entry_limit_price"):
        print(f"- Entry limit price: {preset.get('entry_limit_price')}")
    print(f"- Take-profit: {preset.get('take_profit_kind')}={preset.get('take_profit_value')}")
    if preset.get("rebuy_kind") and preset.get("rebuy_value") not in (None, ""):
        print(f"- Rebuy: {preset.get('rebuy_kind')}={preset.get('rebuy_value')}")
    else:
        print("- Rebuy: (none)")
    if preset.get("stop_loss_kind") and preset.get("stop_loss_value") not in (None, ""):
        print(f"- Stop-loss: {preset.get('stop_loss_kind')}={preset.get('stop_loss_value')}")
    else:
        print("- Stop-loss: (none)")
    print(f"- Created: {preset.get('created_at_utc')}")
    print(f"- Updated: {preset.get('updated_at_utc')}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cryptogent", description="CryptoGent CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="Create default config and initialize DB.")
    _add_common_paths(p_init)
    p_init.set_defaults(fn=cmd_init)

    p_cfg = sub.add_parser("config", help="Show effective configuration.")
    _add_common_paths(p_cfg)
    p_cfg.set_defaults(fn=cmd_config_show)
    cfg_sub = p_cfg.add_subparsers(dest="config_cmd", required=False)

    p_cfg_show = cfg_sub.add_parser("show", help="Show effective configuration.")
    _add_common_paths(p_cfg_show)
    p_cfg_show.set_defaults(fn=cmd_config_show)

    p_cfg_set = cfg_sub.add_parser("set-binance", help="Store Binance API key/secret in config TOML.")
    _add_common_paths(p_cfg_set)
    p_cfg_set.add_argument("--api-key", type=str, default="", help="Binance API key (stored in plaintext).")
    p_cfg_set.add_argument(
        "--api-secret",
        type=str,
        default=None,
        help="Binance API secret (stored in plaintext; prefer --api-secret-stdin).",
    )
    p_cfg_set.add_argument(
        "--api-secret-stdin",
        action="store_true",
        help="Read API secret from stdin (avoids shell history).",
    )
    p_cfg_set.set_defaults(fn=cmd_config_set_binance)

    p_cfg_use_testnet = cfg_sub.add_parser("use-testnet", help="Toggle Binance to Spot Test Network via config flag.")
    _add_common_paths(p_cfg_use_testnet)
    p_cfg_use_testnet.set_defaults(fn=cmd_config_use_testnet)

    p_cfg_use_mainnet = cfg_sub.add_parser("use-mainnet", help="Toggle Binance back to real Spot API via config flag.")
    _add_common_paths(p_cfg_use_mainnet)
    p_cfg_use_mainnet.set_defaults(fn=cmd_config_use_mainnet)

    p_cfg_set_testnet = cfg_sub.add_parser("set-binance-testnet", help="Store Binance testnet API key/secret in config.")
    _add_common_paths(p_cfg_set_testnet)
    p_cfg_set_testnet.add_argument("--api-key", type=str, default="", help="Binance testnet API key (plaintext).")
    p_cfg_set_testnet.add_argument(
        "--api-secret",
        type=str,
        default=None,
        help="Binance testnet API secret (plaintext; prefer --api-secret-stdin).",
    )
    p_cfg_set_testnet.add_argument(
        "--api-secret-stdin",
        action="store_true",
        help="Read API secret from stdin (avoids shell history).",
    )
    p_cfg_set_testnet.set_defaults(fn=cmd_config_set_binance_testnet)

    p_cfg_sync_burn = cfg_sub.add_parser("sync-bnb-burn", help="Fetch and persist Binance spotBNBBurn flag into config.")
    _add_common_paths(p_cfg_sync_burn)
    p_cfg_sync_burn.set_defaults(fn=cmd_config_sync_bnb_burn)

    p_cfg_set_burn = cfg_sub.add_parser("set-bnb-burn", help="Enable/disable paying Spot fees with BNB (spotBNBBurn).")
    _add_common_paths(p_cfg_set_burn)
    g_burn = p_cfg_set_burn.add_mutually_exclusive_group(required=True)
    g_burn.add_argument("--enabled", dest="enabled", action="store_true", help="Enable spotBNBBurn (pay Spot fees with BNB).")
    g_burn.add_argument("--disabled", dest="enabled", action="store_false", help="Disable spotBNBBurn.")
    p_cfg_set_burn.set_defaults(fn=cmd_config_set_bnb_burn)

    p_status = sub.add_parser("status", help="Show local setup status.")
    _add_common_paths(p_status)
    p_status.set_defaults(fn=cmd_status)

    p_pos = sub.add_parser("position", help="Inspect stored positions (Phase 8; no execution).")
    _add_common_paths(p_pos)
    pos_sub = p_pos.add_subparsers(dest="position_cmd", required=True)

    p_pos_list = pos_sub.add_parser("list", help="List positions.")
    _add_common_paths(p_pos_list)
    p_pos_list.add_argument("--status", default=None, help="Filter by status (e.g. OPEN/CLOSED).")
    p_pos_list.add_argument("--limit", type=int, default=50, help="Limit rows (default: 50).")
    p_pos_list.set_defaults(fn=cmd_position_list)

    p_pos_show = pos_sub.add_parser("show", help="Show one position (optionally compute unrealized PnL live).")
    _add_common_paths(p_pos_show)
    _add_exchange_tls_only_args(p_pos_show)
    p_pos_show.add_argument("position_id", type=int, help="Position id.")
    p_pos_show.add_argument("--live", action="store_true", help="Fetch current price and compute unrealized PnL.")
    p_pos_show.set_defaults(fn=cmd_position_show)

    p_mon = sub.add_parser("monitor", help="Phase 8 monitoring (decisions only; no execution).")
    _add_common_paths(p_mon)
    mon_sub = p_mon.add_subparsers(dest="monitor_cmd", required=True)

    p_mon_once = mon_sub.add_parser("once", help="Run one monitoring tick over open positions.")
    _add_common_paths(p_mon_once)
    _add_exchange_tls_only_args(p_mon_once)
    p_mon_once.add_argument("--limit", type=int, default=50, help="Limit positions checked (default: 50).")
    p_mon_once.add_argument("--position-id", type=int, default=None, help="Monitor only this OPEN position id.")
    p_mon_once.add_argument("--verbose", action="store_true", help="Print per-position live PnL each tick.")
    p_mon_once.set_defaults(fn=cmd_monitor_once)

    p_mon_loop = mon_sub.add_parser("loop", help="Run monitoring ticks repeatedly.")
    _add_common_paths(p_mon_loop)
    _add_exchange_tls_only_args(p_mon_loop)
    p_mon_loop.add_argument("--limit", type=int, default=50, help="Limit positions checked per tick (default: 50).")
    p_mon_loop.add_argument("--position-id", type=int, default=None, help="Monitor only this OPEN position id.")
    p_mon_loop.add_argument(
        "--interval-seconds",
        type=int,
        default=None,
        help="Sleep between ticks (default: from config or fallback).",
    )
    p_mon_loop.add_argument("--duration-seconds", type=int, default=None, help="Stop after this many seconds (default: run until Ctrl-C).")
    p_mon_loop.add_argument("--verbose", action="store_true", help="Print per-position live PnL each tick.")
    p_mon_loop.set_defaults(fn=cmd_monitor_loop)

    p_mon_events = mon_sub.add_parser("events", help="Monitoring event history.")
    _add_common_paths(p_mon_events)
    events_sub = p_mon_events.add_subparsers(dest="events_cmd", required=True)

    p_mon_events_list = events_sub.add_parser("list", help="List monitoring events.")
    _add_common_paths(p_mon_events_list)
    p_mon_events_list.add_argument("--limit", type=int, default=50, help="Limit rows (default: 50).")
    p_mon_events_list.set_defaults(fn=cmd_monitor_events_list)

    p_orders = sub.add_parser("orders", help="Manage cached open orders (manual/execution only).")
    _add_common_paths(p_orders)
    orders_sub = p_orders.add_subparsers(dest="orders_cmd", required=True)

    p_orders_cancel = orders_sub.add_parser("cancel", help="Cancel an open order by exchange order id.")
    _add_common_paths(p_orders_cancel)
    _add_exchange_tls_args(p_orders_cancel)
    p_orders_cancel.add_argument("order_id", help="Binance exchange order id.")
    p_orders_cancel.add_argument("--i-am-human", action="store_true", help="Required for manual orders.")
    p_orders_cancel.set_defaults(fn=cmd_orders_cancel)

    p_rel = sub.add_parser("reliability", help="Phase 9 reliability and recovery tools.")
    _add_common_paths(p_rel)
    rel_sub = p_rel.add_subparsers(dest="rel_cmd", required=True)

    p_rel_status = rel_sub.add_parser("status", help="Show reliability pause/recovery status.")
    _add_common_paths(p_rel_status)
    p_rel_status.set_defaults(fn=cmd_reliability_status)

    p_rel_reconcile = rel_sub.add_parser("reconcile", help="Reconcile local state with exchange truth.")
    _add_common_paths(p_rel_reconcile)
    _add_exchange_tls_args(p_rel_reconcile)
    p_rel_reconcile.set_defaults(fn=cmd_reliability_reconcile)

    p_rel_events = rel_sub.add_parser("events", help="Reliability event history.")
    _add_common_paths(p_rel_events)
    rel_events_sub = p_rel_events.add_subparsers(dest="rel_events_cmd", required=True)

    p_rel_events_list = rel_events_sub.add_parser("list", help="List reconciliation events.")
    _add_common_paths(p_rel_events_list)
    p_rel_events_list.add_argument("--limit", type=int, default=50, help="Limit rows (default: 50).")
    p_rel_events_list.set_defaults(fn=cmd_reliability_events_list)

    p_rel_resume = rel_sub.add_parser("resume", help="Resume automation after successful reconciliation.")
    _add_common_paths(p_rel_resume)
    p_rel_resume.add_argument("--i-am-human", action="store_true", required=True, help="Required safety flag.")
    p_rel_resume.add_argument("--global", dest="global_pause", action="store_true", help="Resume global automation.")
    p_rel_resume.add_argument("--symbol", type=str, default=None, help="Resume automation for a symbol (e.g. SOLUSDT).")
    p_rel_resume.add_argument("--loop-id", type=int, default=None, help="Resume automation for a loop id.")
    p_rel_resume.set_defaults(fn=cmd_reliability_resume)

    p_ex = sub.add_parser("exchange", help="Binance Spot connectivity utilities (no trading).")
    _add_common_paths(p_ex)
    ex_sub = p_ex.add_subparsers(dest="exchange_cmd", required=True)

    p_ex_ping = ex_sub.add_parser("ping", help="Call /api/v3/ping.")
    _add_common_paths(p_ex_ping)
    _add_exchange_tls_args(p_ex_ping)
    p_ex_ping.set_defaults(fn=cmd_exchange_ping)

    p_ex_time = ex_sub.add_parser("time", help="Call /api/v3/time.")
    _add_common_paths(p_ex_time)
    _add_exchange_tls_args(p_ex_time)
    p_ex_time.set_defaults(fn=cmd_exchange_time)

    p_ex_info = ex_sub.add_parser("info", help="Call /api/v3/exchangeInfo.")
    _add_common_paths(p_ex_info)
    _add_exchange_tls_args(p_ex_info)
    p_ex_info.add_argument("--symbol", type=str, default=None, help="Optional symbol (e.g. BTCUSDT).")
    p_ex_info.set_defaults(fn=cmd_exchange_info)

    p_ex_bal = ex_sub.add_parser("balances", help="Call /api/v3/account and print balances (requires key+secret).")
    _add_common_paths(p_ex_bal)
    _add_exchange_tls_args(p_ex_bal)
    p_ex_bal.add_argument("--all", action="store_true", help="Show zero balances too.")
    p_ex_bal.set_defaults(fn=cmd_exchange_balances)

    p_sync = sub.add_parser("sync", help="Sync exchange state into the local SQLite cache (no trading).")
    _add_common_paths(p_sync)
    _add_exchange_tls_args(p_sync)
    sync_sub = p_sync.add_subparsers(dest="sync_cmd", required=True)

    p_sync_startup = sync_sub.add_parser("startup", help="Startup sync: account snapshot + balances + open orders.")
    _add_common_paths(p_sync_startup)
    _add_exchange_tls_args(p_sync_startup)
    p_sync_startup.set_defaults(fn=cmd_sync_startup)

    p_sync_bal = sync_sub.add_parser("balances", help="Sync balances into the local cache.")
    _add_common_paths(p_sync_bal)
    _add_exchange_tls_args(p_sync_bal)
    p_sync_bal.set_defaults(fn=cmd_sync_balances)

    p_sync_oo = sync_sub.add_parser("open-orders", help="Sync open orders into the local cache.")
    _add_common_paths(p_sync_oo)
    _add_exchange_tls_args(p_sync_oo)
    p_sync_oo.add_argument("--symbol", type=str, default=None, help="Optional symbol (e.g. BTCUSDT).")
    p_sync_oo.set_defaults(fn=cmd_sync_open_orders)

    p_sync_fng = sync_sub.add_parser("fear-greed", help="Sync Fear & Greed Index into the local cache.")
    _add_common_paths(p_sync_fng)
    _add_exchange_tls_only_args(p_sync_fng)
    p_sync_fng.set_defaults(fn=cmd_sync_fear_greed)

    p_show = sub.add_parser("show", help="Show cached state from SQLite (no network).")
    _add_common_paths(p_show)
    show_sub = p_show.add_subparsers(dest="show_cmd", required=True)

    p_show_bal = show_sub.add_parser("balances", help="Show cached balances.")
    _add_common_paths(p_show_bal)
    p_show_bal.add_argument("--all", action="store_true", help="Include zero balances.")
    p_show_bal.add_argument("--limit", type=int, default=None, help="Limit rows.")
    p_show_bal.add_argument(
        "--filter",
        type=str,
        default=None,
        help='Filter balances by exact asset(s): e.g. SOL or "SOL,AI". Use --contains for substring search.',
    )
    p_show_bal.add_argument("--contains", action="store_true", help="Use substring match instead of exact asset match.")
    p_show_bal.set_defaults(fn=cmd_show_balances)

    p_show_oo = show_sub.add_parser("open-orders", help="Show cached open orders.")
    _add_common_paths(p_show_oo)
    p_show_oo.add_argument("--symbol", type=str, default=None, help="Optional symbol (e.g. BTCUSDT).")
    p_show_oo.add_argument("--limit", type=int, default=None, help="Limit rows.")
    p_show_oo.set_defaults(fn=cmd_show_open_orders)

    p_show_fng = show_sub.add_parser("fear-greed", help="Show cached Fear & Greed Index.")
    _add_common_paths(p_show_fng)
    p_show_fng.add_argument("--limit", type=int, default=20, help="Limit rows.")
    p_show_fng.set_defaults(fn=cmd_show_fear_greed)

    p_show_audit = show_sub.add_parser("audit", help="Show cached audit log entries.")
    _add_common_paths(p_show_audit)
    p_show_audit.add_argument("--limit", type=int, default=50, help="Limit rows (default: 50).")
    p_show_audit.set_defaults(fn=cmd_show_audit)

    p_trade = sub.add_parser("trade", help="Trade request workflow (no execution yet).")
    _add_common_paths(p_trade)
    trade_sub = p_trade.add_subparsers(dest="trade_cmd", required=True)

    p_trade_start = trade_sub.add_parser("start", help="Create a new trade request.")
    _add_common_paths(p_trade_start)
    p_trade_start.add_argument("--profit-target-pct", required=True, help="Profit target percent (e.g. 2.5).")
    p_trade_start.add_argument("--stop-loss-pct", default=None, help="Stop-loss percent (defaults from config).")
    p_trade_start.add_argument(
        "--deadline",
        default=None,
        help="ISO8601 deadline with timezone (e.g. 2026-03-14T12:00:00+00:00).",
    )
    p_trade_start.add_argument(
        "--deadline-minutes",
        type=int,
        default=None,
        help="Relative deadline in minutes from now (alternative to --deadline).",
    )
    p_trade_start.add_argument(
        "--deadline-hours",
        type=int,
        default=None,
        help="Relative deadline in hours from now (alternative to --deadline).",
    )
    p_trade_start.add_argument("--budget-mode", default=None, help="Budget mode: manual|auto (defaults from config).")
    p_trade_start.add_argument("--budget", default=None, help="Budget amount (required for manual mode).")
    p_trade_start.add_argument("--budget-asset", default="USDT", help="Budget asset code (default: USDT).")
    p_trade_start.add_argument("--symbol", default=None, help="Preferred symbol (e.g. BTCUSDT). Optional.")
    p_trade_start.add_argument("--exit-asset", default=None, help="Exit asset (defaults from config).")
    p_trade_start.add_argument("--label", default=None, help="Optional short label.")
    p_trade_start.add_argument("--notes", default=None, help="Optional notes.")
    p_trade_start.add_argument("--yes", action="store_true", help="Skip confirmation prompt.")
    p_trade_start.set_defaults(fn=cmd_trade_start)

    p_trade_list = trade_sub.add_parser("list", help="List trade requests.")
    _add_common_paths(p_trade_list)
    p_trade_list.add_argument("--limit", type=int, default=20, help="Limit rows (default: 20).")
    p_trade_list.set_defaults(fn=cmd_trade_list)

    p_trade_show = trade_sub.add_parser("show", help="Show one trade request.")
    _add_common_paths(p_trade_show)
    p_trade_show.add_argument("id", type=int, help="Trade request id.")
    p_trade_show.set_defaults(fn=cmd_trade_show)

    p_trade_cancel = trade_sub.add_parser("cancel", help="Cancel a NEW trade request.")
    _add_common_paths(p_trade_cancel)
    p_trade_cancel.add_argument("id", type=int, help="Trade request id.")
    p_trade_cancel.set_defaults(fn=cmd_trade_cancel)

    p_trade_validate = trade_sub.add_parser("validate", help="Validate a trade request against Binance symbol rules.")
    _add_common_paths(p_trade_validate)
    _add_exchange_tls_args(p_trade_validate)
    p_trade_validate.add_argument("id", type=int, help="Trade request id.")
    p_trade_validate.set_defaults(fn=cmd_trade_validate)

    p_trade_plan = trade_sub.add_parser("plan", help="Trade plan commands (Phase 5; no execution).")
    _add_common_paths(p_trade_plan)
    _add_exchange_tls_args(p_trade_plan)
    plan_sub = p_trade_plan.add_subparsers(dest="plan_cmd", required=True)

    p_trade_plan_build = plan_sub.add_parser("build", help="Build and persist a deterministic trade plan from a trade request.")
    _add_common_paths(p_trade_plan_build)
    _add_exchange_tls_args(p_trade_plan_build)
    p_trade_plan_build.add_argument("id", type=int, help="Trade request id.")
    p_trade_plan_build.add_argument("--candle-interval", default="5m", help="Candle interval (default: 5m).")
    p_trade_plan_build.add_argument("--candle-count", type=int, default=288, help="Number of candles (default: 288 ≈ 24h).")
    p_trade_plan_build.set_defaults(fn=cmd_trade_plan)

    p_trade_plan_list = plan_sub.add_parser("list", help="List trade plans.")
    _add_common_paths(p_trade_plan_list)
    p_trade_plan_list.add_argument("--limit", type=int, default=20, help="Limit rows (default: 20).")
    p_trade_plan_list.set_defaults(fn=cmd_trade_plan_list)

    p_trade_plan_show = plan_sub.add_parser("show", help="Show one trade plan.")
    _add_common_paths(p_trade_plan_show)
    p_trade_plan_show.add_argument("plan_id", type=int, help="Trade plan id.")
    p_trade_plan_show.set_defaults(fn=cmd_trade_plan_show)

    # Backwards-compatible alias for the previous `trade plan <trade_request_id>` form.
    p_trade_plan_build_alias = trade_sub.add_parser("plan-build", help=argparse.SUPPRESS)
    _add_common_paths(p_trade_plan_build_alias)
    _add_exchange_tls_args(p_trade_plan_build_alias)
    p_trade_plan_build_alias.add_argument("id", type=int, help="Trade request id.")
    p_trade_plan_build_alias.add_argument("--candle-interval", default="5m")
    p_trade_plan_build_alias.add_argument("--candle-count", type=int, default=288)
    p_trade_plan_build_alias.set_defaults(fn=cmd_trade_plan)

    p_trade_safety = trade_sub.add_parser("safety", help="Phase 6 safety validation (plan-based; no execution).")
    _add_common_paths(p_trade_safety)
    _add_exchange_tls_args(p_trade_safety)
    p_trade_safety.add_argument("plan_id", type=int, help="Trade plan id.")
    p_trade_safety.add_argument("--max-age-minutes", type=int, default=60, help="Expire plans older than this (default: 60).")
    p_trade_safety.add_argument("--price-drift-warn-pct", default="1.0", help="Warning threshold for price drift percent (default: 1.0).")
    p_trade_safety.add_argument("--price-drift-unsafe-pct", default="3.0", help="Unsafe threshold for price drift percent (default: 3.0).")
    p_trade_safety.add_argument("--max-position-pct", default="25", help="Max approved budget as percent of free quote balance (default: 25).")
    p_trade_safety.add_argument("--max-stop-loss-pct", default="10", help="Reject stop-loss > this percent (default: 10).")
    p_trade_safety.add_argument(
        "--order-type",
        default="MARKET_BUY",
        help="Order type for the generated execution candidate: MARKET_BUY|LIMIT_BUY|MARKET_SELL|LIMIT_SELL (default: MARKET_BUY).",
    )
    p_trade_safety.add_argument(
        "--limit-price",
        default=None,
        help="Required when --order-type=LIMIT_BUY or LIMIT_SELL. Limit price in quote asset (e.g. 61829.72).",
    )
    p_trade_safety.add_argument(
        "--position-id",
        default=None,
        help="SELL only. Optional explicit position id to close (default: active position for this symbol).",
    )
    p_trade_safety.add_argument(
        "--close-mode",
        choices=["amount", "percent", "all"],
        default="all",
        help="SELL only. How much of the position to close: amount|percent|all (default: all).",
    )
    p_trade_safety.add_argument(
        "--close-amount",
        default=None,
        help="SELL only. Required when --close-mode=amount. Base-asset quantity to sell (e.g. 1.25).",
    )
    p_trade_safety.add_argument(
        "--close-percent",
        default=None,
        help="SELL only. Required when --close-mode=percent. Percent of position to sell (e.g. 50).",
    )
    p_trade_safety.set_defaults(fn=cmd_trade_safety)

    p_trade_execute = trade_sub.add_parser("execute", help="Phase 7 execution (BUY MARKET only; candidate-based).")
    _add_common_paths(p_trade_execute)
    _add_exchange_tls_args(p_trade_execute)
    p_trade_execute.add_argument("candidate_id", type=int, help="Execution candidate id.")
    p_trade_execute.add_argument("--yes", action="store_true", help="Skip confirmation prompt.")
    p_trade_execute.set_defaults(fn=cmd_trade_execute)

    p_trade_exec = trade_sub.add_parser("execution", help="Inspect stored Phase 7 execution attempts.")
    _add_common_paths(p_trade_exec)
    exec_sub = p_trade_exec.add_subparsers(dest="exec_cmd", required=True)

    p_trade_exec_list = exec_sub.add_parser("list", help="List stored executions.")
    _add_common_paths(p_trade_exec_list)
    p_trade_exec_list.add_argument("--limit", type=int, default=20, help="Limit rows (default: 20).")
    p_trade_exec_list.set_defaults(fn=cmd_trade_executions_list)

    p_trade_exec_show = exec_sub.add_parser("show", help="Show one execution.")
    _add_common_paths(p_trade_exec_show)
    p_trade_exec_show.add_argument("execution_id", type=int, help="Execution id.")
    p_trade_exec_show.set_defaults(fn=cmd_trade_executions_show)

    p_trade_exec_cancel = exec_sub.add_parser("cancel", help="Cancel an open LIMIT_BUY execution on Binance.")
    _add_common_paths(p_trade_exec_cancel)
    _add_exchange_tls_args(p_trade_exec_cancel)
    p_trade_exec_cancel.add_argument("execution_id", type=int, help="Execution id.")
    p_trade_exec_cancel.set_defaults(fn=cmd_trade_execution_cancel)

    p_trade_manual = trade_sub.add_parser(
        "manual",
        help="Manual direct order mode (human-only; bypasses planning/safety; enforces exchange rules).",
    )
    _add_common_paths(p_trade_manual)
    manual_sub = p_trade_manual.add_subparsers(dest="manual_cmd", required=True)

    p_manual_list = manual_sub.add_parser("list", help="List manual orders.")
    _add_common_paths(p_manual_list)
    p_manual_list.add_argument("--limit", type=int, default=20, help="Limit rows (default: 20).")
    p_manual_list.set_defaults(fn=cmd_trade_manual_list)

    p_manual_show = manual_sub.add_parser("show", help="Show one manual order.")
    _add_common_paths(p_manual_show)
    p_manual_show.add_argument("manual_order_id", type=int, help="Manual order id.")
    p_manual_show.set_defaults(fn=cmd_trade_manual_show)

    p_manual_reconcile = manual_sub.add_parser("reconcile", help="Reconcile manual orders with Binance by clientOrderId.")
    _add_common_paths(p_manual_reconcile)
    _add_exchange_tls_args(p_manual_reconcile)
    p_manual_reconcile.add_argument("--manual-order-id", type=int, default=None, help="Reconcile only this manual order id.")
    p_manual_reconcile.add_argument("--limit", type=int, default=50, help="Limit rows (default: 50).")
    p_manual_reconcile.add_argument("--loop", action="store_true", help="Run reconciliation repeatedly until stopped.")
    p_manual_reconcile.add_argument("--interval-seconds", type=int, default=60, help="Sleep between loops (default: 60).")
    p_manual_reconcile.add_argument("--duration-seconds", type=int, default=None, help="Stop after this many seconds (default: run until stopped).")
    p_manual_reconcile.set_defaults(fn=cmd_trade_manual_reconcile)

    p_manual_cancel = manual_sub.add_parser("cancel", help="Cancel an open LIMIT manual order on Binance.")
    _add_common_paths(p_manual_cancel)
    _add_exchange_tls_args(p_manual_cancel)
    p_manual_cancel.add_argument("--i-am-human", action="store_true", required=True, help="Required safety flag.")
    p_manual_cancel.add_argument("manual_order_id", type=int, help="Manual order id.")
    p_manual_cancel.set_defaults(fn=cmd_trade_manual_cancel)

    p_manual_bm = manual_sub.add_parser("buy-market", help="Manual MARKET BUY using quoteOrderQty.")
    _add_common_paths(p_manual_bm)
    _add_exchange_tls_args(p_manual_bm)
    p_manual_bm.add_argument("--i-am-human", action="store_true", required=True, help="Required safety flag.")
    p_manual_bm.add_argument("--dry-run", action="store_true", help="Preview + live checks only; do not submit.")
    p_manual_bm.add_argument("--symbol", required=True, help="Symbol (e.g. BTCUSDT).")
    p_manual_bm.add_argument("--quote-qty", required=True, help="Quote amount to spend (e.g. 50).")
    p_manual_bm.set_defaults(fn=cmd_trade_manual_buy_market)

    p_manual_bl = manual_sub.add_parser("buy-limit", help="Manual LIMIT BUY (GTC) sized from quote budget.")
    _add_common_paths(p_manual_bl)
    _add_exchange_tls_args(p_manual_bl)
    p_manual_bl.add_argument("--i-am-human", action="store_true", required=True, help="Required safety flag.")
    p_manual_bl.add_argument("--dry-run", action="store_true", help="Preview + live checks only; do not submit.")
    p_manual_bl.add_argument("--symbol", required=True, help="Symbol (e.g. BTCUSDT).")
    p_manual_bl.add_argument("--quote-qty", required=True, help="Quote budget to allocate (e.g. 50).")
    p_manual_bl.add_argument("--limit-price", required=True, help="Limit price (e.g. 61829.72).")
    p_manual_bl.set_defaults(fn=cmd_trade_manual_buy_limit)

    p_manual_sm = manual_sub.add_parser("sell-market", help="Manual MARKET SELL by base quantity.")
    _add_common_paths(p_manual_sm)
    _add_exchange_tls_args(p_manual_sm)
    p_manual_sm.add_argument("--i-am-human", action="store_true", required=True, help="Required safety flag.")
    p_manual_sm.add_argument("--dry-run", action="store_true", help="Preview + live checks only; do not submit.")
    p_manual_sm.add_argument("--symbol", required=True, help="Symbol (e.g. SOLUSDT).")
    p_manual_sm.add_argument("--base-qty", required=True, help="Base quantity to sell (e.g. 1.0).")
    p_manual_sm.set_defaults(fn=cmd_trade_manual_sell_market)

    p_manual_sl = manual_sub.add_parser("sell-limit", help="Manual LIMIT SELL (GTC) by base quantity.")
    _add_common_paths(p_manual_sl)
    _add_exchange_tls_args(p_manual_sl)
    p_manual_sl.add_argument("--i-am-human", action="store_true", required=True, help="Required safety flag.")
    p_manual_sl.add_argument("--dry-run", action="store_true", help="Preview + live checks only; do not submit.")
    p_manual_sl.add_argument("--symbol", required=True, help="Symbol (e.g. SOLUSDT).")
    p_manual_sl.add_argument("--base-qty", required=True, help="Base quantity to sell (e.g. 1.0).")
    p_manual_sl.add_argument("--limit-price", required=True, help="Limit price (e.g. 100.00).")
    p_manual_sl.set_defaults(fn=cmd_trade_manual_sell_limit)

    p_manual_loop = manual_sub.add_parser("loop", help="Manual loop trading mode (human-only; BUY→SELL→BUY…).")
    _add_common_paths(p_manual_loop)
    loop_sub = p_manual_loop.add_subparsers(dest="loop_cmd", required=True)

    p_loop_create = loop_sub.add_parser("create", help="Create a stored loop preset (no exchange side effects).")
    _add_common_paths(p_loop_create)
    p_loop_create.add_argument("--name", default=None, help="Optional preset name.")
    p_loop_create.add_argument("--notes", default=None, help="Optional notes.")
    p_loop_create.add_argument("--symbol", required=True, help="Symbol (e.g. SOLUSDT).")
    p_loop_create.add_argument("--quote-qty", required=True, help="Quote amount to spend each BUY (e.g. 1000).")
    p_loop_create.add_argument("--entry-type", dest="entry_type", choices=["BUY_MARKET", "BUY_LIMIT"], default="BUY_MARKET")
    p_loop_create.add_argument("--entry-limit-price", default=None, help="Required when --entry-type=BUY_LIMIT.")
    g_tp_c = p_loop_create.add_mutually_exclusive_group(required=True)
    g_tp_c.add_argument("--take-profit-abs", default=None, help="Take-profit absolute offset in quote (e.g. 0.50).")
    g_tp_c.add_argument("--take-profit-pct", default=None, help="Take-profit percent (e.g. 1.0).")
    g_rb_c = p_loop_create.add_mutually_exclusive_group(required=False)
    g_rb_c.add_argument("--rebuy-abs", default=None, help='Rebuy abs offset from last sell price (signed; "0.05"=dip, "+0.05"=momentum).')
    g_rb_c.add_argument("--rebuy-pct", default=None, help='Rebuy pct offset from last sell price (signed; "-1" dip, "+1" momentum).')
    g_sl_c = p_loop_create.add_mutually_exclusive_group(required=False)
    g_sl_c.add_argument("--stop-loss-abs", default=None, help="Stop-loss abs below last BUY avg (e.g. 0.50).")
    g_sl_c.add_argument("--stop-loss-pct", default=None, help="Stop-loss pct below last BUY avg (e.g. 1.0).")
    p_loop_create.add_argument(
        "--stop-loss-action",
        choices=["stop_only", "stop_and_exit"],
        default="stop_only",
        help="When stop-loss hits: stop_only (default) or stop_and_exit (market sell).",
    )
    p_loop_create.add_argument(
        "--cleanup-policy",
        choices=["cancel-open", "none", "cancel-open-and-exit"],
        default="cancel-open-and-exit",
        help="When loop stops/completes: cancel-open-and-exit (default), cancel-open, or none.",
    )
    p_loop_create.set_defaults(fn=cmd_trade_manual_loop_create)

    p_loop_start = loop_sub.add_parser("start", help="Start a new manual loop session.")
    _add_common_paths(p_loop_start)
    _add_exchange_tls_args(p_loop_start)
    p_loop_start.add_argument("--i-am-human", action="store_true", required=True, help="Required safety flag.")
    p_loop_start.add_argument("--dry-run", action="store_true", help="Preview + live checks only; do not submit/cancel.")
    p_loop_start.add_argument("--yes", action="store_true", help="Skip confirmation prompt.")
    p_loop_start.add_argument("--id", type=int, default=None, help="Start from a stored preset id.")
    p_loop_start.add_argument("--symbol", required=False, default=None, help="Symbol (e.g. SOLUSDT). Ignored when --id is used.")
    p_loop_start.add_argument("--quote-qty", required=False, default=None, help="Quote amount to spend each BUY. Ignored when --id is used.")
    p_loop_start.add_argument("--entry-type", dest="entry_type", choices=["BUY_MARKET", "BUY_LIMIT"], default="BUY_MARKET")
    p_loop_start.add_argument("--entry-limit-price", default=None, help="Required when --entry-type=BUY_LIMIT.")
    g_tp = p_loop_start.add_mutually_exclusive_group(required=False)
    g_tp.add_argument("--take-profit-abs", default=None, help="Take-profit absolute offset in quote (e.g. 0.50).")
    g_tp.add_argument("--take-profit-pct", default=None, help="Take-profit percent (e.g. 1.0).")
    g_rb = p_loop_start.add_mutually_exclusive_group(required=False)
    g_rb.add_argument("--rebuy-abs", default=None, help='Rebuy abs offset from last sell price (signed; "0.05"=dip, "+0.05"=momentum).')
    g_rb.add_argument("--rebuy-pct", default=None, help='Rebuy pct offset from last sell price (signed; "-1" dip, "+1" momentum).')
    g_sl = p_loop_start.add_mutually_exclusive_group(required=False)
    g_sl.add_argument("--stop-loss-abs", default=None, help="Stop-loss abs below last BUY avg (e.g. 0.50).")
    g_sl.add_argument("--stop-loss-pct", default=None, help="Stop-loss pct below last BUY avg (e.g. 1.0).")
    p_loop_start.add_argument(
        "--stop-loss-action",
        choices=["stop_only", "stop_and_exit"],
        default=None,
        help="Override preset stop-loss action: stop_only or stop_and_exit (market sell).",
    )
    p_loop_start.add_argument(
        "--cleanup-policy",
        choices=["cancel-open", "none", "cancel-open-and-exit"],
        default=None,
        help="Override preset cleanup policy: cancel-open-and-exit, cancel-open, or none.",
    )
    p_loop_start.add_argument("--max-cycles", type=int, default=1, help="0=infinite; default=1.")
    p_loop_start.add_argument("--interval-seconds", type=int, default=6, help="Runner tick interval (default: 6).")
    p_loop_start.add_argument(
        "--duration-seconds",
        type=int,
        default=None,
        help="Stop the local runner after this many seconds (default: run until stopped/completed).",
    )
    p_loop_start.add_argument("--no-run", action="store_true", help="Submit entry order and exit (advanced).")
    p_loop_start.set_defaults(fn=cmd_trade_manual_loop_start)

    p_loop_status = loop_sub.add_parser("status", help="Show loop session status.")
    _add_common_paths(p_loop_status)
    p_loop_status.add_argument("--loop-id", type=int, default=None, help="Loop id (default: latest running, else latest).")
    p_loop_status.set_defaults(fn=cmd_trade_manual_loop_status)

    p_loop_list = loop_sub.add_parser("list", help="List loop sessions.")
    _add_common_paths(p_loop_list)
    p_loop_list.add_argument("--limit", type=int, default=20, help="Limit rows (default: 20).")
    p_loop_list.set_defaults(fn=cmd_trade_manual_loop_list)

    p_loop_stop = loop_sub.add_parser("stop", help="Stop a loop and cancel any open order (asks for confirmation).")
    _add_common_paths(p_loop_stop)
    _add_exchange_tls_args(p_loop_stop)
    p_loop_stop.add_argument("--i-am-human", action="store_true", required=True, help="Required safety flag.")
    p_loop_stop.add_argument("--loop-id", type=int, default=None, help="Loop id (default: latest running, else latest).")
    p_loop_stop.set_defaults(fn=cmd_trade_manual_loop_stop)

    p_loop_recon = loop_sub.add_parser("reconcile", help="Reconcile a loop session and advance if legs are fully filled.")
    _add_common_paths(p_loop_recon)
    _add_exchange_tls_args(p_loop_recon)
    p_loop_recon.add_argument("--i-am-human", action="store_true", required=True, help="Required safety flag.")
    p_loop_recon.add_argument("--loop-id", type=int, default=None, help="Loop id (default: latest running, else latest).")
    p_loop_recon.add_argument("--loop", action="store_true", help="Run repeatedly until stopped.")
    p_loop_recon.add_argument("--interval-seconds", type=int, default=60, help="Sleep between loops (default: 60).")
    p_loop_recon.add_argument("--duration-seconds", type=int, default=None, help="Stop after this many seconds (default: run until stopped).")
    p_loop_recon.set_defaults(fn=cmd_trade_manual_loop_reconcile)

    p_loop_preset = loop_sub.add_parser("preset", help="Manage stored loop presets.")
    _add_common_paths(p_loop_preset)
    preset_sub = p_loop_preset.add_subparsers(dest="preset_cmd", required=True)

    p_loop_preset_list = preset_sub.add_parser("list", help="List stored loop presets.")
    _add_common_paths(p_loop_preset_list)
    p_loop_preset_list.add_argument("--limit", type=int, default=50, help="Limit rows (default: 50).")
    p_loop_preset_list.set_defaults(fn=cmd_trade_manual_loop_preset_list)

    p_loop_preset_show = preset_sub.add_parser("show", help="Show one stored loop preset.")
    _add_common_paths(p_loop_preset_show)
    p_loop_preset_show.add_argument("preset_id", type=int, help="Preset id.")
    p_loop_preset_show.set_defaults(fn=cmd_trade_manual_loop_preset_show)

    p_trade_reconcile = trade_sub.add_parser("reconcile", help="Reconcile executions with Binance (supports LIMIT timeout expiry).")
    _add_common_paths(p_trade_reconcile)
    _add_exchange_tls_args(p_trade_reconcile)
    p_trade_reconcile.add_argument("--limit", type=int, default=50, help="Limit executions checked (default: 50).")
    p_trade_reconcile.add_argument("--loop", action="store_true", help="Run reconciliation repeatedly until stopped.")
    p_trade_reconcile.add_argument("--interval-seconds", type=int, default=60, help="Sleep between loops (default: 60).")
    p_trade_reconcile.add_argument("--duration-seconds", type=int, default=None, help="Stop after this many seconds (default: run until stopped).")
    p_trade_reconcile.add_argument(
        "--limit-order-timeout-minutes",
        type=int,
        default=30,
        help="Mark open LIMIT_BUY executions as expired after this many minutes (default: 30).",
    )
    p_trade_reconcile.add_argument(
        "--auto-cancel-expired",
        action="store_true",
        default=None,
        help="Cancel expired LIMIT_BUY orders on Binance.",
    )
    p_trade_reconcile.set_defaults(fn=cmd_trade_reconcile)

    p_trade_reconcile_all = trade_sub.add_parser(
        "reconcile-all",
        help="Reconcile both executions and manual orders (convenience wrapper).",
    )
    _add_common_paths(p_trade_reconcile_all)
    _add_exchange_tls_args(p_trade_reconcile_all)
    p_trade_reconcile_all.add_argument("--limit", type=int, default=50, help="Limit executions checked (default: 50).")
    p_trade_reconcile_all.add_argument("--loop", action="store_true", help="Run reconciliation repeatedly until stopped.")
    p_trade_reconcile_all.add_argument("--interval-seconds", type=int, default=60, help="Sleep between loops (default: 60).")
    p_trade_reconcile_all.add_argument("--duration-seconds", type=int, default=None, help="Stop after this many seconds (default: run until stopped).")
    p_trade_reconcile_all.add_argument(
        "--limit-order-timeout-minutes",
        type=int,
        default=30,
        help="Mark open LIMIT executions as expired after this many minutes (default: 30).",
    )
    p_trade_reconcile_all.add_argument(
        "--auto-cancel-expired",
        action="store_true",
        default=None,
        help="Cancel expired LIMIT orders on Binance.",
    )
    p_trade_reconcile_all.add_argument("--manual-order-id", type=int, default=None, help="Reconcile only this manual order id.")
    p_trade_reconcile_all.set_defaults(fn=cmd_trade_reconcile_all)

    p_menu = sub.add_parser("menu", help="Interactive menu wrapper over subcommands.")
    _add_common_paths(p_menu)
    p_menu.set_defaults(fn=cmd_menu)

    p_market = sub.add_parser("market", help="Market data status (basic).")
    _add_common_paths(p_market)
    market_sub = p_market.add_subparsers(dest="market_cmd", required=True)

    p_market_status = market_sub.add_parser("status", help="Basic market status for a single timeframe.")
    _add_common_paths(p_market_status)
    _add_exchange_tls_only_args(p_market_status)
    p_market_status.add_argument("--symbol", required=True, help="Symbol (e.g. SOLUSDT).")
    p_market_status.add_argument("--timeframe", required=True, help="Timeframe (e.g. 1h).")
    p_market_status.add_argument("--limit", type=int, default=100, help="Candle limit (default: 100).")
    p_market_status.add_argument("--market-env", choices=["mainnet_public", "testnet"], default="mainnet_public")
    p_market_status.add_argument("--json", action="store_true", help="Output JSON only.")
    p_market_status.add_argument("--compact", action="store_true", help="Compact single-line output.")
    p_market_status.add_argument("--table", action="store_true", help="Key/value table output.")
    p_market_status.add_argument("--cache", default=None, help="Cache TTL (e.g. 5s, 60, 1m, 1h).")
    p_market_status.add_argument("--save-snapshot", action="store_true", help="Persist a market snapshot.")
    p_market_status.add_argument("--profile", choices=["quick", "trend", "full"], help="Preset analysis profile.")
    p_market_status.add_argument("--momentum", action="store_true", help="Include RSI/MACD/Stoch RSI section.")
    p_market_status.add_argument("--trend", action="store_true", help="Include EMA/SMA + crossover section.")
    p_market_status.add_argument("--volatility", action="store_true", help="Include ATR/Bollinger section.")
    p_market_status.add_argument("--volume", action="store_true", help="Include volume & liquidity section.")
    p_market_status.add_argument("--structure", action="store_true", help="Include structure (BOS/CHOCH, range, accumulation).")
    p_market_status.add_argument("--price-action", action="store_true", help="Include price-action module (S/R, structure, breakouts, patterns).")
    p_market_status.add_argument("--execution", action="store_true", help="Include execution-quality metrics (spread/slippage/depth).")
    p_market_status.add_argument("--quant", action="store_true", help="Include quant metrics (correlation/stat signals/ML features).")
    p_market_status.add_argument("--crypto", action="store_true", help="Include crypto-specific metrics (funding/open interest).")
    p_market_status.add_argument("--volume-window-fast", type=int, default=None, help="Fast volume MA window (default from config).")
    p_market_status.add_argument("--volume-window-slow", type=int, default=None, help="Slow volume MA window (default from config).")
    p_market_status.add_argument("--volume-spike-ratio", type=float, default=None, help="Spike ratio threshold (default from config).")
    p_market_status.add_argument("--volume-zscore", type=float, default=None, help="Volume z-score threshold (default from config).")
    p_market_status.add_argument("--volume-buy-ratio", type=float, default=None, help="Taker buy ratio for buy pressure (default from config).")
    p_market_status.add_argument("--volume-sell-ratio", type=float, default=None, help="Taker buy ratio for sell pressure (default from config).")
    p_market_status.add_argument("--volume-depth", type=int, default=None, help="Order book depth limit for liquidity metrics.")
    p_market_status.add_argument("--volume-wall-ratio", type=float, default=None, help="Wall size multiple vs median (default from config).")
    p_market_status.add_argument("--volume-imbalance", type=float, default=None, help="Book imbalance threshold (default from config).")
    p_market_status.add_argument("--execution-depth", type=int, default=None, help="Order book depth levels for execution metrics (default 10).")
    p_market_status.add_argument("--execution-notional", type=float, default=None, help="Notional size (quote) for slippage estimate (default 1000).")
    p_market_status.add_argument("--execution-side", type=str, default=None, help="Side for slippage simulation (buy|sell).")
    p_market_status.add_argument("--risk", action="store_true", help="Include risk sizing/stop/TP metrics.")
    p_market_status.add_argument("--risk-side", type=str, default=None, help="Risk side for stop/TP sizing (long|short).")
    p_market_status.add_argument("--risk-entry", type=float, default=None, help="Override entry price for risk sizing.")
    p_market_status.add_argument("--risk-pct", type=float, default=None, help="Risk percent of account (default 1).")
    p_market_status.add_argument("--risk-account-balance", type=float, default=None, help="Account balance to use for sizing (quote).")
    p_market_status.add_argument("--risk-max-position-pct", type=float, default=None, help="Max position %% cap (default 20).")
    p_market_status.add_argument("--benchmark", type=str, default=None, help="Benchmark symbol for quant correlation (default BTCUSDT).")
    p_market_status.add_argument("--quant-window", type=int, default=None, help="Quant lookback window in bars (default 200).")
    p_market_status.add_argument("--corr-method", type=str, default=None, help="Correlation method (default pearson).")
    p_market_status.add_argument("--strict", action="store_true", help="Fail if requested indicators are unavailable.")
    p_market_status.add_argument("--debug", action="store_true", help="Print additional indicator debug values.")
    p_market_status.set_defaults(fn=cmd_market_status)

    p_market_snapshot = market_sub.add_parser("snapshot", help="Market snapshots (list/show).")
    _add_common_paths(p_market_snapshot)
    market_snap_sub = p_market_snapshot.add_subparsers(dest="market_snap_cmd", required=True)

    p_market_snap_list = market_snap_sub.add_parser("list", help="List market snapshots.")
    _add_common_paths(p_market_snap_list)
    p_market_snap_list.add_argument("--limit", type=int, default=50, help="Limit rows (default: 50).")
    p_market_snap_list.add_argument("--symbol", type=str, default=None, help="Filter by symbol (e.g. SOLUSDT).")
    p_market_snap_list.add_argument("--timeframe", type=str, default=None, help="Filter by timeframe (e.g. 1h).")
    p_market_snap_list.set_defaults(fn=cmd_market_snapshot_list)

    p_market_snap_show = market_snap_sub.add_parser("show", help="Show one market snapshot.")
    _add_common_paths(p_market_snap_show)
    p_market_snap_show.add_argument("id", type=int, help="Snapshot id.")
    p_market_snap_show.set_defaults(fn=cmd_market_snapshot_show)

    p_dust = sub.add_parser("dust", help="Dust ledger (accounting-only leftover balances).")
    _add_common_paths(p_dust)
    dust_sub = p_dust.add_subparsers(dest="dust_cmd", required=True)

    p_dust_list = dust_sub.add_parser("list", help="List dust ledger rows.")
    _add_common_paths(p_dust_list)
    p_dust_list.add_argument("--limit", type=int, default=200, help="Limit rows (default: 200).")
    p_dust_list.set_defaults(fn=cmd_dust_list)

    p_dust_show = dust_sub.add_parser("show", help="Show one dust ledger asset.")
    _add_common_paths(p_dust_show)
    p_dust_show.add_argument("asset", help="Asset symbol (e.g. SOL).")
    p_dust_show.set_defaults(fn=cmd_dust_show)

    p_pnl = sub.add_parser("pnl", help="Profit and loss helpers (realized/unrealized).")
    _add_common_paths(p_pnl)
    pnl_sub = p_pnl.add_subparsers(dest="pnl_cmd", required=True)

    p_pnl_real = pnl_sub.add_parser("realized", help="Realized PnL from SELL executions (Phase 7).")
    _add_common_paths(p_pnl_real)
    real_sub = p_pnl_real.add_subparsers(dest="real_cmd", required=False)
    p_pnl_real.add_argument("--limit", type=int, default=50, help="Limit rows (default: 50).")
    p_pnl_real.set_defaults(fn=cmd_pnl_realized_list)

    p_pnl_real_show = real_sub.add_parser("show", help="Show realized PnL details for one execution.")
    _add_common_paths(p_pnl_real_show)
    p_pnl_real_show.add_argument("execution_id", type=int, help="Execution id.")
    p_pnl_real_show.set_defaults(fn=cmd_pnl_realized_show)

    p_pnl_unreal = pnl_sub.add_parser("unrealized", help="Unrealized PnL for open positions (Phase 8).")
    _add_common_paths(p_pnl_unreal)
    _add_exchange_tls_args(p_pnl_unreal)
    p_pnl_unreal.add_argument("--position-id", type=int, default=None, help="Optional position id.")
    p_pnl_unreal.add_argument("--limit", type=int, default=50, help="Limit rows (default: 50).")
    p_pnl_unreal.add_argument("--no-live", action="store_true", help="Do not fetch live price; print entry/qty only.")
    p_pnl_unreal.set_defaults(fn=cmd_pnl_unrealized)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # Best-effort lifecycle metadata for Phase 3 recovery readiness.
    if hasattr(args, "config") and hasattr(args, "db"):
        paths = ConfigPaths.from_cli(config_path=getattr(args, "config"), db_path=getattr(args, "db"))
        config_path = ensure_default_config(paths.config_path)
        db_path = ensure_db_initialized(config_path=config_path, db_path=paths.db_path)
        cfg = load_config(config_path)
        mode = "TESTNET" if cfg.binance_testnet else "MAINNET"
        try:
            with connect(db_path) as conn:
                StateManager(conn).update_system_start(current_mode=mode)
        except Exception:
            pass

        def _shutdown_hook() -> None:
            try:
                with connect(db_path) as conn:
                    StateManager(conn).update_system_shutdown()
            except Exception:
                return

        import atexit

        atexit.register(_shutdown_hook)

    return int(args.fn(args))
