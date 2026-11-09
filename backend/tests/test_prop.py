"""The prop book: Propr's rulebook as code, the book acting at our floors before the firm's, the API adapter's orders."""
import asyncio
import json
import time

import httpx
import pytest

from experiments import books, prop, prop_exec, prop_rules as R
from storage import state as kv
from trading import book_switches, monitor
from trading.paper_trader import PaperWallet

UNIVERSE = {"BTC": {"szDecimals": 5, "maxLeverage": 40}, "ETH": {"szDecimals": 4, "maxLeverage": 25}, "ZEC": {"szDecimals": 2, "maxLeverage": 10},
            "kPEPE": {"szDecimals": 0, "maxLeverage": 10}, "xyz:NVDA": {"szDecimals": 2, "maxLeverage": 10}, "xyz:XYZ100": {"szDecimals": 2, "maxLeverage": 10}}


# ── The rulebook ──────────────────────────────────────────────────────────────

def test_symbols_resolve_to_hyperliquid_coins_or_nothing():
    assert R.resolve("BTCUSDT", UNIVERSE) == "BTC" and R.resolve("1000PEPEUSDT", UNIVERSE) == "kPEPE"
    assert R.resolve("NVDAUSDT", UNIVERSE) == "xyz:NVDA" and R.resolve("QQQUSDT", UNIVERSE) == "xyz:XYZ100"
    assert R.resolve("CAPUSDT", UNIVERSE) is None
    assert R.leverage_cap("BTC") == 5 and R.leverage_cap("ZEC") == 2 and R.leverage_cap("xyz:NVDA") == 2


def test_the_floors_and_our_actions_sit_where_the_rulebook_says():
    a = R.assess(25_000, 25_000, 25_000)
    assert a["action"] == "ok" and a["breach"] is None and a["floors"]["daily"] == 24_250 and a["floors"]["max"] == 23_500
    assert R.assess(24_620, 25_000, 25_000)["action"] == "halt"                    # 1.52% today
    assert R.assess(24_430, 25_000, 25_000)["action"] == "flatten"                 # 2.28% today
    assert R.assess(24_250, 25_000, 25_000)["breach"] == "max_daily_loss"
    a = R.assess(23_900, 25_000, 24_100)                                            # a losing week: 4.4% down, 0.8% today → the drawdown halts
    assert a["action"] == "halt" and a["breach"] is None and a["dd_used_pct"] == pytest.approx(73.3, abs=0.1)
    assert R.assess(23_700, 25_000, 24_100)["action"] == "flatten" and R.assess(23_500, 25_000, 24_100)["breach"] == "max_drawdown"
    assert R.passed(27_500, 25_000) and not R.passed(27_499, 25_000) and R.target_progress_pct(26_250, 25_000) == 50.0


def test_sizing_risks_the_registered_fraction_and_respects_the_leverage_cap():
    notional, lev = R.size(25_000, 100.0, 90.0, "reversal", 2)                       # 0.4% at a 10% stop → $1,000
    assert notional == 1_000.0 and lev == 2
    notional, lev = R.size(25_000, 100.0, 99.8, "auction", 2)                        # 1% at a 0.2% stop would be $125k: capped at cap/2 of equity
    assert notional == 25_000.0 and lev == 2
    assert R.quantity(1_000.0, 123.456, 2) == "8.10" and R.quantity(100.0, 0.00123, 0) == "81300"


# ── The book ──────────────────────────────────────────────────────────────────

@pytest.fixture
def book(tmp_path, monkeypatch):
    w = PaperWallet(str(tmp_path / "prop.db"), label="prop", initial_balance=25_000)
    monkeypatch.setitem(books.WALLETS, "prop", w)
    monkeypatch.setattr(books, "ENABLED", True)
    monkeypatch.setattr(book_switches, "enabled", lambda name: True)
    monkeypatch.setattr(prop_exec, "MODE", "off")
    kv.delete_sync(prop.STATE_KEY, prop.LOG_KEY, prop.UNIVERSE_KEY)
    prices = {"ZECUSDT": 100.0, "BTCUSDT": 50_000.0, "CAPUSDT": 1.0, "NVDAUSDT": 200.0}

    async def price(sym):
        return prices.get(sym)

    async def uni(now=None):
        return UNIVERSE
    monkeypatch.setattr(monitor, "live_price", price)
    monkeypatch.setattr(prop, "universe", uni)
    return w, prices


