"""Copy trading routes each leader position to the book for its market."""
import asyncio
import pytest
from experiments import books, copy_trading as ct
from trading import tape


def test_market_of_uses_the_markets_lists():
    assert ct.market_of("BTCUSDT") == "crypto"
    assert ct.market_of("TSLAUSDT") == "stocks" and ct.market_of("SKHYNIXUSDT") == "stocks"
    assert ct.market_of("XAGUSDT") == "futures" and ct.market_of("CLUSDT") == "futures"


def test_every_market_has_its_own_wallet():
    ws = ct._wallets()
    assert set(ws) == {"crypto", "stocks"}                                                   # futures retired 2026-11-03
    assert ws["crypto"].label == "copy_trading" and ws["stocks"].label == "copy_stocks" and "futures" in ct.RETIRED_MARKETS
    assert all(ct.BOOKS_BY_MARKET[m] in books.LABELS for m in ws)


# ── Variants (2026-10-30) ──────────────────────────────────────────────────────

NOW = 1_800_000_000.0


async def no_tape(now=None):
    """The tape feed as the mirror tests see it: unknown, so no gate."""
    return None
PROF = {"median_hold_h": 24.0, "win_move_pct": 2.0, "loss_move_pct": 1.0, "n": 30, "win_rate": 60.0}


def test_every_variant_of_a_market_trades_in_that_markets_one_book():
    from trading import capital
    ws = ct._all_wallets()
    assert set(ws) == {(m, v) for m in ct.MARKETS if m not in ct.RETIRED_MARKETS for v in ct.VARIANTS}
    assert ws[("crypto", "base")] is ws[("crypto", "wide")] is ws[("crypto", "late")] and ws[("crypto", "base")].label == "copy_trading"
    assert ws[("stocks", "fast")].label == "copy_stocks" and len(ct._unique_wallets()) == 2      # crypto and stocks; futures retired
    assert all(capital.PLAN.get(ct.book_name(m, v)) for m in ct.MARKETS if m not in ct.RETIRED_MARKETS for v in ct.VARIANTS)
    assert not any(b.startswith("copy_crypto_") for b in capital.PLAN)                               # the nine $611 wallets were folded 2026-11-02


def test_late_variant_takes_an_old_position_at_half_size_only_when_it_has_barely_moved():
    pos = {"side": "long", "entry": 100.0, "opened_ts": NOW - 12 * 3600}          # 12h old on a 24h hold: past ¼, within 1×
    assert not ct.judge(pos, PROF, 100.3, NOW, "base")[0]
    ok, why, _, _, size = ct.judge(pos, PROF, 100.3, NOW, "late")
    assert ok and size == ct.LATE_SIZE_X and "late entry" in why
    assert not ct.judge(pos, PROF, 100.8, NOW, "late")[0]                          # +0.8% > ¼ of their 2% win: too far gone
    assert not ct.judge({**pos, "opened_ts": NOW - 30 * 3600}, PROF, 100.1, NOW, "late")[0]   # older than their whole hold
    assert not ct.judge(pos, PROF, 98.5, NOW, "late")[0]                           # already failing beats everything
    fresh = {**pos, "opened_ts": NOW - 3600}
    assert ct.judge(fresh, PROF, 100.3, NOW, "late")[-1] == 1.0                    # a fresh one is a full-size copy under every rule
    assert ct.evaluate(fresh, PROF, 100.3, NOW)[0]


