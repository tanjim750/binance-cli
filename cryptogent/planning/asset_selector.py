from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from cryptogent.exchange.binance_errors import BinanceAPIError
from cryptogent.exchange.binance_spot import BinanceSpotClient
from cryptogent.market.market_data_service import MarketDataError, MarketSnapshot, fetch_market_snapshot
from cryptogent.planning.feasibility import FeasibilityError, evaluate_feasibility, freshness_and_consistency_checks
from cryptogent.validation.binance_rules import RuleError, SymbolRules, parse_symbol_rules


class AssetSelectionError(RuntimeError):
    pass


def _is_leveraged_token(base_asset: str) -> bool:
    base = (base_asset or "").upper()
    if len(base) < 4:
        return False
    return base.endswith(("UP", "DOWN", "BULL", "BEAR"))


def candidate_universe(preferred_symbol: str | None) -> list[str]:
    if preferred_symbol:
        return [preferred_symbol.strip().upper()]
    return ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"]


@dataclass(frozen=True)
class SelectedAsset:
    symbol: str
    rules: SymbolRules
    snapshot: MarketSnapshot
    candidates: list[dict]


def select_asset(
    *,
    client: BinanceSpotClient,
    preferred_symbol: str | None,
    budget_asset: str,
    profit_target_pct: Decimal,
    stop_loss_pct: Decimal,
    deadline_hours: int,
    candle_interval: str,
    candle_count: int,
) -> SelectedAsset:
    universe = candidate_universe(preferred_symbol)
    excluded: list[dict] = []
    scored: list[dict] = []
    selected: tuple[str, SymbolRules, MarketSnapshot] | None = None

    for sym in universe:
        try:
            info = client.get_symbol_info(symbol=sym)
            if not info:
                excluded.append({"symbol": sym, "reason": "symbol_not_found"})
                continue
            rules = parse_symbol_rules(info)
            if rules.status != "TRADING":
                excluded.append({"symbol": sym, "reason": f"status_{rules.status}"})
                continue
            if rules.quote_asset.upper() != budget_asset.upper():
                excluded.append({"symbol": sym, "reason": "wrong_quote_asset"})
                continue
            if _is_leveraged_token(rules.base_asset):
                excluded.append({"symbol": sym, "reason": "leveraged_token"})
                continue
            if not rules.lot_size or not rules.min_notional:
                excluded.append({"symbol": sym, "reason": "missing_filters"})
                continue

            snapshot = fetch_market_snapshot(
                client=client,
                symbol=rules.symbol,
                candle_interval=candle_interval,
                candle_count=candle_count,
                fetch_book_ticker=True,
            )
            warnings, hard = freshness_and_consistency_checks(
                snapshot=snapshot, candle_interval=candle_interval, candle_count=candle_count
            )
            if hard:
                excluded.append({"symbol": sym, "reason": hard})
                continue

            spread_available = snapshot.bid is not None and snapshot.ask is not None and snapshot.spread_pct is not None
            feas = evaluate_feasibility(
                profit_target_pct=profit_target_pct,
                stop_loss_pct=stop_loss_pct,
                deadline_hours=deadline_hours,
                volume_24h_quote=snapshot.volume_24h_quote,
                volatility_pct=snapshot.candles.volatility_pct,
                spread_pct=snapshot.spread_pct,
                spread_available=spread_available,
                warnings=warnings,
            )
            if feas.category == "not_feasible":
                excluded.append({"symbol": sym, "reason": feas.rejection_reason or "not_feasible"})
                continue

            scored.append(
                {
                    "symbol": rules.symbol,
                    "volume_24h_quote": str(snapshot.volume_24h_quote),
                    "spread_pct": str(snapshot.spread_pct) if snapshot.spread_pct is not None else None,
                    "volatility_pct": str(snapshot.candles.volatility_pct),
                    "momentum_pct": str(snapshot.candles.momentum_pct),
                    "category": feas.category,
                    "warnings": feas.warnings,
                }
            )

            if preferred_symbol:
                selected = (rules.symbol, rules, snapshot)
                break
        except (BinanceAPIError, MarketDataError, FeasibilityError, RuleError) as e:
            excluded.append({"symbol": sym, "reason": f"market_data_error:{e}"})
            continue

    if preferred_symbol:
        if not selected:
            raise AssetSelectionError(f"No feasible plan for requested symbol {preferred_symbol.strip().upper()}")
        symbol, rules, snapshot = selected
        candidates: list[dict] = [{"selected": True, "symbol": symbol, "reason": "requested"}] + [
            {**ex, "excluded": True} for ex in excluded
        ]
        return SelectedAsset(symbol=symbol, rules=rules, snapshot=snapshot, candidates=candidates)

    if not scored:
        raise AssetSelectionError("No suitable asset found in candidate universe")

    def _score_key(r: dict) -> tuple:
        liq = Decimal(r["volume_24h_quote"])
        sp = Decimal(r["spread_pct"]) if r.get("spread_pct") is not None else Decimal("999")
        vol = Decimal(r["volatility_pct"])
        mom = Decimal(r["momentum_pct"])
        return (-liq, sp, vol, -mom, str(r["symbol"]))

    chosen = sorted(scored, key=_score_key)[0]
    chosen_symbol = str(chosen["symbol"])
    info = client.get_symbol_info(symbol=chosen_symbol)
    if not info:
        raise AssetSelectionError("Selected symbol missing from exchangeInfo")
    rules = parse_symbol_rules(info)
    snapshot = fetch_market_snapshot(
        client=client,
        symbol=chosen_symbol,
        candle_interval=candle_interval,
        candle_count=candle_count,
        fetch_book_ticker=True,
    )

    candidates = []
    for c in sorted(scored, key=lambda r: str(r["symbol"])):
        candidates.append({**c, "selected": str(c["symbol"]) == chosen_symbol})
    for ex in excluded:
        candidates.append({**ex, "excluded": True})

    return SelectedAsset(symbol=chosen_symbol, rules=rules, snapshot=snapshot, candidates=candidates)
