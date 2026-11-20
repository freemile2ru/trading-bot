"""The tenant engine host behind its flag (2026-11-20): kv scoping at access, the wallet proxies, who runs, and that
a tenant's run of an engine lands in the tenant's state and wallet, never the owner's."""
import asyncio

import pytest

from storage import state as kv
from tenancy import accounts, context, engines, runner, tiers, wallets


@pytest.fixture(autouse=True)
def _accounts(tmp_path, monkeypatch):
    accounts.use(str(tmp_path / "accounts.db"))
    monkeypatch.setattr(wallets, "TENANTS_DIR", str(tmp_path / "tenants"))
    monkeypatch.setattr(accounts, "OWNER_EMAILS", set())
    monkeypatch.setenv("APP_ENV", "test")
    wallets.forget()
    runner._queue.clear()
    runner._state["mirrored_24h"].clear(); runner._state["errors"].clear()
    engines._state.update({"last_tick": None, "ticks": 0, "runs": 0, "errors": [], "per_tenant": {}})
    yield
    wallets.forget()
    accounts.use(str(tmp_path / "gone.db"))


def _elite_own(monkeypatch, email="own@example.com", books=("auto", "composite")):
    monkeypatch.setattr(engines, "FLAG", True)
    accounts.sign_in("admin@example.com")
    u = accounts.sign_in(email)
    tid = u["tenant_id"]
    accounts.set_tier(tid, "elite")
    accounts.update_settings(tid, books={**{b: False for b in tiers.DEFAULT_BOOKS_ON}, **{b: True for b in books}}, engine_mode="own")
    return tid


def test_the_kv_store_scopes_every_key_to_the_tenant_at_access_and_shares_market_reads():
    from autotrader.engine import PAUSED_KEY                                  # an import-time constant
    kv.set_sync(kv.key("books", "auto"), {"at": 1})
    kv.set_sync("state:scan:snapshot", {"owner": True})
    with context.as_tenant("t1"):
        assert kv.get_sync(kv.key("books", "auto")) is None                  # the owner's switch is not theirs
        kv.set_sync(kv.key("books", "auto"), {"at": 2})
        kv.set_sync(PAUSED_KEY, True)
        assert kv.get_sync("state:scan:snapshot") == {"owner": True}         # the scan is shared
        assert kv.get_sync(PAUSED_KEY) is True
        assert asyncio.run(kv.acquire_once("state:lock:x", 60)) is True
    assert kv.get_sync(kv.key("books", "auto")) == {"at": 1}
    assert kv.get_sync(PAUSED_KEY) is None                                   # the owner's autotrader was not paused
    assert asyncio.run(kv.acquire_once("state:lock:x", 60)) is True          # a different lock from the tenant's
    with context.as_tenant("t2"):
        assert kv.get_sync(kv.key("books", "auto")) is None                  # nor is t1's switch t2's
    assert kv._scope("not-a-state-key") == "not-a-state-key"


def test_the_wallet_globals_resolve_to_the_tenants_wallets_inside_their_context(monkeypatch):
    from autotrader import engine as autotrader
    from experiments import books
    from markets import engine as markets
    owner_auto = autotrader.auto_wallet.owner
    assert autotrader.auto_wallet.label == "auto" and autotrader.auto_wallet.path == owner_auto.path
    owner_comp = dict.__getitem__(books.WALLETS, "composite")
    assert books.WALLETS["composite"] is owner_comp and books.WALLETS.get("nope") is None
    accounts.sign_in("admin@example.com"); u = accounts.sign_in("x@example.com"); tid = u["tenant_id"]
    with context.as_tenant(tid):
        assert autotrader.auto_wallet.path == wallets.path_for(tid) and autotrader.auto_wallet.tenant == tid
        assert books.WALLETS["composite"].path == wallets.path_for(tid) and books.WALLETS["composite"].label == "composite"
        assert markets.WALLETS["stocks_trend"].tenant == tid
        assert dict(books.WALLETS.items())["prop"].tenant == tid and all(w.tenant == tid for w in books.WALLETS.values())
    assert books.WALLETS["composite"] is owner_comp                          # back outside: the owner's object, untouched
    assert dict(books.owner_items() if hasattr(books, "owner_items") else books.WALLETS.owner_items())["composite"] is owner_comp


def test_only_elite_tenants_who_opted_in_run_and_only_with_the_flag(monkeypatch):
    monkeypatch.setattr(engines, "FLAG", False)
    accounts.sign_in("admin@example.com")
    u = accounts.sign_in("pro@example.com"); pro = u["tenant_id"]; accounts.set_tier(pro, "pro")
    with pytest.raises(ValueError):
        accounts.update_settings(pro, engine_mode="own")                      # flag off: nobody may opt in
    assert engines.own_tenants() == [] and engines.modes_allowed(accounts.tenant(pro)) == ["mirror"]
    monkeypatch.setattr(engines, "FLAG", True)
    with pytest.raises(ValueError):
        accounts.update_settings(pro, engine_mode="own")                      # pro: not in the tier
    accounts.set_tier(pro, "elite")
    assert engines.modes_allowed(accounts.tenant(pro)) == ["mirror", "own"]
    assert engines.own_tenants() == []                                         # elite but still mirroring by default
    accounts.update_settings(pro, engine_mode="own", books={b: False for b in tiers.DEFAULT_BOOKS_ON})
    assert [t["id"] for t in engines.own_tenants()] == [pro]
    assert asyncio.run(engines.tick())["tenants"] == {pro: 0}                  # every book off: nothing ran
    monkeypatch.setattr(engines, "FLAG", False)
    assert asyncio.run(engines.tick()) == {"skipped": "off"}


