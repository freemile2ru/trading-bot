"""The "bits" exit shadow on copies: half out at +2%, half back at the entry; it ends with the live copy; its audit."""
import pytest

from experiments import copy_trading as ct
from trading import exits
from trading.paper_trader import PaperWallet

T0 = 1_794_900_000.0


def _copy(**kw):
    t = {"id": "c1", "symbol": "NEARUSDT", "direction": "short", "entry_price": 3.0, "stop_loss": 3.12, "take_profit_1": 1.5, "take_profit_2": 1.2,
         "quantity": 8000.0, "notional_usd": 24000.0, "risk_usd": 960.0, "timeframe": "copy", "leverage": 7, "opened_at": "2026-11-16T03:50:00+00:00",
         "opened_ts": T0, "exit_policy": "manual_lock", "shadows": False, "leader_id": "0xabc", "checked_from_ms": None, "fees_usd": 0.0}
    t.update(kw)
    return t


def test_a_copy_gets_the_bits_shadow_and_a_reversal_leg_does_not(monkeypatch):
    monkeypatch.setattr(exits, "LOCK_MODE", "live")
    sh = exits.make_shadows(_copy(), "copy_trading")
    assert [s["exit_policy"] for s in sh] == ["manual_lock_bits"] and sh[0]["initial_quantity"] == 8000.0
    leg = _copy(leader_id=None, sizing={"notional": 1500.0}, atr_pct=8.0)
    assert "manual_lock_bits" not in [s["exit_policy"] for s in exits.make_shadows(leg, "auto")]
    monkeypatch.setattr(exits, "BITS_SHADOW", False)
    assert exits.make_shadows(_copy(), "copy_trading") == []


def test_bits_sells_half_at_two_percent_and_buys_it_back_at_the_entry():
    t = exits.stamp(_copy(), "manual_lock_bits", T0)
    e = 3.0
    assert exits.apply_bar(t, 3.0, 3.01, 2.97, 2.98, T0 + 300) is None and not t.get("bits_out")               # −1%: nothing
    assert exits.apply_bar(t, 2.98, 2.985, 2.935, 2.95, T0 + 600) is None                                       # low 2.935 = −2.17%: half out at 2.94
    assert t["bits_out"] and t["quantity"] == pytest.approx(4000.0) and t["realized_pnl_usd"] == pytest.approx((e - e * 0.98) * 4000.0, rel=1e-6)
    assert t["stop_loss"] == 3.12                                                                                 # the lock (arm 3% of notional) is not armed at +2.2%
    assert exits.apply_bar(t, 2.95, 3.005, 2.95, 3.0, T0 + 900) is None                                          # back at the entry: the half comes back
    assert not t["bits_out"] and t["bits_readds"] == 1 and t["quantity"] == pytest.approx(8000.0) and t["entry_price"] == pytest.approx(3.0, abs=1e-6)
    assert t["adjustments"][-1]["kind"] == "bits_readd" and t["adjustments"][-2]["kind"] == "bits_out"
    exits.apply_bar(t, 3.0, 3.0, 2.93, 2.95, T0 + 1200)                                                           # out again…
    exits.apply_bar(t, 2.95, 3.0, 2.95, 2.999, T0 + 1500)                                                         # …and back (2 of 2)
    exits.apply_bar(t, 3.0, 3.0, 2.93, 2.95, T0 + 1800)
    assert t["bits_readds"] == 2 and t["bits_out"] and t["quantity"] == pytest.approx(4000.0)
    assert exits.apply_bar(t, 2.95, 3.0, 2.95, 3.0, T0 + 2100) is None and t["bits_out"]                         # no third re-add
    assert exits.apply_bar(t, 3.0, 3.13, 3.0, 3.12, T0 + 2400)[0] == "stop_loss"                                   # the original stop still ends the rest


def test_the_bits_shadow_closes_with_the_live_copy_when_the_leader_closes(tmp_path, monkeypatch):
    monkeypatch.setattr(exits, "LOCK_MODE", "live")
    w = PaperWallet(str(tmp_path / "c.db"), label="copy_trading", initial_balance=50_000)
    t = w.open_trade(symbol="NEARUSDT", direction="short", entry_price=3.0, stop_loss=3.12, take_profit_1=1.5, take_profit_2=1.2, notional_usd=3000.0,
                     timeframe="copy", leverage=5, extra={"strategy": "copy_trading", "exit_policy": "manual", "shadows": False, "leader_id": "0xabc", "leader": "x"})
    assert "error" not in t
    rep = w.shadow_report()
    assert rep["open_shadows"] == 1
    w.close_trade_manually(t["id"], 2.97, reason="leader_closed")
    rep = w.shadow_report()
    assert rep["open_shadows"] == 0 and [r["policy"] for r in rep["results"]] == ["manual_lock_bits"] and rep["results"][0]["reason"] == "live_closed"
    t2 = w.open_trade(symbol="NEARUSDT", direction="short", entry_price=3.0, stop_loss=3.12, take_profit_1=1.5, take_profit_2=1.2, notional_usd=3000.0,
                      timeframe="copy", leverage=5, extra={"strategy": "copy_trading", "exit_policy": "manual", "shadows": False, "leader_id": "0xabc", "leader": "x"})
    w.close_trade_manually(t2["id"], 3.12, reason="stop_loss")                                                   # a price exit: the shadow runs on
    assert w.shadow_report()["open_shadows"] == 1


def test_bits_audit_pairs_copies_with_their_shadows(monkeypatch):
    class W:
        label = "copy_trading"
        def all_trades(self):
            return {"closed": [{"id": f"c{i}", "leader_id": "x", "risk_usd": 100.0, "pnl_usd": 5.0, "opened_ts": float(i), "symbol": "NEARUSDT"} for i in range(45)], "open": []}
        def shadow_report(self):
            return {"results": [{"trade_id": f"c{i}", "policy": "manual_lock_bits", "r": 0.10 + 0.02 * (i % 2), "reason": "live_closed"} for i in range(45)], "open_shadows": 0}
    monkeypatch.setattr(ct, "_unique_wallets", lambda: [W()])
    a = ct.bits_audit()
    assert a["policy"] == "manual_lock_bits" and a["n"] == 45 and a["verdict"] == "adopt" and a["label"].startswith("bits")
