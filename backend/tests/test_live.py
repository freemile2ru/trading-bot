"""Live trading layer against a fake exchange — no network, no keys."""

import time

import pytest

from live import config as LC
from live import control
from live.binance import BinanceError, BinanceFutures
from live.swing import LiveWallet


class FakeBinance:
    """Just enough of BinanceFutures: fills market orders at `mark`, holds stops, tracks positions."""

    def __init__(self, mark=100.0, wallet=10_000.0):
        self.mark, self.wallet = mark, wallet
        self.pos: dict[str, float] = {}
        self.stops: dict[str, dict] = {}
        self.trades: list[dict] = []
        self.calls: list[tuple] = []
        self.fail_stop = False
        self.fail_market = False
        self.fail_fills = False
        self.order_status: dict[str, dict] = {}     # cid → what GET /order returns
        self._oid = 0
        self.rest = LC.REST.rstrip("/")

    def rules(self, symbol):
        return {"tick": 0.01, "step": 0.001, "min_qty": 0.001, "market_step": 0.001, "min_notional": 5}

    qty = BinanceFutures.qty
    price = BinanceFutures.price
    _floor = staticmethod(BinanceFutures._floor)

    def equity(self):
        return {"wallet": self.wallet, "unrealized": 0.0, "equity": self.wallet, "available": self.wallet}

    def hedge_mode(self):
        return False

    def prepare(self, symbol, leverage):
        self.calls.append(("prepare", symbol, leverage))

    def _fill(self, symbol, side, qty, price):
        self._oid += 1
        sgn = 1 if side == "BUY" else -1
        self.pos[symbol] = round(self.pos.get(symbol, 0) + sgn * qty, 8)
        self.trades.append({"orderId": self._oid, "side": side, "qty": str(qty), "price": str(price),
                            "commission": str(qty * price * 0.0005), "commissionAsset": "USDT",
                            "realizedPnl": "0", "maker": False, "time": int(time.time() * 1000)})
        return {"orderId": self._oid, "avgPrice": str(price), "executedQty": str(qty)}

    def market(self, symbol, side, qty, reduce_only=False, cid=None):
        self.calls.append(("market", symbol, side, qty, reduce_only))
        if self.fail_market and reduce_only:          # closes fail; entries go through
            raise BinanceError(503, -1001, "Internal error; unable to process your request.")
        return self._fill(symbol, side, qty, self.mark)

    market_confirmed = market

    def stop_market(self, symbol, side, stop, cid=None, kind="STOP_MARKET"):
        if self.fail_stop:
            raise BinanceError(400, -2021, "Order would immediately trigger.")
        self.stops[cid] = {"symbol": symbol, "side": side, "stop": stop, "kind": kind}
        self.calls.append(("stop", symbol, kind, stop))
        return {"cid": cid, "algo": False, "orderId": 1}

    def cancel(self, symbol, cid, algo=False):
        self.calls.append(("cancel", symbol, cid))
        self.stops.pop(cid, None)

    def trigger_stop(self, symbol):
        """The exchange fires the resting stop: position closed at the stop price."""
        cid, o = next((k, v) for k, v in self.stops.items() if v["symbol"] == symbol and v["kind"] == "STOP_MARKET")
        self._fill(symbol, o["side"], abs(self.pos[symbol]), o["stop"])
        del self.stops[cid]

    def positions(self, symbol=None):
        return [{"symbol": s, "positionAmt": str(q)} for s, q in self.pos.items()
                if q and (symbol is None or s == symbol)]

    def fills(self, symbol, start_ms):
        if self.fail_fills:
            raise BinanceError(0, None, "network: read timeout")
        return list(self.trades)

    def income(self, symbol, start_ms, income_type=None):
        return [{"income": "-0.5"}]

    def order(self, symbol, cid):
        return self.order_status.get(cid, {"status": "NEW"})

    def limit(self, symbol, side, qty, price, post_only=False, reduce_only=False, cid=None):
        self.calls.append(("limit", symbol, side, qty, price))
        return {"orderId": 99, "status": "NEW"}


@pytest.fixture
def live(tmp_path, monkeypatch):
    fake = FakeBinance()
    monkeypatch.setattr(LC, "ALLOWED", True)
    monkeypatch.setattr(LC, "keys", lambda s: ("k-" + s, "secret"))
    monkeypatch.setattr(control, "client", lambda s: fake)
    monkeypatch.setattr(control, "LC", LC)
    monkeypatch.setattr(control, "_daily_pnl", {})
    w = LiveWallet("main", str(tmp_path / "swing.db"))
    monkeypatch.setattr(control, "_is_flat", {})
    control.set_mode("main", "live", control.confirmation_phrase("main"))
    return w, fake


