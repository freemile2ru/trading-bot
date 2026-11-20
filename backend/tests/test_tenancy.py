"""Accounts, tiers, the subscriber policy, the tenant runner and billing (2026-11-20, docs/saas.md)."""
import asyncio
import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from storage import state as kv
from tenancy import accounts, billing, context, gate, google, runner, sessions, tiers, wallets


@pytest.fixture(autouse=True)
def _accounts(tmp_path, monkeypatch):
    accounts.use(str(tmp_path / "accounts.db"))
    monkeypatch.setattr(wallets, "TENANTS_DIR", str(tmp_path / "tenants"))
    wallets.forget()
    runner._queue.clear()
    runner._state["mirrored_24h"].clear(); runner._state["errors"].clear()
    monkeypatch.setattr(accounts, "OWNER_EMAILS", set())
    monkeypatch.setenv("APP_ENV", "test")
    yield
    wallets.forget()
    accounts.use(str(tmp_path / "gone.db"))


@pytest.fixture
def client():
    import main
    return TestClient(main.app)


def _login(client, email, name="Someone"):
    r = client.post("/api/auth/dev-login", json={"email": email, "name": name})
    assert r.status_code == 200, r.text
    return r.json()


def test_the_catalogue_is_the_users_three_tiers():
    cat = tiers.catalogue()
    assert [t["id"] for t in cat["tiers"]] == ["free", "pro", "elite"]
    assert [t["price_usd_month"] for t in cat["tiers"]] == [0, 49, 199]
    assert tiers.upgrade_for("mentor") == "elite" and tiers.upgrade_for("trades") == "pro" and tiers.upgrade_for("news") == "free"
    assert not tiers.has("free", "trades") and tiers.has("pro", "trades") and tiers.has("elite", "mirror.exchange")
    assert tiers.limits("pro")["paper_books"] == 5 and tiers.limits("elite")["paper_books"] is None
    assert all(f in tiers.FEATURES for t in cat["tiers"] for f in t["features"])


def test_without_a_session_the_local_instance_is_the_owner(client):
    me = client.get("/api/auth/me").json()
    assert me["user"]["role"] == "admin" and me["tenant"]["id"] == "owner" and me["tenant"]["tier"] == "elite"
    assert "mirror.exchange" in me["features"]
    assert client.get("/api/pricing").json()["current_tier"] == "elite"
    assert client.get("/api/admin/runner").status_code == 200 and client.get("/api/admin/tenants").status_code == 200   # the local owner administers
    cfg = client.get("/api/auth/config").json()
    assert cfg["dev_login"] is True and cfg["auth_required"] is False and cfg["google_client_id"] is None


def test_the_first_local_sign_in_is_the_admin_and_the_next_gets_a_free_tenant(client):
    first = _login(client, "me@example.com", "Me")
    assert first["user"]["role"] == "admin" and first["tenant"]["id"] == "owner"
    client.cookies.clear()
    second = _login(client, "sub@example.com", "Sub")
    assert second["user"]["role"] == "owner" and second["tenant"]["id"] != "owner" and second["tenant"]["tier"] == "free"
    assert second["features"] == ["signals.delayed", "news", "calendar"] and second["limits"]["signal_delay_min"] == 15
    me = client.get("/api/auth/me").json()                                  # the cookie carries the session
    assert me["user"]["email"] == "sub@example.com" and me["tenant"]["id"] == second["tenant"]["id"]
    client.post("/api/auth/logout")
    assert client.get("/api/auth/me").json()["user"]["id"] == "owner"       # back to the local owner view


def test_a_free_subscriber_is_policed_by_feature_and_never_reaches_the_owners_money(client):
    _login(client, "admin@example.com"); client.cookies.clear()
    _login(client, "free@example.com")
    r = client.get("/api/trades/all")
    assert r.status_code == 402 and r.json()["feature"] == "trades" and r.json()["upgrade"] == "pro"
    assert client.get("/api/live/status").status_code == 403
    assert client.post("/api/paper/trade", json={}).status_code == 403
    assert client.post("/api/trade/order", json={}).status_code == 403
    assert client.get("/api/admin/tenants").status_code == 403
    assert client.get("/api/auction/status").status_code == 402
    assert client.get("/api/pricing").status_code == 200 and client.get("/api/billing/status").status_code == 200
    assert client.post("/api/billing/checkout", json={"tier": "pro"}).status_code == 503     # Stripe not configured here
    # the policy table itself
    assert gate.feature_for_read("/api/trades/all") == "trades" and gate.feature_for_read("/api/live/status") is False
    assert gate.feature_for_read("/api/signals/fired") == "signals.delayed" and gate.feature_for_read("/api/pricing") is None
    with context.as_tenant("t1", {"role": "owner", "tier": "free"}):
        assert gate.policy("POST", "/api/books/auto", "free")[0] == 402
        assert gate.policy("POST", "/api/books/auto", "pro") is None
        assert gate.policy("POST", "/api/positions/auto/abc/close", "pro") is None
        assert gate.policy("POST", "/api/positions/auto/abc/other", "pro")[0] == 403
        assert gate.policy("GET", "/api/news/latest", "free") is None