def test_reversal_picks_open_under_the_rules_and_coins_the_firm_lacks_are_refused(book):
    w, _ = book
    now = time.time()
    opened = asyncio.run(prop.mirror_reversal([("ZECUSDT", "short"), ("CAPUSDT", "short")], {"ZECUSDT": -0.3, "CAPUSDT": -0.4}, now))
    assert [t["symbol"] for t in opened] == ["ZECUSDT"]
    t = opened[0]
    assert t["source"] == "reversal" and t["hl_coin"] == "ZEC" and t["leverage"] == 2 and t["stop_loss"] == pytest.approx(110.0)
    assert t["notional_usd"] == pytest.approx(1_000.0, rel=0.02)                   # 0.4% of $25k at a 10% stop
    log = kv.get_sync(prop.LOG_KEY)
    assert any(e["event"] == "refused" and e["symbol"] == "CAPUSDT" and "Hyperliquid" in e["why"] for e in log)
    st = prop.status()
    assert st["phase"] == "evaluation" and st["by_source"]["reversal"]["open"] == 1 and st["open"][0]["coin"] == "ZEC"


def test_the_books_own_caps_hold_before_the_firms(book):
    w, prices = book
    now = time.time()
    syms = ["ZECUSDT", "BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT"]
    for s in syms:
        prices[s] = 100.0
    uni = {**UNIVERSE, "SOL": {"szDecimals": 2}, "DOGE": {"szDecimals": 0}}

    async def u(now=None):
        return uni
    prop.universe = u
    opened = asyncio.run(prop.mirror_reversal([(s, "short") for s in syms], {}, now))
    assert len(opened) == R.MAX_OPEN_BY_SOURCE["reversal"] == 4
    refused = [e for e in kv.get_sync(prop.LOG_KEY) if e["event"] == "refused"]
    assert refused and "our cap 4" in refused[-1]["why"]


def test_a_losing_day_halts_then_flattens_and_the_firms_breach_is_recorded(book):
    w, prices = book
    now = time.time()
    coins = ["ZECUSDT", "BTCUSDT", "ETHUSDT", "SOLUSDT"]
    for c in coins:
        prices[c] = 100.0
    prices["QQQUSDT"] = 200.0
    uni = {**UNIVERSE, "SOL": {"szDecimals": 2}}

    async def u(now=None):
        return uni
    prop.universe = u
    # four reversal shorts ($1,000 each, stop +10%) and two auction longs (1% at a 2% stop → ~$12.5k each, stop −2%)
    assert len(asyncio.run(prop.mirror_reversal([(c, "short") for c in coins], {}, now))) == 4
    for sym in ("NVDAUSDT", "QQQUSDT"):
        t = asyncio.run(prop.mirror_auction(sym, "long", 200.0, 196.0, 210.0, now + 7200, "auction test", {"level_name": "ONH"}, now + 1))
        assert t and t["notional_usd"] == pytest.approx(12_400.0, rel=0.03)
    asyncio.run(prop.tick(now + 2))
    # the shorts go 8% against us: −$320 → 1.3%, nothing happens
    for c in coins:
        prices[c] = 108.0
    w.update_prices({c: 108.0 for c in coins})
    out = asyncio.run(prop.tick(now + 60))
    assert out["assess"]["action"] == "ok" and out["acted"] is None
    # the stock legs slip 0.5%: −$125 more → ~1.8%: halt, and nothing new opens
    prices["NVDAUSDT"] = prices["QQQUSDT"] = 199.0
    w.update_prices({"NVDAUSDT": 199.0, "QQQUSDT": 199.0})
    out = asyncio.run(prop.tick(now + 120))
    assert out["assess"]["action"] == "halt" and prop.status()["halted_day"] == R.day_key(now + 120)
    prices["DOGEUSDT"] = 1.0
    uni["DOGE"] = {"szDecimals": 0}
    assert asyncio.run(prop.mirror_reversal([("DOGEUSDT", "short")], {}, now + 121)) == []
    # the stock legs slip 1.5% (their stops sit at −2%): −$375 → ~2.8%: everything is flattened by our rule
    prices["NVDAUSDT"] = prices["QQQUSDT"] = 197.0
    w.update_prices({"NVDAUSDT": 197.0, "QQQUSDT": 197.0})
    out = asyncio.run(prop.tick(now + 180))
    assert out["acted"] and len(out["acted"]["flattened"]) == 6 and w.get_portfolio()["open_trades"] == []
    st = prop.status()
    assert st["flattens"] == 1 and st["halts"] == 1 and st["breach"] is None and st["phase"] == "evaluation"
    assert st["assess"]["daily_loss_pct"] > R.FLATTEN_DAILY_PCT and st["assess"]["daily_loss_pct"] < R.PLAN["daily_loss_pct"]
    # had the book not acted, equity at the firm's daily floor would have been a breach — recorded, not acted on again
    real = prop.ledger
    prop.ledger = lambda w_, st_: {**real(w_, st_), "equity": 24_250.0}
    try:
        asyncio.run(prop.tick(now + 240))
    finally:
        prop.ledger = real
    assert prop.status()["breach"]["rule"] == "max_daily_loss"


