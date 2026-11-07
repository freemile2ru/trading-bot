"""Trade alerts: every open and close on a wallet becomes one email and one text, bursts share a message."""
import json
import time

import pytest

from trading import trade_alerts as ta
from trading.paper_trader import PaperWallet


@pytest.fixture
def sent(monkeypatch):
    """Capture what would be sent; the channels on; no waiting window."""
    from data import notifications
    got = {"email": [], "sms": [], "whatsapp": []}
    monkeypatch.setattr(notifications, "send_email", lambda subject, body, html=None: got["email"].append((subject, body)) or True)
    monkeypatch.setattr(notifications, "send_sms", lambda body: got["sms"].append(body) or True)
    monkeypatch.setattr(notifications, "send_whatsapp", lambda body, title="", when="": got["whatsapp"].append((body, title, when)) or True)
    monkeypatch.setattr(ta, "CHANNELS", {"email", "sms", "whatsapp"})
    monkeypatch.setattr(ta, "WINDOW", 0.3)
    return got


def test_an_open_and_a_close_each_send_an_email_and_a_text(tmp_path, sent):
    w = PaperWallet(str(tmp_path / "w.json"), label="copy_trading")
    w.reset_portfolio(10_000)
    t = w.open_trade("ZECUSDT", "short", 100, 110, 90, 85, risk_pct=1, timeframe="1h",
                     extra={"exit_policy": "manual", "shadows": False, "strategy": "copy_trading", "reasoning": "leader 0x6e87 opened"})
    ta.flush()
    assert len(sent["email"]) == 1 and len(sent["sms"]) == 1
    subject, body = sent["email"][0]
    assert subject.startswith("[Oracle local] opened S ZECUSDT") and "OPENED" in body and "copy_trading" in body
    assert "SHORT ZECUSDT" in body and "stop 110" in body and "risk $100" in body and "why: leader 0x6e87 opened" in body
    assert "ZECUSDT" in sent["sms"][0] and "[Oracle" not in sent["sms"][0]
    body, title, when = sent["whatsapp"][0]
    assert body == sent["sms"][0] and title == "Oracle local" and when.endswith("UTC")

    w.close_trade_manually(t["id"], 95, "manual")
    ta.flush()
    assert len(sent["email"]) == 2
    subject, body = sent["email"][1]
    assert subject.startswith("[Oracle local] closed S ZECUSDT +$") and "CLOSED" in body and "manual" in body and "R)" in body and "held" in body


def test_a_burst_of_legs_shares_one_message_and_a_lane_is_named(tmp_path, sent):
    w = PaperWallet(str(tmp_path / "w.json"), label="auto")
    w.reset_portfolio(50_000)
    for sym in ("RUNEUSDT", "ENAUSDT", "DASHUSDT"):
        w.open_trade(sym, "short", 100, 110, 90, 85, risk_pct=0.5, timeframe="1d",
                     extra={"exit_policy": "manual", "shadows": False, "strategy": "auto", "lane": "weekly"})
    ta.flush()
    assert len(sent["email"]) == 1 and len(sent["sms"]) == 1
    subject, body = sent["email"][0]
    assert subject == "[Oracle local] opened S RUNEUSDT; opened S ENAUSDT; opened S DASHUSDT"
    assert body.count("OPENED") == 3 and "auto · weekly lane" in body


def test_off_by_default_outside_production_and_never_raises(tmp_path, monkeypatch):
    from data import notifications
    monkeypatch.setattr(ta, "CHANNELS", set())
    monkeypatch.setattr(notifications, "send_email", lambda *a: (_ for _ in ()).throw(RuntimeError("no")))
    w = PaperWallet(str(tmp_path / "w.json"), label="main")
    w.reset_portfolio(10_000)
    t = w.open_trade("BTCUSDT", "long", 100, 90, 110, 120, risk_pct=1, timeframe="1h", extra={"exit_policy": "manual", "shadows": False})
    assert "error" not in t and ta._q.empty()
    monkeypatch.setattr(ta, "CHANNELS", {"email"})
    monkeypatch.setattr(ta, "WINDOW", 0.1)
    w.close_trade_manually(t["id"], 105, "manual")            # the sender raises: the close still stands, the alert is logged and dropped
    ta.flush()
    assert w.get_portfolio()["closed_trades"][0]["close_reason"] == "manual"