def test_an_admin_upgrades_a_tenant_whose_state_and_record_are_their_own(client, monkeypatch):
    _login(client, "admin@example.com"); admin_cookies = dict(client.cookies); client.cookies.clear()
    sub = _login(client, "pro@example.com"); sub_cookies = dict(client.cookies); tid = sub["tenant"]["id"]
    client.cookies.clear(); client.cookies.update(admin_cookies)
    r = client.post(f"/api/admin/tenants/{tid}/tier", json={"tier": "pro"})
    assert r.status_code == 200 and r.json()["tier"] == "pro"
    assert tid in {t["id"] for t in client.get("/api/admin/tenants").json()["tenants"]}
    assert client.post("/api/admin/tenants/owner/tier", json={"tier": "free"}).status_code == 400
    client.cookies.clear(); client.cookies.update(sub_cookies)
    assert client.get("/api/auth/me").json()["tenant"]["tier"] == "pro"
    r = client.get("/api/trades/all")
    assert r.status_code == 200 and r.json()["rows"] == [] and r.json()["total"] == 0   # their own (empty) wallets, not the owner's record
    # the kv prefix: a subscriber's book switch is theirs alone
    from trading import book_switches
    with context.as_tenant(tid):
        assert kv._scope(kv.key("books", "auto")) == f"state:t:{tid}:books:auto"
    assert kv._scope(kv.key("books", "auto")) == "state:books:auto"
    r = client.post("/api/books/auto", json={"enabled": False})
    assert r.status_code == 200
    with context.as_tenant(tid):
        assert book_switches.enabled("auto") is False
    assert book_switches.enabled("auto") is True                             # the owner's autotrader is untouched
    # settings: the tier's defaults, then their edits, within the tier's limits
    s = client.get("/api/me/settings").json()
    assert s["books"]["auto"] is True and s["paper_capital_usd"] == 10_000 and "auction" not in s["allowed_books"]
    r = client.post("/api/me/settings", json={"risk_x": 0.5, "books": {"composite": True, "auction": True}})
    assert r.status_code == 200 and r.json()["risk_x"] == 0.5 and r.json()["books"]["composite"] is True and "auction" not in r.json()["books"]
    assert client.post("/api/me/exchange-keys", json={"venue": "binance", "api_key": "k" * 20, "api_secret": "s" * 20}).status_code == 402


def test_the_runner_mirrors_an_engine_entry_at_risk_parity_and_closes_it_with_the_source(monkeypatch):
    from trading import monitor
    async def no_monitor(_w):
        return []
    monkeypatch.setattr(monitor, "run", no_monitor)
    u = accounts.sign_in("admin@example.com", "A")
    sub = accounts.sign_in("pro@example.com", "P")
    tid = sub["tenant_id"]
    accounts.set_tier(tid, "pro")
    assert [t["id"] for t in accounts.active_tenants()] == [tid]
    src = {"id": "src1", "symbol": "ETHUSDT", "direction": "short", "entry_price": 2000.0, "stop_loss": 2100.0, "take_profit_1": 1800.0,
           "take_profit_2": 1700.0, "quantity": 2.5, "notional_usd": 5000.0, "risk_usd": 250.0, "leverage": 3, "timeframe": "4h",
           "exit_policy": "manual_lock", "lock_arm_pct": 0.01, "lock_trail_atr": 1.0, "atr_pct": 6.0, "lane": "reversal"}
    runner.publish("opened", "auto", src, 50_000.0)                           # the source risks 0.5% of a $50k book
    runner.publish("opened", "burst_up", {**src, "id": "src2"}, 10_000.0)      # a book the tenant does not run
    assert asyncio.run(runner.tick())["applied"] == 1
    w = wallets.get(tid, "auto")
    legs = w.get_portfolio()["open_trades"]
    assert len(legs) == 1 and legs[0]["source_trade_id"] == "src1" and legs[0]["direction"] == "short" and legs[0]["mirror"] is True
    assert legs[0]["risk_usd"] == pytest.approx(50.0, rel=0.02)              # 0.5% of their $10k, at the same stop
    assert legs[0]["notional_usd"] == pytest.approx(1000.0, rel=0.02) and legs[0]["exit_policy"] == "manual_lock" and legs[0]["lock_trail_atr"] == 1.0
    assert wallets.get(tid, "burst_up").get_portfolio()["open_trades"] == [] if ("burst_up" in wallets.existing(tid)) else True
    runner.publish("closed", "auto", {**src, "exit_price": 1900.0, "close_reason": "trailing_stop"}, 50_250.0)
    asyncio.run(runner.tick())
    closed = w.all_trades()["closed"]
    assert len(closed) == 1 and closed[0]["close_reason"] == "source: trailing_stop" and closed[0]["pnl_usd"] > 0
    assert runner.status()["mirrored_24h"] == 1 and runner.status()["tenants_active"] == 1
    # sizing on its own
    assert runner.size_for(src, 50_000.0, 10_000.0, 2.0) == (100.0, 2000.0)
    assert runner.size_for({**src, "risk_usd": 0}, 50_000.0, 10_000.0, 1.0) == (0.0, 0.0)


