"""Copy trading routes each leader position to the book for its market."""
import asyncio
from experiments import books, copy_trading as ct


def test_market_of_uses_the_markets_lists():
    assert ct.market_of("BTCUSDT") == "crypto"
    assert ct.market_of("TSLAUSDT") == "stocks" and ct.market_of("SKHYNIXUSDT") == "stocks"
    assert ct.market_of("XAGUSDT") == "futures" and ct.market_of("CLUSDT") == "futures"


def test_every_market_has_its_own_wallet():
    ws = ct._wallets()
    assert set(ws) == {"crypto", "stocks", "futures"}
    assert ws["crypto"].label == "copy_trading" and ws["stocks"].label == "copy_stocks" and ws["futures"].label == "copy_futures"
    assert all(ct.BOOKS_BY_MARKET[m] in books.LABELS for m in ws)


# ── Variants (2026-10-30) ──────────────────────────────────────────────────────

NOW = 1_800_000_000.0
PROF = {"median_hold_h": 24.0, "win_move_pct": 2.0, "loss_move_pct": 1.0, "n": 30, "win_rate": 60.0}


def test_every_market_has_a_wallet_per_variant_and_the_plan_funds_them():
    from trading import capital
    ws = ct._all_wallets()
    assert set(ws) == {(m, v) for m in ct.MARKETS for v in ct.VARIANTS}
    assert ws[("crypto", "base")].label == "copy_trading" and ws[("stocks", "wide")].label == "copy_stocks_wide"
    assert all(name in books.LABELS for name in ct.ALL_BOOKS.values())
    assert all(capital.PLAN.get(ct.book_name(m, v)) == round(5_500 / 9, 2) for m in ct.MARKETS for v in ct.VARIANTS if v != "base")


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
    assert hl.binance_symbol("xyz:SP500", perps) == "SPYUSDT" and hl.binance_symbol("xyz:SKHX", perps) == "SKHYNIXUSDT"
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
                         notional_usd=200.0, timeframe="copy", leverage=2, extra={"exit_policy": "manual", "shadows": False})
        w.close_trade_manually(t["id"], 104.0, "leader_closed")
    v, why = ct.route("crypto", ["wide", "base", "fast"], ws)
    assert v == "wide" and "best record" in why
    n, avg = ct.record_r(w)
    assert n == 10 and avg > 0


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
    out = asyncio.run(ct.mirror(NOW))
    assert len(out["opened"]) == 1 and out["opened"][0]["variant"] == "base"            # fresh position: all three would take it; base is the plainer rule
    assert sorted(out["opened"][0]["routed_from"]) == ["base", "late", "wide"]
    assert sum(len(w.get_portfolio()["open_trades"]) for w in ws.values()) == 1
    routed = [e for e in ct.skipped() if e.get("routed")]
    assert sorted(e["variant"] for e in routed) == ["late", "wide"] and "routed to" in routed[0]["reason"]
    kv.delete_sync(ct._k("leaders"), ct._k("seen", "L7"), ct._k("skipped"))