def _open(w, **kw):
    return w.open_trade("SOLUSDT", "long", 100.0, 98.0, 103.0, 105.0, risk_pct=1, **kw)


def test_nothing_goes_live_without_the_master_switch(tmp_path, monkeypatch):
    monkeypatch.setattr(LC, "ALLOWED", False)
    with pytest.raises(control.LiveBlocked, match="LIVE_TRADING_ALLOWED"):
        control.set_mode("main", "live", control.confirmation_phrase("main"))
    assert control.is_live("main") is False
    with pytest.raises(control.LiveBlocked):
        control.guard_entry("main", "SOLUSDT", 5)


def test_typed_confirmation_is_required(live):
    with pytest.raises(control.LiveBlocked, match="to confirm"):
        control.set_mode("auto", "live", "yes")


def test_live_open_uses_exchange_fill_and_places_a_stop(live):
    w, fake = live
    fake.mark = 100.2                                   # the exchange filled a little worse
    t = _open(w)
    assert "error" not in t, t
    assert t["mode"] == "live" and t["entry_price"] == 100.2
    assert t["quantity"] == fake.qty("SOLUSDT", t["quantity"])          # rounded to the lot step
    assert t["fees_usd"] == pytest.approx(t["quantity"] * 100.2 * 0.0005, abs=1e-4)   # the exchange's commission
    assert ("stop", "SOLUSDT", "STOP_MARKET", 98.0) in fake.calls
    assert t["risk_usd"] == pytest.approx(abs(100.2 - 98.0) * t["quantity"], abs=0.01)
    assert t["notional_usd"] == pytest.approx(t["quantity"] * 100.2, abs=0.01) and t["margin_usd"] > 0


def test_moving_the_stop_replaces_it_on_the_exchange(live):
    w, fake = live
    t = _open(w)
    fake.mark = 101.0
    moved = w.move_stop(t["id"], 99.0, "test")
    assert moved and moved["exchange_stop"]["price"] == 99.0
    assert [c for c in fake.calls if c[0] == "cancel"]
    assert [v["stop"] for v in fake.stops.values()] == [99.0]           # exactly one live stop


def test_exchange_stop_fill_is_reconciled_with_real_numbers(live):
    w, fake = live
    t = _open(w)
    fake.trigger_stop("SOLUSDT")
    events = w.process_paths({"SOLUSDT": {"bars": [], "price": 98.0}})
    closed = [e for e in events if e["type"] == "closed"]
    assert closed and closed[0]["trade"]["close_reason"] == "stop_loss"
    c = closed[0]["trade"]
    assert c["current_price"] == 98.0 and c["funding_usd"] == 0.5
    assert c["pnl_usd"] == pytest.approx((98.0 - 100.0) * c["quantity"] - c["fees_usd"] - 0.5, abs=0.01)
    assert not w.get_portfolio()["open_trades"]


def test_time_exit_closes_at_market(live):
    w, fake = live
    t = _open(w)
    t_exp = t["expires_at"]
    bar = {"open_time": int((t_exp + 60) * 1000), "open": 100.5, "high": 100.6, "low": 100.4, "close": 100.5}
    fake.mark = 100.5
    events = w.process_paths({"SOLUSDT": {"bars": [bar], "price": 100.5}})
    assert events[0]["trade"]["close_reason"] == "expired"
    assert fake.pos["SOLUSDT"] == 0 and not fake.stops                   # flat, stop cancelled


def test_failed_stop_closes_the_position(live):
    w, fake = live
    fake.fail_stop = True
    r = _open(w)
    assert "error" in r and "closed" in r["error"]
    assert fake.pos["SOLUSDT"] == 0 and not w.get_portfolio()["open_trades"]


def test_daily_loss_limit_halts_new_entries(live, monkeypatch):
    w, fake = live
    monkeypatch.setattr(control, "_daily_pnl", {"main": lambda: -250.0})   # 2.5% of 10k
    with pytest.raises(control.LiveBlocked, match="daily loss"):
        control.guard_entry("main", "ETHUSDT", 5)
    assert "error" in _open(w)
    from storage import state as kv
    kv.delete_sync(kv.key("live", "halt", "main"))