def test_a_tenant_wallet_never_publishes_and_an_owner_wallet_does(tmp_path):
    from trading.paper_trader import PaperWallet
    kw = dict(symbol="SOLUSDT", direction="long", entry_price=100.0, stop_loss=95.0, take_profit_1=110.0, take_profit_2=120.0, notional_usd=500.0, leverage=2)
    mine = PaperWallet(str(tmp_path / "t.db"), label="auto", tenant="t1")
    mine.open_trade(**kw)
    assert len(runner._queue) == 0
    owner = PaperWallet(str(tmp_path / "o.db"), label="auto")
    t = owner.open_trade(**kw)
    assert len(runner._queue) == 1 and runner._queue[0]["kind"] == "opened" and runner._queue[0]["book"] == "auto"
    owner.close_trade_manually(t["id"], 101.0, "manual")
    assert len(runner._queue) == 2 and runner._queue[1]["kind"] == "closed"
    other = PaperWallet(str(tmp_path / "x.db"), label="signals_claude")       # not a mirrorable book
    other.open_trade(**kw)
    assert len(runner._queue) == 2


def test_a_tenant_wallet_fires_none_of_the_owners_alerts(tmp_path, monkeypatch):
    from trading import trade_alerts
    from trading.paper_trader import PaperWallet
    fired = []
    monkeypatch.setattr(trade_alerts, "opened", lambda label, t: fired.append(("opened", label)))
    monkeypatch.setattr(trade_alerts, "closed", lambda label, t: fired.append(("closed", label)))
    kw = dict(symbol="SOLUSDT", direction="long", entry_price=100.0, stop_loss=95.0, take_profit_1=110.0, take_profit_2=120.0, notional_usd=500.0, leverage=2)
    mine = PaperWallet(str(tmp_path / "t.db"), label="auto", tenant="t1")
    t = mine.open_trade(**kw)
    mine.close_trade_manually(t["id"], 101.0, "manual")
    assert fired == []
    owner = PaperWallet(str(tmp_path / "o.db"), label="auto")
    t = owner.open_trade(**kw)
    owner.close_trade_manually(t["id"], 101.0, "manual")
    assert fired == [("opened", "auto"), ("closed", "auto")]
    assert all("coming soon" in tiers.FEATURES[k][0] for k in ("alerts.email", "claude.second_look", "mirror.exchange", "api.access"))


def test_sessions_round_trip_and_reject_tampering():
    tok = sessions.issue("u1", "t1")
    assert sessions.read(tok)["sub"] == "u1" and sessions.read(tok)["tid"] == "t1"
    assert sessions.read(tok[:-2] + "xx") is None and sessions.read(None) is None
    assert sessions.read(sessions.issue("u1", "t1", ttl_s=-5)) is None


def test_google_claims_must_be_ours_and_verified(monkeypatch):
    monkeypatch.setattr(google, "CLIENT_ID", "abc.apps.googleusercontent.com")
    good = {"aud": "abc.apps.googleusercontent.com", "iss": "https://accounts.google.com", "exp": time.time() + 60,
            "email": "x@example.com", "email_verified": "true", "name": "X", "sub": "123"}
    assert google.check_claims(good)["email"] == "x@example.com"
    for bad in ({**good, "aud": "someone-else"}, {**good, "iss": "evil"}, {**good, "exp": time.time() - 1}, {**good, "email_verified": "false"}):
        with pytest.raises(google.BadToken):
            google.check_claims(bad)


