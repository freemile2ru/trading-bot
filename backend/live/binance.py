"""Binance USDⓈ-M futures REST client (signed) — testnet or mainnet per live/config.py.

Sync on purpose: the swing wallets call it from worker threads under their own lock; the
scalper wraps calls in asyncio.to_thread. Every request/response goes to the audit hook
with the signature and keys stripped.

Conditional orders: in late 2025 Binance began moving STOP_MARKET / TAKE_PROFIT_MARKET to a
separate "algo order" endpoint on some accounts. `stop_market` tries the classic endpoint and
falls back to /fapi/v1/algoOrder if the exchange refuses the order type there (-4120).
selfcheck.py exercises both paths against the testnet.
"""

import hashlib
import hmac
import math
import time
import uuid
from typing import Any, Callable
from urllib.parse import urlencode

import httpx

from live import config as LC

_info_cache: dict[str, tuple[float, dict]] = {}


class BinanceError(Exception):
    def __init__(self, status: int, code: int | None, msg: str):
        super().__init__(f"Binance {status} {code}: {msg}")
        self.status, self.code, self.msg = status, code, msg


def client_id(prefix: str) -> str:
    """Our own order id (≤36 chars, [.A-Z:/a-z0-9_-]) — ties exchange orders back to ledger rows."""
    return f"{prefix}-{uuid.uuid4().hex[:20]}"