def test_each_variant_reads_its_own_leaders_from_the_pool(monkeypatch):
    from storage import state as kv
    pool = []
    for i in range(20):
        venue = "binance" if i % 2 == 0 else "hyperliquid"
        pool.append({"id": f"L{i}", "venue": venue, "score": 20 - i, "nickname": f"L{i}", "profile": {"median_hold_h": 6.0 if i >= 10 else 72.0}})
    kv.set_sync(ct._k("leaders"), {"at": NOW, "pool": pool, "leaders": pool[:10]})
    base = [L["id"] for L in ct.leaders("base")]
    assert len(base) == 2 * ct.LEADERS_N and base[0] == "L0"
    assert len(ct.leaders("wide")) == min(2 * ct.WIDE_N, 20)
    fast = ct.leaders("fast")
    assert fast and all(L["profile"]["median_hold_h"] <= ct.FAST_HOLD_H for L in fast) and len(fast) <= 2 * ct.LEADERS_N
    assert [L["id"] for L in ct.leaders("late")] == base
    assert set(ct.variants_of("L0")) == {"base", "wide", "late"} and ct.variants_of("L19") == ["wide", "fast"] or "fast" in ct.variants_of("L19")
    polled = {L["id"] for L in ct.leaders_to_poll()}
    assert polled >= set(base) | {L["id"] for L in fast}
    kv.delete_sync(ct._k("leaders"))


def test_a_single_missed_poll_does_not_close_copies_held_by_several_variants(tmp_path, monkeypatch):
    """Two variants hold the same leader position; the leader's feed omits it once. Neither copy may close on that
    one miss — the per-copy miss counters must not advance each other."""
    from trading import book_switches, monitor
    from trading.paper_trader import PaperWallet
    from storage import state as kv
    ws = {v: PaperWallet(str(tmp_path / f"{v}.db"), label=f"copy_{v}", initial_balance=10_000) for v in ("base", "wide")}
    monkeypatch.setattr(ct, "VARIANTS", {v: ct.VARIANTS[v] for v in ("base", "wide")})
    monkeypatch.setattr(ct, "ALL_BOOKS", {("crypto", "base"): "copy_trading", ("crypto", "wide"): "copy_crypto_wide"})
    monkeypatch.setitem(books.WALLETS, "copy_trading", ws["base"])
    monkeypatch.setitem(books.WALLETS, "copy_crypto_wide", ws["wide"])
    monkeypatch.setattr(books, "ENABLED", True)
    monkeypatch.setattr(book_switches, "enabled", lambda name: True)
    monkeypatch.setattr(ct, "LOG_DB", str(tmp_path / "log.db"))
    prof = {"median_hold_h": 24.0, "win_move_pct": 2.0, "loss_move_pct": 1.0}
    kv.set_sync(ct._k("leaders"), {"at": NOW, "pool": [{"id": "L9", "venue": "binance", "nickname": "nine", "profile": prof}]})
    for v in ("base", "wide"):
        ws[v].open_trade(symbol="ETHUSDT", direction="long", entry_price=100.0, stop_loss=92.0, take_profit_1=150.0, take_profit_2=160.0,
                         notional_usd=500.0, timeframe="copy", leverage=2,
                         extra={"exit_policy": "manual", "shadows": False, "variant": v, "leader_id": "L9", "leader_position_id": "p9"})
    kv.delete_sync(ct._k("missing", "p9", "base"), ct._k("missing", "p9", "wide"), ct._k("seen", "L9"))

    async def rows(L, now):
        return []                                                   # the position is missing from the feed this poll

    async def price(sym):
        return 101.0
    monkeypatch.setattr(ct, "leader_rows", rows)
    monkeypatch.setattr(monitor, "live_price", price)
    monkeypatch.setattr(tape, "btc_tape", no_tape)
    out = asyncio.run(ct.mirror(NOW))
    assert out["closed"] == [] and all(len(w.get_portfolio()["open_trades"]) == 1 for w in ws.values())
    out = asyncio.run(ct.mirror(NOW + 90))                          # missing twice in a row: now it is a close, for both
    assert sorted(c["close_reason"] for c in out["closed"]) == ["leader_closed", "leader_closed"]
    kv.delete_sync(ct._k("leaders"), ct._k("seen", "L9"))


def test_markets_traded_counts_a_leaders_positions_per_market():
    m = ct.markets_traded(["BTCUSDT", "TSLAUSDT", "XAGUSDT", "AAPLUSDT", None, "ETHUSDT"])
    assert m == {"crypto": 2, "stocks": 2, "futures": 1}
    assert ct.markets_traded([]) == {}