def test_a_tenants_engine_run_writes_its_own_state_and_wallet_and_the_owner_is_untouched(monkeypatch):
    tid = _elite_own(monkeypatch)
    seen = []

    async def fake_tick():
        from experiments import books
        seen.append(context.tenant_id())
        kv.set_sync(kv.key("composite", "state"), {"ran": True})
        w = books.WALLETS["composite"]
        w.open_trade(symbol="ETHUSDT", direction="short", entry_price=2000.0, stop_loss=2100.0, take_profit_1=1800.0, take_profit_2=1700.0,
                     notional_usd=1000.0, leverage=2, extra={"strategy": "composite", "shadows": False})

    async def fake_monitor():
        seen.append(("monitor", context.tenant_id()))

    import tenancy.engines as eng
    monkeypatch.setattr(eng, "BOOK_TICKS", [("composite", ("composite",), "tests.test_tenant_engines:_fake_tick")])
    monkeypatch.setattr(eng, "_load", lambda ref: fake_tick)
    from autotrader import engine as autotrader
    monkeypatch.setattr(autotrader, "monitor_now", fake_monitor)
    out = asyncio.run(engines.tick())
    assert out["tenants"] == {tid: 3} and seen == [("monitor", tid), tid]       # auto's monitor, composite's wallet monitor, composite's tick
    assert kv.get_sync(kv.key("composite", "state")) is None                 # the owner's composite state is untouched
    with context.as_tenant(tid):
        assert kv.get_sync(kv.key("composite", "state")) == {"ran": True}
    mine = wallets.get(tid, "composite").get_portfolio()["open_trades"]
    assert len(mine) == 1 and mine[0]["symbol"] == "ETHUSDT"
    from experiments import books
    assert dict.__getitem__(books.WALLETS, "composite").path != wallets.path_for(tid)
    assert engines.status()["tenants_own"] == [tid] and engines.status()["per_tenant"][tid]["books"] == ["auto", "composite"]


def test_on_scans_runs_each_opted_in_autotrader_and_a_failing_engine_is_contained(monkeypatch):
    tid = _elite_own(monkeypatch, books=("auto",))
    other = accounts.sign_in("mirror@example.com")["tenant_id"]
    accounts.set_tier(other, "elite"); accounts.update_settings(other, books={"auto": True})     # mirror mode: not run
    calls = []

    async def fake_on_scans(scans, watched, gm):
        calls.append((context.tenant_id(), len(scans), gm))
        if gm == "boom":
            raise RuntimeError("boom")

    from autotrader import engine as autotrader
    monkeypatch.setattr(autotrader, "on_scans", fake_on_scans)
    assert asyncio.run(engines.on_scans([{"pair": "BTCUSDT"}], {"BTCUSDT"}, {"ok": 1})) == 1
    assert calls == [(tid, 1, {"ok": 1})]
    assert asyncio.run(engines.on_scans([{"pair": "BTCUSDT"}], set(), "boom")) == 0
    assert engines.status()["errors"][-1]["tenant"] == tid and "boom" in engines.status()["errors"][-1]["error"]
    assert asyncio.run(engines.on_scans([], set(), None)) == 0


def test_a_tenant_running_its_own_engines_is_not_mirrored(monkeypatch):
    from trading import monitor
    async def no_monitor(_w):
        return []
    monkeypatch.setattr(monitor, "run", no_monitor)
    own = _elite_own(monkeypatch, books=("auto",))
    mirrored = accounts.sign_in("m@example.com")["tenant_id"]
    accounts.set_tier(mirrored, "pro"); accounts.update_settings(mirrored, books={"auto": True})
    src = {"id": "s1", "symbol": "ETHUSDT", "direction": "short", "entry_price": 2000.0, "stop_loss": 2100.0, "take_profit_1": 1800.0,
           "take_profit_2": 1700.0, "quantity": 2.5, "notional_usd": 5000.0, "risk_usd": 250.0, "leverage": 3, "timeframe": "4h", "exit_policy": "manual_lock"}
    runner.publish("opened", "auto", src, 50_000.0)
    assert asyncio.run(runner.tick())["applied"] == 1
    assert len(wallets.get(mirrored, "auto").get_portfolio()["open_trades"]) == 1
    assert (own, "auto") not in wallets._cache or wallets.get(own, "auto").get_portfolio()["open_trades"] == []


def test_the_feature_is_elite_only_and_the_config_route_carries_the_flag(monkeypatch):
    assert tiers.has("elite", engines.FEATURE) and not tiers.has("pro", engines.FEATURE)
    from fastapi.testclient import TestClient
    import main
    monkeypatch.setattr(engines, "FLAG", True)
    c = TestClient(main.app)
    assert c.get("/api/auth/config").json()["tenant_engines"] is True
    s = c.get("/api/me/settings").json()
    assert s["engine_mode"] == "mirror" and s["engine_modes_allowed"] == ["mirror", "own"]      # the owner is elite
    assert "engines" in c.get("/api/admin/runner").json()
