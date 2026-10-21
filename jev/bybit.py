"""Bybit v5 REST Client with Dual-Host Failover and Rate Limiting."""

import time
import hmac
import hashlib
import json
import logging
import requests
from typing import Dict, Any, Optional, List

logger = logging.getLogger("jevtrader.bybit")

class BybitClient:
    def __init__(self, api_key: str = "", api_secret: str = "", hosts: Optional[List[str]] = None):
        self.api_key = api_key
        self.api_secret = api_secret
        self.hosts = hosts or ["https://api.bytick.com", "https://api.bybit.com"]
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "JevTrader-Apex/1.0",
            "Content-Type": "application/json"
        })

    def _sign(self, timestamp: str, payload_str: str) -> str:
        param_str = f"{timestamp}{self.api_key}5000{payload_str}"
        return hmac.new(
            self.api_secret.encode('utf-8'),
            param_str.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()

    def _request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None, data: Optional[Dict[str, Any]] = None, is_private: bool = False) -> Dict[str, Any]:
        params = params or {}
        data = data or {}
        last_error = None

        for host in self.hosts:
            url = f"{host}{path}"
            headers = dict(self.session.headers)

            if is_private and self.api_key and self.api_secret:
                ts = str(int(time.time() * 1000))
                payload_str = json.dumps(data) if method == "POST" else "&".join(f"{k}={v}" for k, v in sorted(params.items()))
                signature = self._sign(ts, payload_str)
                headers.update({
                    "X-BAPI-API-KEY": self.api_key,
                    "X-BAPI-TIMESTAMP": ts,
                    "X-BAPI-SIGN": signature,
                    "X-BAPI-RECV-WINDOW": "5000"
                })

            try:
                if method == "GET":
                    resp = self.session.get(url, params=params, headers=headers, timeout=6)
                else:
                    resp = self.session.post(url, json=data, headers=headers, timeout=6)

                if resp.status_code == 200:
                    res_json = resp.json()
                    ret_code = res_json.get("retCode", -1)
                    if ret_code == 0:
                        return res_json.get("result", {})
                    else:
                        raise RuntimeError(f"Bybit API Error {ret_code}: {res_json.get('retMsg')}")
                elif resp.status_code == 429:
                    logger.warning("Bybit Rate Limit hit, backing off 500ms...")
                    time.sleep(0.5)
                else:
                    last_error = f"HTTP {resp.status_code}: {resp.text}"
            except Exception as e:
                last_error = str(e)
                continue

        raise ConnectionError(f"All Bybit hosts failed for {path}. Last error: {last_error}")

    # ================= PUBLIC MARKET DATA =================

    def get_all_linear_tickers(self) -> List[Dict[str, Any]]:
        """Fetch all linear perpetual tickers in a single call."""
        res = self._request("GET", "/v5/market/tickers", {"category": "linear"})
        return res.get("list", [])

    def get_orderbook(self, symbol: str, limit: int = 50) -> Dict[str, Any]:
        """Fetch orderbook depth."""
        return self._request("GET", "/v5/market/orderbook", {
            "category": "linear",
            "symbol": symbol,
            "limit": limit
        })

    def get_recent_trades(self, symbol: str, limit: int = 100) -> List[Dict[str, Any]]:
        """Fetch recent trades to compute Taker Buy/Sell volume (CVD)."""
        res = self._request("GET", "/v5/market/recent-trade", {
            "category": "linear",
            "symbol": symbol,
            "limit": limit
        })
        return res.get("list", [])

    def get_klines(self, symbol: str, interval: str = "15", limit: int = 50) -> List[Dict[str, Any]]:
        """Fetch klines: [startTime, openPrice, highPrice, lowPrice, closePrice, volume, turnover]."""
        res = self._request("GET", "/v5/market/kline", {
            "category": "linear",
            "symbol": symbol,
            "interval": interval,
            "limit": limit
        })
        return res.get("list", [])

    def get_instruments_info(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetch precision, lot size, minNotional."""
        params = {"category": "linear"}
        if symbol:
            params["symbol"] = symbol
        res = self._request("GET", "/v5/market/instruments-info", params)
        return res.get("list", [])

    # ================= PRIVATE ACCOUNT & ORDERS =================

    def get_wallet_balance(self, coin: str = "USDT") -> float:
        """Fetch available equity in USDT."""
        res = self._request("GET", "/v5/account/wallet-balance", {
            "accountType": "UNIFIED",
            "coin": coin
        }, is_private=True)
        accounts = res.get("list", [])
        if not accounts:
            return 0.0
        coins = accounts[0].get("coin", [])
        for c in coins:
            if c.get("coin") == coin:
                return float(c.get("equity") or c.get("walletBalance") or 0.0)
        return float(accounts[0].get("totalEquity") or 0.0)

    def get_positions(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetch active positions."""
        params = {"category": "linear", "settleCoin": "USDT"}
        if symbol:
            params["symbol"] = symbol
        res = self._request("GET", "/v5/position/list", params, is_private=True)
        return res.get("list", [])

    def place_order(self, symbol: str, side: str, order_type: str, qty: float, 
                    price: Optional[float] = None, stop_loss: Optional[float] = None, 
                    take_profit: Optional[float] = None, reduce_only: bool = False,
                    post_only: bool = False, order_link_id: Optional[str] = None) -> Dict[str, Any]:
        """Send an order with native exchange Stop-Loss and Take-Profit."""
        data: Dict[str, Any] = {
            "category": "linear",
            "symbol": symbol,
            "side": side,
            "orderType": order_type,
            "qty": str(qty),
            "positionIdx": 0 # One-way mode
        }
        if price is not None:
            data["price"] = str(price)
        if reduce_only:
            data["reduceOnly"] = True
        if post_only:
            data["timeInForce"] = "PostOnly"
        if order_link_id:
            data["orderLinkId"] = order_link_id
        if stop_loss is not None:
            data["stopLoss"] = str(stop_loss)
            data["slTriggerBy"] = "MarkPrice" # MarkPrice protects from scam wicks!
        if take_profit is not None:
            data["takeProfit"] = str(take_profit)
            data["tpTriggerBy"] = "LastPrice" # LastPrice harvests quick wicks!

        return self._request("POST", "/v5/order/create", data=data, is_private=True)

    def set_trading_stop(self, symbol: str, stop_loss: Optional[float] = None,
                         take_profit: Optional[float] = None) -> Dict[str, Any]:
        """Ratchet existing position stop-loss to Break-Even."""
        data: Dict[str, Any] = {
            "category": "linear",
            "symbol": symbol,
            "positionIdx": 0
        }
        if stop_loss is not None:
            data["stopLoss"] = str(stop_loss)
            data["slTriggerBy"] = "MarkPrice"
        if take_profit is not None:
            data["takeProfit"] = str(take_profit)
            data["tpTriggerBy"] = "LastPrice"

        return self._request("POST", "/v5/position/trading-stop", data=data, is_private=True)

    def set_leverage(self, symbol: str, buy_leverage: float, sell_leverage: Optional[float] = None) -> Dict[str, Any]:
        """Set leverage for linear perpetual symbol."""
        sell_lev = sell_leverage or buy_leverage
        data = {
            "category": "linear",
            "symbol": symbol,
            "buyLeverage": str(buy_leverage),
            "sellLeverage": str(sell_lev)
        }
        try:
            return self._request("POST", "/v5/position/set-leverage", data=data, is_private=True)
        except RuntimeError as e:
            # Code 110043 = "Set leverage not modified", which is safe to ignore
            if "110043" in str(e):
                logger.debug(f"Leverage for {symbol} already set to {buy_leverage}x")
                return {"retCode": 0, "retMsg": "OK"}
            raise

    def cancel_order(self, symbol: str, order_link_id: str) -> Dict[str, Any]:
        """Cancel a pending limit or post-only order."""
        return self._request("POST", "/v5/order/cancel", {
            "category": "linear",
            "symbol": symbol,
            "orderLinkId": order_link_id
        }, is_private=True)