def test_shared_account_blocks_the_same_coin_twice(live, monkeypatch):
    monkeypatch.setattr(LC, "keys", lambda s: ("shared", "secret"))
    with pytest.raises(control.LiveBlocked, match="same Binance account"):
        control.guard_entry("main", "SOLUSDT", 5, others_holding=lambda other, sym: other == "auto")


def test_kill_switch_flattens_and_returns_to_paper(live, monkeypatch):
    w, fake = live
    monkeypatch.setattr(control, "_flatten", {})     # only this test's wallet, never the module-level ones
    _open(w)
    control.register("main", flatten=w.flatten)
    out = control.kill_switch("test")
    assert any("closed SOLUSDT" in x for x in out["main"])
    assert fake.pos["SOLUSDT"] == 0 and control.mode("main") == "paper"
    assert w.get_portfolio()["closed_trades"][-1]["close_reason"] == "kill_switch"


def test_failed_close_keeps_the_exchange_stop(live):
    """Closing used to cancel the stop first; a failed close then left the position naked."""
    w, fake = live
    t = _open(w)
    fake.fail_market = True
    assert w.close_trade_manually(t["id"], 100.0) is None
    assert [v["stop"] for v in fake.stops.values()] == [98.0]          # still protected
    assert w.get_portfolio()["open_trades"][0]["exchange_stop"]


def test_entry_is_booked_even_if_the_fills_lookup_fails(live):
    w, fake = live
    fake.fail_fills = True
    t = _open(w)
    assert "error" not in t and t["quantity"] > 0 and t["exchange_stop"]
    assert w.get_portfolio()["open_trades"]


def test_unprotected_position_is_flagged_and_healed(live):
    w, fake = live
    fake.fail_stop = True
    fake.fail_market = True                                             # the emergency close fails too
    t = _open(w)
    assert "warning" in t and t["unprotected"]
    assert w.get_portfolio()["open_trades"][0]["unprotected"]
    fake.fail_stop = fake.fail_market = False
    w.process_paths({"SOLUSDT": {"bars": [], "price": 100.0}})
    healed = w.get_portfolio()["open_trades"][0]
    assert healed["exchange_stop"] and not healed.get("unprotected")


def test_partly_filled_limit_is_protected_and_booked_when_cancelled(live):
    w, fake = live
    o = w.place_order("SOLUSDT", "long", 99.0, 97.0, 101.0, 103.0, None, time.time() + 3600, risk_pct=1)
    assert "error" not in o, o
    cid = o["exchange_cid"]
    fake.order_status[cid] = {"status": "PARTIALLY_FILLED", "executedQty": "3", "avgPrice": "99", "orderId": 7}
    fake.pos["SOLUSDT"] = 3
    w.process_paths({"SOLUSDT": {"bars": [], "price": 99.0}})
    assert [v["stop"] for v in fake.stops.values()] == [97.0]           # the filled part got a stop at once
    fake.order_status[cid] = {"status": "CANCELED", "executedQty": "3", "avgPrice": "99", "orderId": 7}
    w.cancel_order(o["id"])
    trades = w.get_portfolio()["open_trades"]
    assert len(trades) == 1 and trades[0]["quantity"] == 3 and trades[0]["exchange_stop"]
    assert len(fake.stops) == 1                                         # reused, not a second stop


def test_no_market_entry_while_a_limit_rests_on_the_same_coin(live):
    w, fake = live
    w.place_order("SOLUSDT", "long", 99.0, 97.0, 101.0, 103.0, None, time.time() + 3600, risk_pct=1)
    assert "pending order" in _open(w)["error"]


def test_market_confirmed_recovers_an_order_after_a_timeout(monkeypatch):
    c = BinanceFutures("k", "s")
    monkeypatch.setattr(time, "sleep", lambda s: None)
    calls = []

    def lost(*a, **k):
        calls.append("market")
        raise BinanceError(0, None, "network: read timeout")
    monkeypatch.setattr(c, "market", lost)
    monkeypatch.setattr(c, "order", lambda sym, cid: {"status": "FILLED", "executedQty": "1", "avgPrice": "100", "orderId": 5})
    assert c.market_confirmed("SOLUSDT", "BUY", 1, cid="ent-x")["executedQty"] == "1"

    def missing(sym, cid):
        raise BinanceError(400, -2013, "Order does not exist.")
    monkeypatch.setattr(c, "order", missing)
    with pytest.raises(BinanceError, match="read timeout"):
        c.market_confirmed("SOLUSDT", "BUY", 1, cid="ent-y")
    assert calls == ["market", "market"]                                  # never re-sent