def test_compose_sums_a_red_minute():
    evs = [{"kind": "closed", "book": "copy_trading", "ts": time.time(),
            "trade": {"symbol": s, "direction": "long", "pnl_usd": p, "risk_usd": 100, "close_reason": "stop_loss",
                      "entry_price": 1, "exit_price": 0.9, "opened_at": "2026-11-06T01:00:00", "closed_at": "2026-11-06T03:00:00"}}
           for s, p in (("AUSDT", -100), ("BUSDT", -50))]
    subject, body = ta.compose(evs)
    assert subject == "[Oracle local] closed L AUSDT −$100; closed L BUSDT −$50"
    assert "2 closed: −$150 net" in body and "(-1.00R)" in body and "held 2.0h" in body


def test_the_email_has_a_table_per_kind_with_the_same_facts():
    evs = [{"kind": "opened", "book": "auto", "ts": time.time(),
            "trade": {"symbol": "BNBUSDT", "direction": "short", "lane": "reversal", "notional_usd": 1000, "leverage": 8, "entry_price": 694.211674,
                      "stop_loss": 763.862, "risk_usd": 100, "oracle_reasoning": "reversal score -0.182: short BNBUSDT (rank bottom 2 of 24)"}},
           {"kind": "closed", "book": "copy_trading", "ts": time.time(),
            "trade": {"symbol": "ZECUSDT", "direction": "short", "pnl_usd": 97.9, "risk_usd": 165, "close_reason": "trailing_stop", "locked_usd": 110,
                      "entry_price": 1358.51, "exit_price": 1301.2, "opened_at": "2026-11-06T17:28:00", "closed_at": "2026-11-06T21:07:00"}}]
    html = ta.compose_html(evs)
    assert html.count("<table") == 2 and "OPENED" in html and "CLOSED" in html
    assert "BNBUSDT" in html and "auto · reversal lane" in html and "763.86" in html and "(10.0%)" in html and "$100" in html and "rank bottom 2 of 24" in html
    assert "ZECUSDT" in html and "+$98" in html and "(+0.59R)" in html and "trailing_stop" in html and "3.6h" in html and "$110" in html
    assert "<script" not in html and "&lt;" not in html                       # nothing to escape here, and nothing executable ever
    subject, body = ta.compose(evs)
    assert "🟢 OPENED" in body and "SHORT BNBUSDT · $1,000 at 8x" in body and "entry 694.21 · stop 763.86 (10.0%) · risk $100" in body
    assert "🟢 CLOSED" in body and "SHORT ZECUSDT · +$98 (+0.59R)" in body and "trailing_stop · held 3.6h · 1,359 → 1,301 · lock kept $110" in body


class _FakeTwilio:
    """A Twilio client that fails free-form sends outside the window and accepts templates."""
    def __init__(self, window_open: bool, template_text: str = "Your {{1}} order of {{2}} has shipped and should be delivered on {{3}}. Details: {{4}}"):
        self.window_open, self.sent, self._n = window_open, [], 0
        text = template_text
        class _Content:
            class v1:
                @staticmethod
                def contents(sid):
                    class C:
                        @staticmethod
                        def fetch():
                            class T: types = {"twilio/text": {"body": text}}
                            return T()
                    return C()
        self.content = _Content()

    class _Msg:
        def __init__(self, status, err): self.status, self.error_code = status, err
        def fetch(self): return self

    @property
    def messages(self):
        fake = self
        class M:
            def create(self, **kw):
                fake.sent.append(kw); fake._n += 1
                m = _FakeTwilio._Msg("delivered" if fake.window_open or kw.get("content_sid") else "failed",
                                     None if fake.window_open or kw.get("content_sid") else 63016)
                m.sid = f"SM{fake._n}"
                return m
            def __call__(self, sid):
                return fake.sent and _FakeTwilio._Msg(*(("delivered", None) if fake.window_open or fake.sent[-1].get("content_sid") else ("failed", 63016)))
        return M()