def test_market_of_uses_binance_asset_classes_when_known(monkeypatch):
    monkeypatch.setitem(ct._perp_cache, "market", {"SNDKUSDT": "stocks", "XPTUSDT": "futures", "BTCUSDT": "crypto"})
    assert ct.market_of("SNDKUSDT") == "stocks" and ct.market_of("XPTUSDT") == "futures" and ct.market_of("BTCUSDT") == "crypto"
    assert ct.market_of("TSLAUSDT") == "stocks"                   # not in the fetched map: the Markets lists still answer


def test_hyperliquid_builder_markets_map_to_binance_tradfi_perps():
    from data import hyperliquid_client as hl
    perps = {"TSLAUSDT", "XAUUSDT", "XAGUSDT", "CLUSDT", "SPYUSDT", "BTCUSDT", "SKHYNIXUSDT"}
    assert hl.binance_symbol("xyz:TSLA", perps) == "TSLAUSDT" and hl.binance_symbol("xyz:GOLD", perps) == "XAUUSDT"
    assert hl.binance_symbol("xyz:SILVER", perps) == "XAGUSDT" and hl.binance_symbol("xyz:CL", perps) == "CLUSDT"
    assert hl.binance_symbol("xyz:SKHX", perps) == "SKHYNIXUSDT"
    assert hl.binance_symbol("xyz:SP500", perps) is None and hl.binance_symbol("xyz:XYZ100", perps | {"QQQUSDT"}) is None   # index-level perps: no ETF-priced twin
    assert hl.binance_symbol("xyz:VIX", perps) is None and hl.binance_symbol("abc:TSLA", perps) is None
    assert hl.binance_symbol("BTC", perps) == "BTCUSDT"


def test_route_sends_a_position_to_one_book_by_record_then_by_order(tmp_path):
    from trading.paper_trader import PaperWallet
    ws = {("crypto", v): PaperWallet(str(tmp_path / f"{v}.db"), label=f"copy_{v}", initial_balance=10_000) for v in ct.VARIANTS}
    assert ct.route("crypto", ["late"], ws) == ("late", "only rule that takes it")
    v, why = ct.route("crypto", ["wide", "base", "fast"], ws)
    assert v == "base" and "plainer" in why                                   # nobody has a record yet
    # Give the wide book a record of ten winners: it now takes the trades the other rules would also take.
    w = ws[("crypto", "wide")]
    for i in range(10):
        t = w.open_trade(symbol="ETHUSDT", direction="long", entry_price=100.0, stop_loss=92.0, take_profit_1=150.0, take_profit_2=160.0,
                         notional_usd=200.0, timeframe="copy", leverage=2, extra={"exit_policy": "manual", "shadows": False, "variant": "wide"})
        w.close_trade_manually(t["id"], 104.0, "leader_closed")
    v, why = ct.route("crypto", ["wide", "base", "fast"], ws)
    assert v == "wide" and "best record" in why
    n, avg = ct.record_r(w, "wide")
    assert n == 10 and avg > 0 and ct.record_r(w, "base") == (0, None)                               # the record is per rule, by the copies' tag