def test_the_day_rolls_at_midnight_utc_and_the_target_moves_the_phase(book):
    w, prices = book
    day1 = 1_794_000_000.0                                                          # 2026-11-06 ~14:00 UTC
    asyncio.run(prop.tick(day1))
    asyncio.run(prop.mirror_reversal([("ZECUSDT", "short")], {}, day1))
    prices["ZECUSDT"] = 90.0
    w.close_trade_manually(w.get_portfolio()["open_trades"][0]["id"], 90.0, "test")   # +$100
    asyncio.run(prop.tick(day1 + 60))
    st = prop._state()
    assert st["sod_balance"] == 25_000.0                                            # today's start-of-day is unchanged by the day's profit
    asyncio.run(prop.tick(day1 + 86_400))
    assert prop._state()["sod_balance"] == pytest.approx(25_100.0, abs=2)          # the next day starts from the realised balance
    big = prop.ledger
    prop.ledger = lambda w_, st_: {**big(w_, st_), "balance": 27_500.0, "equity": 27_500.0}
    try:
        asyncio.run(prop.tick(day1 + 86_460))
    finally:
        prop.ledger = big
    assert prop._state()["phase"] == "funded" and prop._state()["passed_at"]


def test_reset_starts_a_new_attempt_and_keeps_the_record(book):
    w, _ = book
    now = time.time()
    asyncio.run(prop.mirror_reversal([("ZECUSDT", "short")], {}, now))
    st = asyncio.run(prop.reset("the user bought another challenge"))
    assert st["attempt"] == 2 and st["open"] == [] and st["attempts"][0]["attempt"] == 1 and st["phase"] == "evaluation"
    assert st["closed_n"] == 0                                                      # the new attempt's ledger starts clean


# ── The firm's API ─────────────────────────────────────────────────────────────

def test_the_adapter_sends_the_documented_orders():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, req.url.path, req.headers.get("x-api-key"), json.loads(req.content) if req.content else None))
        if req.url.path.endswith("/challenge-attempts"):
            return httpx.Response(200, json={"data": [{"accountId": "urn:prp-account:1", "status": "active"}]})
        if "/margin-config/" in req.url.path and req.method == "GET":
            return httpx.Response(200, json={"configId": "cfg1", "asset": "ZEC"})
        if req.method == "PUT":
            return httpx.Response(200, json={"configId": "cfg1", "leverage": 2})
        if req.url.path.endswith("/orders"):
            return httpx.Response(201, json={"data": [{"orderId": "o1"}, {"orderId": "o2"}]})
        return httpx.Response(404, json={"code": 13450, "message": "not found"})

    c = prop_exec.Client(base_url="https://api.beta.propr.xyz/v1", api_key="pk_live_test", transport=httpx.MockTransport(handler))

    async def run():
        acct = await c.account_id()
        await c.set_leverage(acct, "ZEC", 2)
        orders = await c.open(acct, "ZEC", "short", "10.00", 110.0)
        await c.close(acct, "ZEC", "short", "10.00")
        return acct, orders
    acct, orders = asyncio.run(run())
    assert acct == "urn:prp-account:1" and [o["orderId"] for o in orders] == ["o1", "o2"]
    assert all(k == "pk_live_test" for _, _, k, _ in seen)
    put = next(b for m, p, _, b in seen if m == "PUT")
    assert put == {"exchange": "hyperliquid", "asset": "ZEC", "marginMode": "cross", "leverage": 2}
    entry_body = [b for m, p, _, b in seen if m == "POST" and p.endswith("/orders")][0]
    entry, stop = entry_body["orders"]
    assert entry_body["orderGroupId"] and len(entry_body["orderGroupId"]) == 26
    assert entry["type"] == "market" and entry["side"] == "sell" and entry["positionSide"] == "short" and entry["asset"] == "ZEC" and entry["quote"] == "USDC"
    assert entry["quantity"] == "10.00" and entry["timeInForce"] == "IOC" and len(entry["intentId"]) == 26
    assert stop["type"] == "stop_market" and stop["side"] == "buy" and stop["reduceOnly"] is True and stop["triggerPrice"] == "110"
    close = [b for m, p, _, b in seen if m == "POST" and p.endswith("/orders")][1]["orders"][0]
    assert close["side"] == "buy" and close["reduceOnly"] is True and close["closePosition"] is True


def test_api_errors_carry_the_firms_code():
    def handler(req):
        return httpx.Response(400, json={"code": 13056, "message": "CONDITIONAL_ORDER_REQUIRES_POSITION_OR_GROUP"})
    c = prop_exec.Client(base_url="https://x", api_key="k", transport=httpx.MockTransport(handler))
    with pytest.raises(prop_exec.ProprError) as e:
        asyncio.run(c.health())
    assert e.value.code == 13056 and e.value.status == 400
    assert prop_exec.describe()["enabled"] is False