def test_whatsapp_falls_back_to_the_template_outside_the_window(monkeypatch):
    from data import notifications as n
    for k, v in {"TWILIO_ACCOUNT_SID": "AC1", "TWILIO_AUTH_TOKEN": "t", "NOTIFY_PHONE": "+15550001111", "TWILIO_WHATSAPP_TEMPLATE_SID": "HX123"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("NOTIFY_WHATSAPP", raising=False)
    fake = _FakeTwilio(window_open=True)
    monkeypatch.setattr(n, "_twilio_client", lambda: fake)
    assert n.send_whatsapp("OPENED · SHORT INJUSDT", title="Oracle prod", when="06 Nov 12:04 UTC") is True
    assert len(fake.sent) == 1 and fake.sent[0]["from_"] == "whatsapp:+14155238886" and fake.sent[0]["to"] == "whatsapp:+15550001111"
    fake = _FakeTwilio(window_open=False)
    monkeypatch.setattr(n, "_twilio_client", lambda: fake)
    assert n.send_whatsapp("CLOSED · SHORT ZECUSDT +$310", title="Oracle prod", when="06 Nov 16:05 UTC") is True
    assert len(fake.sent) == 2 and fake.sent[1]["content_sid"] == "HX123"
    vars4 = json.loads(fake.sent[1]["content_variables"])
    assert vars4 == {"1": "Oracle prod", "2": "trade update", "3": "06 Nov 16:05 UTC", "4": "CLOSED · SHORT ZECUSDT +$310"}
    # The two-slot appointment template (the sandbox page's sample): title in the first slot, the alert in the last.
    n._slots.clear()
    fake = _FakeTwilio(window_open=False, template_text="Your appointment is coming up on {{1}} at {{2}}")
    monkeypatch.setattr(n, "_twilio_client", lambda: fake)
    assert n.send_whatsapp("OPENED · LONG CCUSDT", title="Oracle prod", when="now") is True
    assert json.loads(fake.sent[1]["content_variables"]) == {"1": "Oracle prod", "2": "OPENED · LONG CCUSDT"}
    n._slots.clear()
    fake = _FakeTwilio(window_open=False, template_text="{{1}} is your verification code. For your security, do not share this code.")
    monkeypatch.setattr(n, "_twilio_client", lambda: fake)
    assert n.send_whatsapp("OPENED · LONG CCUSDT", title="Oracle prod") is True
    assert json.loads(fake.sent[1]["content_variables"]) == {"1": "Oracle prod · OPENED · LONG CCUSDT"}
    # The sandbox's own templates are known without the Content API (which 404s on them).
    monkeypatch.setenv("TWILIO_WHATSAPP_TEMPLATE_SID", "HXb5b62575e6e4ff6129ad7c8efe1f983e")
    n._slots.clear(); n._slots.update(n.SANDBOX_TEMPLATES)
    fake = _FakeTwilio(window_open=False, template_text="unreadable")
    monkeypatch.setattr(n, "_twilio_client", lambda: fake)
    assert n.send_whatsapp("CLOSED · LONG CCUSDT +$73", title="Oracle prod") is True
    assert json.loads(fake.sent[1]["content_variables"]) == {"1": "Oracle prod", "2": "CLOSED · LONG CCUSDT +$73"}
    monkeypatch.delenv("TWILIO_WHATSAPP_TEMPLATE_SID")
    fake = _FakeTwilio(window_open=False)
    monkeypatch.setattr(n, "_twilio_client", lambda: fake)
    assert n.send_whatsapp("x") is False                                   # window closed, nothing to fall back on