def test_exchange_keys_are_encrypted_at_rest_and_never_shown():
    accounts.sign_in("admin@example.com")
    u = accounts.sign_in("elite@example.com")
    tid = u["tenant_id"]
    accounts.set_exchange_key(tid, "binance", "AKIA-the-key-1234", "very-secret")
    shown = accounts.exchange_keys(tid)
    assert shown == {"binance": {"configured": True, "label": "…1234"}}
    assert accounts.exchange_secret(tid, "binance") == ("AKIA-the-key-1234", "very-secret")
    con = accounts._db()
    raw = con.execute("SELECT api_key_enc, api_secret_enc FROM exchange_keys").fetchone()
    assert "AKIA" not in raw[0] and "very-secret" not in raw[1]
    accounts.delete_exchange_key(tid, "binance")
    assert accounts.exchange_keys(tid) == {} and accounts.exchange_secret(tid, "binance") is None


def _signed(payload: dict, secret: str, ts: int | None = None) -> tuple[bytes, str]:
    body = json.dumps(payload).encode()
    ts = ts or int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return body, f"t={ts},v1={sig}"


def test_stripe_events_set_the_tier_once_and_only_with_a_good_signature(monkeypatch):
    accounts.sign_in("admin@example.com")
    u = accounts.sign_in("buyer@example.com")
    tid = u["tenant_id"]
    body, header = _signed({"id": "evt_1"}, "whsec_test")
    assert billing.verify_signature(body, header, "whsec_test") is True
    assert billing.verify_signature(body, header, "whsec_other") is False
    assert billing.verify_signature(body, _signed({"id": "evt_1"}, "whsec_test", ts=int(time.time()) - 3600)[1], "whsec_test") is False
    ev = {"id": "evt_co", "type": "checkout.session.completed",
          "data": {"object": {"object": "checkout.session", "client_reference_id": tid, "customer": "cus_1", "subscription": "sub_1", "metadata": {"tenant_id": tid, "tier": "elite"}}}}
    assert billing.handle_event(ev)["tier"] == "elite" and accounts.tenant(tid)["tier"] == "elite" and accounts.tenant(tid)["stripe_customer_id"] == "cus_1"
    assert billing.handle_event(ev)["handled"] is False                      # the same event again does nothing
    monkeypatch.setattr(billing, "PRICES", {"pro": "price_pro", "elite": "price_elite"})
    upd = {"id": "evt_up", "type": "customer.subscription.updated",
           "data": {"object": {"id": "sub_1", "customer": "cus_1", "status": "past_due", "current_period_end": 1_800_000_000, "cancel_at_period_end": True,
                               "items": {"data": [{"price": {"id": "price_pro"}}]}, "metadata": {}}}}
    out = billing.handle_event(upd)
    t = accounts.tenant(tid)
    assert out["tier"] == "pro" and t["tier"] == "pro" and t["status"] == "past_due" and t["cancel_at_period_end"] is True
    assert [x["id"] for x in accounts.active_tenants()] == [tid]              # past_due still runs
    billing.handle_event({"id": "evt_del", "type": "customer.subscription.deleted", "data": {"object": {"id": "sub_1", "customer": "cus_1", "metadata": {}}}})
    t = accounts.tenant(tid)
    assert t["tier"] == "free" and t["status"] == "canceled" and accounts.active_tenants() == []
    assert billing.handle_event({"id": "evt_x", "type": "invoice.paid", "data": {"object": {"customer": "cus_nobody"}}})["handled"] is False


def test_auth_required_turns_the_missing_session_into_401(client, monkeypatch):
    from tenancy import middleware
    monkeypatch.setattr(middleware, "AUTH_REQUIRED", True)
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/trades/all").status_code == 401
    assert client.get("/api/pricing").status_code == 200 and client.get("/api/pricing").json()["current_tier"] is None
    _login(client, "admin@example.com")
    assert client.get("/api/auth/me").status_code == 200 and client.get("/api/trades/all").status_code == 200


def test_production_requires_sign_in_only_once_google_is_configured(monkeypatch):
    from tenancy import middleware
    monkeypatch.delenv("AUTH_REQUIRED", raising=False)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    assert middleware._default_required() is False                             # today's prod: Caddy in front, no accounts yet
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "abc.apps.googleusercontent.com")
    assert middleware._default_required() is True
    monkeypatch.setenv("AUTH_REQUIRED", "false")
    assert middleware._default_required() is False
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    assert sessions.read("not-a-token") is None                                 # production without a secret: no session, no 500
