from __future__ import annotations

import ssl
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from cryptogent.config.model import AppConfig
from cryptogent.exchange.binance_errors import BinanceAuthError
from cryptogent.exchange.binance_http import ms_timestamp, request_json, with_query
from cryptogent.exchange.binance_signing import hmac_sha256_hex
from cryptogent.exchange.interfaces import Balance, SpotExchangeClient


@dataclass(frozen=True)
class BinanceSpotClient(SpotExchangeClient):
    base_url: str
    api_key: str | None
    api_secret: str | None
    recv_window_ms: int = 5000
    timeout_s: float = 10.0
    tls_verify: bool = True
    ca_bundle_path: Path | None = None

    @staticmethod
    def from_config(cfg: AppConfig) -> "BinanceSpotClient":
        return BinanceSpotClient(
            base_url=cfg.binance_base_url,
            api_key=cfg.binance_api_key,
            api_secret=cfg.binance_api_secret,
            recv_window_ms=cfg.binance_recv_window_ms,
            timeout_s=cfg.binance_timeout_s,
            tls_verify=cfg.binance_tls_verify,
            ca_bundle_path=cfg.binance_ca_bundle_path,
        )

    def _url(self, path: str) -> str:
        return self.base_url.rstrip("/") + path

    def _ssl_context(self) -> ssl.SSLContext:
        if not self.tls_verify:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx
        if self.ca_bundle_path:
            return ssl.create_default_context(cafile=str(self.ca_bundle_path))
        return ssl.create_default_context()

    def _headers(self, *, signed: bool) -> dict[str, str]:
        headers: dict[str, str] = {"Accept": "application/json"}
        if signed:
            if not self.api_key:
                raise BinanceAuthError(status=0, code=None, msg="Missing BINANCE_API_KEY", body=None)
            headers["X-MBX-APIKEY"] = self.api_key
        return headers

    def _signed_url(self, path: str, params: dict[str, str | int | float] | None = None) -> str:
        if not self.api_secret:
            raise BinanceAuthError(status=0, code=None, msg="Missing BINANCE_API_SECRET", body=None)
        params = dict(params or {})
        params.setdefault("timestamp", ms_timestamp())
        params.setdefault("recvWindow", self.recv_window_ms)

        # Binance signs the query string (not the full URL).
        query = urllib.parse.urlencode([(k, str(v)) for k, v in sorted(params.items(), key=lambda kv: kv[0])])
        signature = hmac_sha256_hex(secret=self.api_secret, payload=query)
        full_query = f"{query}&signature={signature}"
        return self._url(path) + "?" + full_query

    def ping(self) -> None:
        url = self._url("/api/v3/ping")
        request_json(
            method="GET",
            url=url,
            headers=self._headers(signed=False),
            timeout_s=self.timeout_s,
            ssl_context=self._ssl_context(),
        )

    def get_server_time_ms(self) -> int:
        url = self._url("/api/v3/time")
        resp = request_json(
            method="GET",
            url=url,
            headers=self._headers(signed=False),
            timeout_s=self.timeout_s,
            ssl_context=self._ssl_context(),
        )
        if not isinstance(resp.data, dict) or "serverTime" not in resp.data:
            raise RuntimeError("Unexpected /api/v3/time response")
        return int(resp.data["serverTime"])

    def get_exchange_info(self, *, symbol: str | None = None) -> dict:
        url = self._url("/api/v3/exchangeInfo")
        if symbol:
            url = with_query(url, {"symbol": symbol})
        resp = request_json(
            method="GET",
            url=url,
            headers=self._headers(signed=False),
            timeout_s=self.timeout_s,
            ssl_context=self._ssl_context(),
        )
        if not isinstance(resp.data, dict):
            raise RuntimeError("Unexpected /api/v3/exchangeInfo response")
        return resp.data

    def get_symbol_info(self, *, symbol: str) -> dict | None:
        info = self.get_exchange_info(symbol=symbol)
        symbols = info.get("symbols")
        if not isinstance(symbols, list) or not symbols:
            return None
        first = symbols[0]
        return first if isinstance(first, dict) else None

    def get_ticker_price(self, *, symbol: str) -> str:
        url = self._url("/api/v3/ticker/price")
        url = with_query(url, {"symbol": symbol})
        resp = request_json(
            method="GET",
            url=url,
            headers=self._headers(signed=False),
            timeout_s=self.timeout_s,
            ssl_context=self._ssl_context(),
        )
        if not isinstance(resp.data, dict) or "price" not in resp.data:
            raise RuntimeError("Unexpected /api/v3/ticker/price response")
        return str(resp.data["price"])

    def get_ticker_24hr(self, *, symbol: str) -> dict:
        url = self._url("/api/v3/ticker/24hr")
        url = with_query(url, {"symbol": symbol})
        resp = request_json(
            method="GET",
            url=url,
            headers=self._headers(signed=False),
            timeout_s=self.timeout_s,
            ssl_context=self._ssl_context(),
        )
        if not isinstance(resp.data, dict):
            raise RuntimeError("Unexpected /api/v3/ticker/24hr response")
        return resp.data

    def get_book_ticker(self, *, symbol: str) -> dict:
        url = self._url("/api/v3/ticker/bookTicker")
        url = with_query(url, {"symbol": symbol})
        resp = request_json(
            method="GET",
            url=url,
            headers=self._headers(signed=False),
            timeout_s=self.timeout_s,
            ssl_context=self._ssl_context(),
        )
        if not isinstance(resp.data, dict):
            raise RuntimeError("Unexpected /api/v3/ticker/bookTicker response")
        return resp.data

    def get_klines(self, *, symbol: str, interval: str, limit: int) -> list[list]:
        url = self._url("/api/v3/klines")
        url = with_query(url, {"symbol": symbol, "interval": interval, "limit": int(limit)})
        resp = request_json(
            method="GET",
            url=url,
            headers=self._headers(signed=False),
            timeout_s=self.timeout_s,
            ssl_context=self._ssl_context(),
        )
        if not isinstance(resp.data, list):
            raise RuntimeError("Unexpected /api/v3/klines response")
        out: list[list] = []
        for row in resp.data:
            if isinstance(row, list):
                out.append(row)
        return out

    def get_account(self) -> dict:
        url = self._signed_url("/api/v3/account")
        resp = request_json(
            method="GET",
            url=url,
            headers=self._headers(signed=True),
            timeout_s=self.timeout_s,
            ssl_context=self._ssl_context(),
        )
        if not isinstance(resp.data, dict):
            raise RuntimeError("Unexpected /api/v3/account response")
        return resp.data

    def get_open_orders(self, *, symbol: str | None = None) -> list[dict]:
        params: dict[str, str] = {}
        if symbol:
            params["symbol"] = symbol
        url = self._signed_url("/api/v3/openOrders", params=params if params else None)
        resp = request_json(
            method="GET",
            url=url,
            headers=self._headers(signed=True),
            timeout_s=self.timeout_s,
            ssl_context=self._ssl_context(),
        )
        if not isinstance(resp.data, list):
            raise RuntimeError("Unexpected /api/v3/openOrders response")
        out: list[dict] = []
        for item in resp.data:
            if isinstance(item, dict):
                out.append(item)
        return out

    def get_balances(self) -> list[Balance]:
        account = self.get_account()
        balances = account.get("balances", [])
        out: list[Balance] = []
        if not isinstance(balances, list):
            return out
        for b in balances:
            if not isinstance(b, dict):
                continue
            asset = str(b.get("asset") or "")
            if not asset:
                continue
            out.append(Balance(asset=asset, free=str(b.get("free") or "0"), locked=str(b.get("locked") or "0")))
        return out

    def create_order_market_buy_quote(
        self,
        *,
        symbol: str,
        quote_order_qty: str,
        client_order_id: str,
    ) -> dict:
        params: dict[str, str] = {
            "symbol": symbol,
            "side": "BUY",
            "type": "MARKET",
            "quoteOrderQty": quote_order_qty,
            "newClientOrderId": client_order_id,
            "newOrderRespType": "FULL",
        }
        url = self._signed_url("/api/v3/order", params=params)
        resp = request_json(
            method="POST",
            url=url,
            headers=self._headers(signed=True),
            timeout_s=self.timeout_s,
            ssl_context=self._ssl_context(),
        )
        if not isinstance(resp.data, dict):
            raise RuntimeError("Unexpected POST /api/v3/order response")
        return resp.data

    def create_order_market_sell_qty(
        self,
        *,
        symbol: str,
        quantity: str,
        client_order_id: str,
    ) -> dict:
        params: dict[str, str] = {
            "symbol": symbol,
            "side": "SELL",
            "type": "MARKET",
            "quantity": quantity,
            "newClientOrderId": client_order_id,
            "newOrderRespType": "FULL",
        }
        url = self._signed_url("/api/v3/order", params=params)
        resp = request_json(
            method="POST",
            url=url,
            headers=self._headers(signed=True),
            timeout_s=self.timeout_s,
            ssl_context=self._ssl_context(),
        )
        if not isinstance(resp.data, dict):
            raise RuntimeError("Unexpected POST /api/v3/order response")
        return resp.data

    def create_order_limit_buy(
        self,
        *,
        symbol: str,
        price: str,
        quantity: str,
        client_order_id: str,
        time_in_force: str = "GTC",
    ) -> dict:
        params: dict[str, str] = {
            "symbol": symbol,
            "side": "BUY",
            "type": "LIMIT",
            "timeInForce": time_in_force,
            "price": price,
            "quantity": quantity,
            "newClientOrderId": client_order_id,
            "newOrderRespType": "FULL",
        }
        url = self._signed_url("/api/v3/order", params=params)
        resp = request_json(
            method="POST",
            url=url,
            headers=self._headers(signed=True),
            timeout_s=self.timeout_s,
            ssl_context=self._ssl_context(),
        )
        if not isinstance(resp.data, dict):
            raise RuntimeError("Unexpected POST /api/v3/order response")
        return resp.data

    def create_order_limit_sell(
        self,
        *,
        symbol: str,
        price: str,
        quantity: str,
        client_order_id: str,
        time_in_force: str = "GTC",
    ) -> dict:
        params: dict[str, str] = {
            "symbol": symbol,
            "side": "SELL",
            "type": "LIMIT",
            "timeInForce": time_in_force,
            "price": price,
            "quantity": quantity,
            "newClientOrderId": client_order_id,
            "newOrderRespType": "FULL",
        }
        url = self._signed_url("/api/v3/order", params=params)
        resp = request_json(
            method="POST",
            url=url,
            headers=self._headers(signed=True),
            timeout_s=self.timeout_s,
            ssl_context=self._ssl_context(),
        )
        if not isinstance(resp.data, dict):
            raise RuntimeError("Unexpected POST /api/v3/order response")
        return resp.data

    def get_order_by_client_order_id(self, *, symbol: str, client_order_id: str) -> dict:
        params: dict[str, str] = {"symbol": symbol, "origClientOrderId": client_order_id}
        url = self._signed_url("/api/v3/order", params=params)
        resp = request_json(
            method="GET",
            url=url,
            headers=self._headers(signed=True),
            timeout_s=self.timeout_s,
            ssl_context=self._ssl_context(),
        )
        if not isinstance(resp.data, dict):
            raise RuntimeError("Unexpected GET /api/v3/order response")
        return resp.data

    def get_order_by_order_id(self, *, symbol: str, order_id: str) -> dict:
        params: dict[str, str] = {"symbol": symbol, "orderId": str(order_id)}
        url = self._signed_url("/api/v3/order", params=params)
        resp = request_json(
            method="GET",
            url=url,
            headers=self._headers(signed=True),
            timeout_s=self.timeout_s,
            ssl_context=self._ssl_context(),
        )
        if not isinstance(resp.data, dict):
            raise RuntimeError("Unexpected GET /api/v3/order response")
        return resp.data

    def cancel_order_by_client_order_id(self, *, symbol: str, client_order_id: str) -> dict:
        params: dict[str, str] = {"symbol": symbol, "origClientOrderId": client_order_id}
        url = self._signed_url("/api/v3/order", params=params)
        resp = request_json(
            method="DELETE",
            url=url,
            headers=self._headers(signed=True),
            timeout_s=self.timeout_s,
            ssl_context=self._ssl_context(),
        )
        if not isinstance(resp.data, dict):
            raise RuntimeError("Unexpected DELETE /api/v3/order response")
        return resp.data

    def get_spot_bnb_burn(self) -> bool:
        """
        Returns whether "pay fees with BNB" (spotBNBBurn) is enabled.
        Endpoint: GET /sapi/v1/bnbBurn (USER_DATA, signed)
        """
        url = self._signed_url("/sapi/v1/bnbBurn", params={})
        resp = request_json(
            method="GET",
            url=url,
            headers=self._headers(signed=True),
            timeout_s=self.timeout_s,
            ssl_context=self._ssl_context(),
        )
        if not isinstance(resp.data, dict):
            raise RuntimeError("Unexpected GET /sapi/v1/bnbBurn response")
        val = resp.data.get("spotBNBBurn")
        if isinstance(val, bool):
            return val
        if isinstance(val, (int, float)):
            return bool(val)
        if isinstance(val, str):
            return val.strip().lower() in ("1", "true", "yes", "y", "on")
        raise RuntimeError("Unexpected GET /sapi/v1/bnbBurn payload (missing spotBNBBurn)")

    def set_spot_bnb_burn(self, *, enabled: bool) -> bool:
        """
        Sets "pay fees with BNB" (spotBNBBurn).
        Endpoint: POST /sapi/v1/bnbBurn (USER_DATA, signed)
        """
        params: dict[str, str] = {"spotBNBBurn": "true" if enabled else "false"}
        url = self._signed_url("/sapi/v1/bnbBurn", params=params)
        resp = request_json(
            method="POST",
            url=url,
            headers=self._headers(signed=True),
            timeout_s=self.timeout_s,
            ssl_context=self._ssl_context(),
        )
        if not isinstance(resp.data, dict):
            raise RuntimeError("Unexpected POST /sapi/v1/bnbBurn response")
        # Response commonly includes booleans for spotBNBBurn/interestBNBBurn; treat as best-effort.
        val = resp.data.get("spotBNBBurn")
        if isinstance(val, bool):
            return val
        return enabled