def test_mirror_copies_a_position_into_exactly_one_book(tmp_path, monkeypatch):
    from trading import book_switches, monitor
    from trading.paper_trader import PaperWallet
    from storage import state as kv
    ws = {v: PaperWallet(str(tmp_path / f"{v}.db"), label=f"copy_{v}", initial_balance=10_000) for v in ("base", "wide", "late")}
    monkeypatch.setattr(ct, "VARIANTS", {v: ct.VARIANTS[v] for v in ("base", "wide", "late")})
    monkeypatch.setattr(ct, "ALL_BOOKS", {("crypto", "base"): "copy_trading", ("crypto", "wide"): "copy_crypto_wide", ("crypto", "late"): "copy_crypto_late"})
    monkeypatch.setitem(books.WALLETS, "copy_trading", ws["base"])
    monkeypatch.setitem(books.WALLETS, "copy_crypto_wide", ws["wide"])
    monkeypatch.setitem(books.WALLETS, "copy_crypto_late", ws["late"])
    monkeypatch.setattr(books, "ENABLED", True)
    monkeypatch.setattr(book_switches, "enabled", lambda name: True)
    monkeypatch.setattr(ct, "LOG_DB", str(tmp_path / "log.db"))
    prof = {"median_hold_h": 24.0, "win_move_pct": 2.0, "loss_move_pct": 1.0}
    kv.set_sync(ct._k("leaders"), {"at": NOW, "pool": [{"id": "L7", "venue": "binance", "nickname": "seven", "profile": prof}]})
    kv.delete_sync(ct._k("seen", "L7"), ct._k("skipped"))
    row = {"position_id": "p7", "symbol": "ETHUSDT", "side": "long", "opened_ts": NOW - 600, "closed_ts": None, "entry": 3000.0, "close_price": None,
           "leverage": 5.0, "remaining": 1.0, "roi": 0.0, "status": "Open"}

    async def rows(L, now):
        return [row]

    async def price(sym):
        return 3003.0
    monkeypatch.setattr(ct, "leader_rows", rows)
    monkeypatch.setattr(monitor, "live_price", price)
    monkeypatch.setattr(tape, "btc_tape", no_tape)                                      # the wide book's tape gate stays out of this
    out = asyncio.run(ct.mirror(NOW))
    assert len(out["opened"]) == 1 and out["opened"][0]["variant"] == "base"            # fresh position: all three would take it; base is the plainer rule
    assert sorted(out["opened"][0]["routed_from"]) == ["base", "late", "wide"]
    assert sum(len(w.get_portfolio()["open_trades"]) for w in ws.values()) == 1
    routed = [e for e in ct.skipped() if e.get("routed")]
    assert sorted(e["variant"] for e in routed) == ["late", "wide"] and "routed to" in routed[0]["reason"]
    kv.delete_sync(ct._k("leaders"), ct._k("seen", "L7"), ct._k("skipped"))


# ── Exits and sizing re-ruled 2026-11-02 ──────────────────────────────────────

def test_stop_sits_on_the_leaders_typical_loss_and_the_leg_is_sized_to_it():
    assert ct.stop_pct({"loss_move_pct": 1.5}) == 3.0                                  # 2× their median loss
    assert ct.stop_pct({"loss_move_pct": 0.2}) == ct.STOP_PCT_MIN == 2.0               # never tighter than 2%: 1% was intraday noise (2026-11-04)
    assert ct.stop_pct({"loss_move_pct": 5.0}) == ct.DISASTER_PCT == ct.stop_pct(None)  # never wider than the disaster stop; no profile → disaster
    assert ct.leg_notional(20_000, 2.0) == 5_000                                       # 0.5% risk at a 2% stop: a quarter of the book, which is the cap
    assert ct.leg_notional(20_000, 1.0) == 20_000 * ct.MAX_LEG_PCT / 100 == 5_000      # a tighter stop would ask for half the book: capped at a quarter
    assert ct.leg_notional(20_000, 4.0) == 2_500 and ct.leg_notional(500, 4.0) == 62.5    # the disaster stop: an eighth
    assert ct.leg_notional(400, 4.0) == 50                                              # floor: a tenth of the book
    assert ct.leg_notional(20_000, 2.0, "stocks") == 5_000 == ct.leg_notional(20_000, 2.0, "crypto")   # a stock perp: the same quarter
    assert ct.CONSENSUS_X == 1.0                                                           # consensus is recorded, never sized