def test_request_signing_matches_binance_documentation(monkeypatch):
    """The worked example from Binance's API docs (HMAC-SHA256 over the query string)."""
    import httpx
    c = BinanceFutures("key", "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j", rest="https://x")
    c._synced, c._offset_ms = True, 0
    seen = {}

    def fake_request(method, url):
        seen["url"] = url
        return httpx.Response(200, json={}, request=httpx.Request(method, url))

    monkeypatch.setattr(c._http, "request", fake_request)
    monkeypatch.setattr(time, "time", lambda: 1499827319.559)
    monkeypatch.setattr(LC, "RECV_WINDOW_MS", 5000)
    c.request("POST", "/api/v3/order", {"symbol": "LTCBTC", "side": "BUY", "type": "LIMIT", "timeInForce": "GTC",
                                        "quantity": 1, "price": 0.1})
    assert seen["url"].endswith("signature=c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71")


def test_quantities_and_prices_round_to_exchange_steps():
    fake = FakeBinance()
    assert fake.qty("SOLUSDT", 1.23456) == 1.234          # floors to the 0.001 step, never up
    assert fake.price("SOLUSDT", 100.126) == 100.13       # nearest 0.01 tick


def test_entry_owner_is_the_latest_entry_order(live):
    control.record_order("a1", "auto", "LINKUSDT", "entry", "BUY", "MARKET", 1, None, "filled")
    control.record_order("s1", "scalper", "LINKUSDT", "stop", "SELL", "STOP_MARKET", 1, None, "open")
    assert control.entry_owner("LINKUSDT") == "auto"
    assert control.entry_owner("SOLUSDT") is None


def test_live_wallet_holds_is_lock_free(live):
    w, fake = live
    _open(w)
    with w._lock:                                                  # another thread holding the wallet…
        assert w.holds("SOLUSDT") is True                          # …doesn't block the check


def test_live_entry_with_the_real_daily_loss_check_does_not_hang(live):
    """Regression: guard_entry → daily_pnl re-entered the wallet's own lock and hung forever.
    The other tests stub the daily-P&L callback, which is how this slipped through."""
    import threading
    w, fake = live
    control.register("main", daily_pnl=w.daily_pnl)
    out = {}
    th = threading.Thread(target=lambda: out.setdefault("t", _open(w)), daemon=True)
    th.start()
    th.join(timeout=5)
    assert not th.is_alive(), "open_trade deadlocked"
    assert "error" not in out["t"], out["t"]



def test_audit_keeps_order_bodies_but_not_routine_reads(tmp_path):
    import time as _t
    from live import control
    row = lambda method, status, ts=None: {"ts": ts or _t.time(), "system": "auto", "method": method, "path": "/x",
                                           "params": {}, "status": status, "response": {"big": "x" * 500}, "ms": 5}
    control.audit(row("GET", 200))
    control.audit(row("POST", 200))
    control.audit(row("GET", 400))
    got = control._db().execute("SELECT method, status, length(response) FROM audit ORDER BY ts DESC LIMIT 3").fetchall()
    assert [(m, s, n > 100) for m, s, n in reversed(got)] == [("GET", 200, False), ("POST", 200, True), ("GET", 400, True)]


def test_stops_go_straight_to_the_algo_endpoint_once_the_account_needs_it(monkeypatch):
    from live.binance import BinanceError, BinanceFutures
    c = BinanceFutures("k", "s", rest="https://example.invalid")
    monkeypatch.setattr(c, "price", lambda symbol, p: p)
    calls = []

    def request(method, path, params=None, signed=True):
        calls.append(path)
        if path == "/fapi/v1/order":
            raise BinanceError(400, -4120, "Order type not supported for this endpoint")
        return {"algoId": 1}
    monkeypatch.setattr(c, "request", request)
    assert c.stop_market("ENAUSDT", "SELL", 0.2)["algo"]
    assert c.stop_market("ENAUSDT", "SELL", 0.19)["algo"]
    assert calls == ["/fapi/v1/order", "/fapi/v1/algoOrder", "/fapi/v1/algoOrder"]   # the classic try only once