class BinanceFutures:
    def __init__(self, api_key: str, secret: str, rest: str = LC.REST,
                 audit: Callable[[dict], None] | None = None, system: str = ""):
        self._key, self._secret = api_key, secret.encode()
        self.rest, self.system, self._audit = rest.rstrip("/"), system, audit
        self._algo_stops = False              # set once the exchange refuses stops on the classic endpoint (-4120)
        self._http = httpx.Client(timeout=10.0, headers={"X-MBX-APIKEY": api_key})
        self._offset_ms = 0
        self._synced = False

    # ── Transport ─────────────────────────────────────────────────────────────

    def _sync_time(self) -> None:
        server = self._http.get(f"{self.rest}/fapi/v1/time").json()["serverTime"]
        self._offset_ms = server - int(time.time() * 1000)
        self._synced = True

    def request(self, method: str, path: str, params: dict | None = None, signed: bool = True) -> Any:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        for attempt in (1, 2):
            q = dict(params)
            if signed:
                if not self._synced:
                    self._sync_time()
                q["recvWindow"] = LC.RECV_WINDOW_MS
                q["timestamp"] = int(time.time() * 1000) + self._offset_ms
                qs = urlencode(q, doseq=True)
                qs += "&signature=" + hmac.new(self._secret, qs.encode(), hashlib.sha256).hexdigest()
            else:
                qs = urlencode(q, doseq=True)
            t0 = time.time()
            try:
                r = self._http.request(method, f"{self.rest}{path}?{qs}")
                body = r.json() if r.content else {}
            except (httpx.HTTPError, ValueError) as e:
                self._log(method, path, params, None, {"error": str(e)}, t0)
                raise BinanceError(0, None, f"network: {e}") from e
            self._log(method, path, params, r.status_code, body, t0)
            if r.status_code < 400:
                return body
            code = body.get("code") if isinstance(body, dict) else None
            if code == -1021 and attempt == 1:          # timestamp outside recvWindow: resync once
                self._sync_time()
                continue
            raise BinanceError(r.status_code, code, (body or {}).get("msg", r.text) if isinstance(body, dict) else r.text)
        raise BinanceError(0, None, "unreachable")

    def _log(self, method, path, params, status, body, t0) -> None:
        if self._audit:
            self._audit({"ts": time.time(), "system": self.system, "method": method, "path": path,
                         "params": params, "status": status, "response": body,
                         "ms": round((time.time() - t0) * 1000)})

    # ── Symbol rules ──────────────────────────────────────────────────────────

    def exchange_info(self) -> dict:
        hit = _info_cache.get(self.rest)
        if hit and time.time() - hit[0] < 3600:
            return hit[1]
        info = self.request("GET", "/fapi/v1/exchangeInfo", signed=False)
        rules = {}
        for s in info["symbols"]:
            f = {x["filterType"]: x for x in s["filters"]}
            rules[s["symbol"]] = {
                "tick": float(f["PRICE_FILTER"]["tickSize"]), "step": float(f["LOT_SIZE"]["stepSize"]),
                "min_qty": float(f["LOT_SIZE"]["minQty"]),
                "market_step": float(f.get("MARKET_LOT_SIZE", f["LOT_SIZE"])["stepSize"]),
                "min_notional": float(f.get("MIN_NOTIONAL", {}).get("notional", 0) or 0),
                "status": s.get("status"),
            }
        _info_cache[self.rest] = (time.time(), rules)
        return rules

    def rules(self, symbol: str) -> dict:
        r = self.exchange_info().get(symbol)
        if not r:
            raise BinanceError(400, None, f"{symbol} is not listed on this exchange ({LC.ENV})")
        return r

    @staticmethod
    def _floor(x: float, step: float) -> float:
        n = math.floor(x / step + 1e-9)
        return round(n * step, max(0, -int(math.floor(math.log10(step)))))

    def qty(self, symbol: str, qty: float, market: bool = True) -> float:
        r = self.rules(symbol)
        return self._floor(qty, r["market_step"] if market else r["step"])

    def price(self, symbol: str, price: float) -> float:
        tick = self.rules(symbol)["tick"]
        return round(round(price / tick) * tick, max(0, -int(math.floor(math.log10(tick)))))

    # ── Account ───────────────────────────────────────────────────────────────

    def account(self) -> dict:
        return self.request("GET", "/fapi/v2/account")

    def equity(self) -> dict:
        a = self.account()
        return {"wallet": float(a["totalWalletBalance"]), "unrealized": float(a["totalUnrealizedProfit"]),
                "equity": float(a["totalMarginBalance"]), "available": float(a["availableBalance"])}

    def positions(self, symbol: str | None = None) -> list[dict]:
        rows = self.request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
        return [r for r in rows if float(r.get("positionAmt", 0)) != 0]

    def hedge_mode(self) -> bool:
        return bool(self.request("GET", "/fapi/v1/positionSide/dual").get("dualSidePosition"))

    def prepare(self, symbol: str, leverage: int) -> None:
        """Isolated margin and the requested leverage (Binance errors if already set: ignored)."""
        try:
            self.request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": "ISOLATED"})
        except BinanceError as e:
            if e.code != -4046:          # "No need to change margin type."
                raise
        self.request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": int(leverage)})

    # ── Orders ────────────────────────────────────────────────────────────────

    def market(self, symbol: str, side: str, qty: float, reduce_only: bool = False, cid: str | None = None) -> dict:
        return self.request("POST", "/fapi/v1/order", {
            "symbol": symbol, "side": side, "type": "MARKET", "quantity": self.qty(symbol, qty),
            "reduceOnly": "true" if reduce_only else None, "newClientOrderId": cid or client_id("mkt"),
            "newOrderRespType": "RESULT"})

    def market_confirmed(self, symbol: str, side: str, qty: float, reduce_only: bool = False,
                         cid: str | None = None) -> dict:
        """A market order whose outcome is known even when the response is lost.

        A timeout after Binance accepted the order used to be reported as a failure while the
        position was in fact opened (or closed) — an entry nobody booked and no stop protected.
        On a network error the order is looked up by its client id: found → its result is
        returned like a normal response; confirmed absent → the original error is raised."""
        cid = cid or client_id("mkt")
        try:
            return self.market(symbol, side, qty, reduce_only=reduce_only, cid=cid)
        except BinanceError as e:
            if e.status != 0:
                raise
            err = e
        for delay in (0.5, 1.5, 3.0):
            time.sleep(delay)
            try:
                o = self.order(symbol, cid)
            except BinanceError as e:
                if e.code == -2013:              # "Order does not exist" — it never reached the book
                    raise err
                continue                          # still unreachable: ask again
            if o.get("status") in ("NEW", "PARTIALLY_FILLED"):
                continue                          # a market order is about to finish filling
            return o
        raise BinanceError(0, None, f"order {cid} outcome unknown after a network error — check Binance: {err.msg}")

    def limit(self, symbol: str, side: str, qty: float, price: float, post_only: bool = False,
              reduce_only: bool = False, cid: str | None = None) -> dict:
        return self.request("POST", "/fapi/v1/order", {
            "symbol": symbol, "side": side, "type": "LIMIT", "quantity": self.qty(symbol, qty, market=False),
            "price": self.price(symbol, price), "timeInForce": "GTX" if post_only else "GTC",
            "reduceOnly": "true" if reduce_only else None, "newClientOrderId": cid or client_id("lmt"),
            "newOrderRespType": "RESULT"})

    def stop_market(self, symbol: str, side: str, stop: float, cid: str | None = None,
                    kind: str = "STOP_MARKET") -> dict:
        """A close-the-whole-position stop (or TAKE_PROFIT_MARKET) triggered on the mark price.
        Returns {"id", "algo": bool, ...} — `algo` says which endpoint holds it."""
        cid = cid or client_id("stp")
        params = {"symbol": symbol, "side": side, "type": kind, "stopPrice": self.price(symbol, stop),
                  "closePosition": "true", "workingType": "MARK_PRICE", "newClientOrderId": cid}
        # Once the exchange has said this account takes stops only on the algo endpoint, go straight there:
        # the failed classic attempt cost a round trip while the new position sat without a stop.
        if not self._algo_stops:
            try:
                r = self.request("POST", "/fapi/v1/order", params)
                return {**r, "cid": cid, "algo": False}
            except BinanceError as e:
                if e.code != -4120:
                    raise
                self._algo_stops = True
        r = self.request("POST", "/fapi/v1/algoOrder", {
            "algoType": "CONDITIONAL", "symbol": symbol, "side": side, "type": kind,
            "triggerPrice": self.price(symbol, stop), "closePosition": "true", "workingType": "MARK_PRICE",
            "clientAlgoId": cid})
        return {**r, "cid": cid, "algo": True}

    def cancel(self, symbol: str, cid: str, algo: bool = False) -> dict | None:
        """Cancel by our client id; an already-gone order is not an error."""
        try:
            if algo:
                return self.request("DELETE", "/fapi/v1/algoOrder", {"symbol": symbol, "clientAlgoId": cid})
            return self.request("DELETE", "/fapi/v1/order", {"symbol": symbol, "origClientOrderId": cid})
        except BinanceError as e:
            if e.code in (-2011, -2013):             # unknown order / does not exist
                return None
            raise

    def cancel_all(self, symbol: str) -> None:
        self.request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})
        try:
            self.request("DELETE", "/fapi/v1/algoOpenOrders", {"symbol": symbol})
        except BinanceError:
            pass                                    # account without algo orders

    def order(self, symbol: str, cid: str) -> dict:
        return self.request("GET", "/fapi/v1/order", {"symbol": symbol, "origClientOrderId": cid})

    def open_orders(self, symbol: str | None = None) -> list[dict]:
        return self.request("GET", "/fapi/v1/openOrders", {"symbol": symbol})

    def fills(self, symbol: str, start_ms: int) -> list[dict]:
        return self.request("GET", "/fapi/v1/userTrades", {"symbol": symbol, "startTime": start_ms, "limit": 1000})

    def income(self, symbol: str, start_ms: int, income_type: str | None = None) -> list[dict]:
        return self.request("GET", "/fapi/v1/income", {"symbol": symbol, "startTime": start_ms,
                                                       "incomeType": income_type, "limit": 1000})

    # ── User-data stream (order/fill events pushed over a websocket) ─────────

    def listen_key(self) -> str:
        return self.request("POST", "/fapi/v1/listenKey", signed=False)["listenKey"]

    def keepalive(self) -> None:
        self.request("PUT", "/fapi/v1/listenKey", signed=False)

    def close(self) -> None:
        self._http.close()


def summarize_fills(fills: list[dict], order_id: int | None = None) -> dict:
    """Average price, quantity, commission (USDT) and realized P&L of a set of fills."""
    rows = [f for f in fills if order_id is None or f.get("orderId") == order_id]
    qty = sum(float(f["qty"]) for f in rows)
    return {
        "qty": qty,
        "price": sum(float(f["price"]) * float(f["qty"]) for f in rows) / qty if qty else None,
        "commission": sum(float(f["commission"]) for f in rows if f.get("commissionAsset") == "USDT"),
        "realized_pnl": sum(float(f.get("realizedPnl", 0)) for f in rows),
        "maker": all(f.get("maker") for f in rows) if rows else None,
        "time_ms": max((int(f["time"]) for f in rows), default=None),
    }