def test_a_book_stops_opening_when_full_over_the_gross_cap_or_down_three_percent_today(tmp_path):
    from trading.paper_trader import PaperWallet
    import time
    now = time.time()                            # the closes below are stamped with the real clock: "today" must be their day
    w = PaperWallet(str(tmp_path / "w.db"), label="copy_test", initial_balance=10_000)
    assert ct.book_room(w, now) is None
    for i in range(3):                                                                   # three legs of half the book each: 150% on
        w.open_trade(f"C{i}USDT", "long", 100, 98, 103, 105, notional_usd=5_000, timeframe="copy", leverage=5, extra={"strategy": "copy_test", "exit_policy": "manual"})
    assert ct.book_room(w, now).startswith("gross cap")
    w2 = PaperWallet(str(tmp_path / "w2.db"), label="copy_test2", initial_balance=10_000)
    t = w2.open_trade("LOSSUSDT", "long", 100, 96, 103, 105, notional_usd=2_500, timeframe="copy", leverage=5, extra={"strategy": "copy_test2", "exit_policy": "manual"})
    w2.close_trade_manually(t["id"], 87.0, "stop_loss")                                  # −13% on a quarter of the book: −3.25% today
    assert ct.day_loss(w2.get_portfolio(), now) == pytest.approx(325 + t["fees_usd"], abs=20)
    assert ct.book_room(w2, now).startswith("halted for the day")
    assert ct.book_room(w2, now + 86400) is None                                         # tomorrow is a new day
    w3 = PaperWallet(str(tmp_path / "w3.db"), label="copy_test3", initial_balance=10_000)
    for i in range(ct.MAX_OPEN):
        w3.open_trade(f"F{i}USDT", "long", 100, 98, 103, 105, notional_usd=100, timeframe="copy", leverage=1, extra={"strategy": "copy_test3", "exit_policy": "manual"})
    assert ct.book_room(w3, now) == "book full"


def test_a_copy_that_cannot_pay_its_fees_is_skipped():
    pos = {"side": "long", "entry": 100.0, "opened_ts": NOW - 600}
    ok, why, *_ = ct.judge(pos, {**PROF, "win_move_pct": 0.3}, 100.0, NOW)            # a 0.3% median win vs a 0.16% round trip × 3
    assert not ok and why.startswith("not worth the fees")
    ok, why, *_ = ct.judge(pos, PROF, 100.0, NOW)                                      # 2% is plenty
    assert ok
    ok, why, *_ = ct.judge(pos, {**PROF, "win_move_pct": 0.6}, 100.3, NOW)            # 0.6% win, +0.3% gone already: 0.3% left is not enough
    assert not ok and "not worth the fees" in why


def test_wide_variant_skips_legs_that_fight_the_tape(tmp_path, monkeypatch):
    from trading import book_switches, monitor, tape
    from trading.paper_trader import PaperWallet
    from storage import state as kv
    ws = {v: PaperWallet(str(tmp_path / f"{v}.db"), label=f"copy_{v}", initial_balance=10_000) for v in ("base", "wide")}
    monkeypatch.setattr(ct, "VARIANTS", {v: ct.VARIANTS[v] for v in ("base", "wide")})
    monkeypatch.setattr(ct, "ALL_BOOKS", {("crypto", "base"): "copy_trading", ("crypto", "wide"): "copy_crypto_wide"})
    monkeypatch.setitem(books.WALLETS, "copy_trading", ws["base"])
    monkeypatch.setitem(books.WALLETS, "copy_crypto_wide", ws["wide"])
    monkeypatch.setattr(books, "ENABLED", True)
    monkeypatch.setattr(book_switches, "enabled", lambda name: True)
    monkeypatch.setattr(ct, "LOG_DB", str(tmp_path / "log.db"))
    kv.set_sync(ct._k("leaders"), {"at": NOW, "pool": [{"id": "L8", "venue": "binance", "nickname": "eight", "profile": PROF}]})
    kv.delete_sync(ct._k("seen", "L8"), ct._k("skipped"))
    assert "base" in ct.variants_of("L8") and "wide" in ct.variants_of("L8")

    async def rows(L, now):
        return [{"position_id": "p8", "symbol": "NEARUSDT", "side": "long", "opened_ts": NOW - 600, "closed_ts": None, "entry": 4.0, "close_price": None,
                 "leverage": 5.0, "remaining": 1.0, "roi": 0.0, "status": "Open"}]

    async def price(sym):
        return 4.0

    async def red_tape(now=None):
        return {"price": 77_000.0, "vwap": 78_000.0, "pct": -1.282, "tilt": "down"}
    monkeypatch.setattr(ct, "leader_rows", rows)
    monkeypatch.setattr(monitor, "live_price", price)
    monkeypatch.setattr(tape, "btc_tape", red_tape)
    # routing would normally hand a fresh position to the base book anyway; make base full so only wide could take it
    monkeypatch.setattr(ct, "MAX_OPEN", 0)
    out = asyncio.run(ct.mirror(NOW))
    assert out["opened"] == [] and ws["wide"].get_portfolio()["open_trades"] == []
    reasons = {s["variant"]: s["reason"] for s in ct.skipped() if s.get("position_id") == "p8"}
    assert reasons["wide"].startswith("tape: BTC -1.28% below its 24h VWAP") and reasons["base"] == "book full"
    kv.delete_sync(ct._k("leaders"), ct._k("seen", "L8"), ct._k("skipped"))


def test_against_tape():
    from trading import tape
    assert tape.against_tape("long", {"tilt": "down", "pct": -0.5}).startswith("tape:")
    assert tape.against_tape("short", {"tilt": "down", "pct": -0.5}) is None
    assert tape.against_tape("short", {"tilt": "up", "pct": 0.5}).startswith("tape:")
    assert tape.against_tape("long", None) is None                                  # feed down: no gate


def test_btc_tape_is_a_volume_weighted_average_of_the_day(monkeypatch):
    from trading import tape
    from data import binance_client

    async def klines(symbol, interval="4h", limit=100):
        assert symbol == "BTCUSDT" and interval == "5m" and limit == 288
        return [{"high": 101.0, "low": 99.0, "close": 100.0, "volume": 3.0}, {"high": 111.0, "low": 109.0, "close": 110.0, "volume": 1.0}]
    monkeypatch.setattr(binance_client, "fetch_futures_klines", klines)
    tape._cache.update(at=0.0, data=None)
    t = asyncio.run(tape.btc_tape(1.0))
    assert t["vwap"] == 102.5 and t["price"] == 110.0 and t["tilt"] == "up" and t["pct"] == pytest.approx(7.317, abs=0.01)
    assert asyncio.run(tape.btc_tape(2.0)) is t                                     # cached
    tape._cache.update(at=0.0, data=None)


def test_an_empty_venue_selection_keeps_that_venues_previous_leaders():
    prev = [{"id": "b1", "venue": "binance", "score": 1.0}, {"id": "h1", "venue": "hyperliquid", "score": 2.0}, {"id": "l1", "venue": "lighter", "score": 0.5}]
    fresh = {"binance": [{"id": "b2", "venue": "binance", "score": 1.5}], "hyperliquid": [], "lighter": []}
    out = ct._merge_with_previous(fresh, prev, 1_000.0)
    assert [L["id"] for L in out] == ["b2", "h1", "l1"] and out[1]["stale_since"] == 1_000.0 and "stale_since" not in out[0]
    assert ct._merge_with_previous({"binance": [], "hyperliquid": [], "lighter": []}, prev, 2_000.0) == [{**L, "stale_since": 2_000.0} for L in prev]
    assert ct._merge_with_previous({"binance": [], "hyperliquid": [], "lighter": []}, [], 2_000.0) == []      # nothing before either


def test_coin_tape_and_the_mid_dump_gate(monkeypatch):
    from trading import tape
    from data import binance_client

    async def klines(symbol, interval="4h", limit=100):
        assert symbol == "TRUMPUSDT" and interval == "5m" and limit == 288
        quiet = [{"open": 1.39, "high": 1.4, "low": 1.38, "close": 1.39, "volume": 1.0} for _ in range(276)]
        dump = [{"open": 1.39, "high": 1.39, "low": 1.25, "close": 1.39 - 0.0125 * (i + 1), "volume": 6.0} for i in range(12)]   # −10.8% in the hour, 6× volume
        return quiet + dump
    monkeypatch.setattr(binance_client, "fetch_futures_klines", klines)
    tape._coin_cache.clear()
    t = asyncio.run(tape.coin_tape("TRUMPUSDT", 1.0))
    assert t["move_pct"] == pytest.approx(-10.79, abs=0.05) and t["vol_x"] == pytest.approx(4.97, abs=0.05)
    assert tape.against_move("long", t).startswith("mid-dump")                     # buying into the dump: deferred
    assert tape.against_move("short", t) is None                                     # selling it is not gated here
    assert tape.against_move("short", {"move_pct": 4.0, "vol_x": 3.0}).startswith("mid-squeeze")
    assert tape.against_move("long", {"move_pct": -4.0, "vol_x": 1.2}) is None       # a drift on normal volume is not a dump
    assert tape.against_move("long", {"move_pct": -2.0, "vol_x": 5.0}) is None       # a small move, however busy
    assert tape.against_move("long", None) is None                                   # feed down: no gate
    tape._coin_cache.clear()


def test_leaders_and_venues_are_sized_by_their_record_with_us(tmp_path, monkeypatch):
    """2026-11-04: one Hyperliquid wallet cost −$1,789 over 7 copies and the venue −$2,704 over 13 while Lighter's 43
    copies lost a fifth of that — a leader at ≤ 0R over 5 copies sits out a week, a venue at ≤ 0R over 10 copies
    is copied at measuring size."""
    from trading.paper_trader import PaperWallet
    w = PaperWallet(str(tmp_path / "rec.db"), label="copy_trading", initial_balance=20_000)
    monkeypatch.setattr(ct, "_unique_wallets", lambda: [w])
    now = 1_793_900_000.0

    def copy(leader, venue, pnl, i):
        t = w.open_trade(f"C{i}USDT", "long", 100, 98, 103, 105, notional_usd=1_000, timeframe="copy", leverage=3,
                         extra={"strategy": "copy_trading", "exit_policy": "manual", "leader_id": leader, "venue": venue, "leader": leader})
        w.close_trade_manually(t["id"], 100 + pnl / 10, "leader_closed")                  # $1,000 notional: $1 a 0.1%
    for i in range(5):
        copy("bad", "hyperliquid", -8, i)                                                  # five copies, every one a loser
    for i in range(10, 13):
        copy("good", "lighter", 6, i)
    rec = ct.records()
    assert rec["leaders"]["bad"]["n"] == 5 and rec["leaders"]["bad"]["avg_r"] < 0
    assert rec["leaders"]["good"]["avg_r"] > 0
    assert ct.leader_probation(rec, "bad", rec["leaders"]["bad"]["last_closed"] + 3600).startswith("leader on probation")
    assert ct.leader_probation(rec, "bad", rec["leaders"]["bad"]["last_closed"] + 8 * 86400) is None     # a week later: one more chance
    assert ct.leader_probation(rec, "good", now) is None and ct.leader_probation(rec, "unknown", now) is None
    assert ct.venue_multiplier(rec, "hyperliquid") == (1.0, "hyperliquid: " + f"{rec['venues']['hyperliquid']['avg_r']:+.2f}R over 5 copies")   # under 10 copies: full size
    for i in range(20, 26):
        copy("other", "hyperliquid", -3, i)                                                # eleven losing copies on the venue
    rec = ct.records()
    x, why = ct.venue_multiplier(rec, "hyperliquid")
    assert x == ct.VENUE_MEASURE_X and why.startswith("hyperliquid at measuring size")
    assert ct.venue_multiplier(rec, "lighter")[0] == 1.0 and ct.venue_multiplier(rec, "binance") == (1.0, "binance: no record yet")
