"""The copy book's second look — our own read of the tape before a leader's position is copied (2026-11-08, the user's
ask after prod's first dozen copies: "factor in your own direction and analysis before copying").

What the first twelve closed copies said (2026-11-04 → 11-08, +$349 net): the five losers were a 20× leader's DRAM long
(now blocked by the leverage cap), a CAP short into a coin up 27% on the day at the top of its range (−$168), an ETH long
at the top of its range the leader closed in four minutes, a flat BTC short, and a BTC long stopped by a 2% Sunday-night
drop. Every alignment rule we could compute (with the coin's trend, with BTC's tape, with the day's move) would have
removed more winners than losers — the four ZEC shorts that made the book were all counter-trend fades. So this is not a
trend filter. It looks for the handful of things that were plainly wrong at entry and would be wrong again:

  disagree  we already hold the OPPOSITE side of the same coin on a live book (the autotrader's reversal short against a
            copied long, say) — the house cannot be long and short the same coin for two different reasons;
            or Claude disagrees AND at least one tape concern stands (Claude alone never vetoes: research/signal_rebuild
            and the retired Claude swing book showed its direction calls carry no information on their own)
  doubt     one tape concern: the coin is parabolic (≥ PARABOLIC_PCT on the day) and the copy fades it from the top or
            bottom quarter of its range; the copy chases a coin already ≥ CHASE_PCT beyond its 24h VWAP in the trade's direction; the coin
            sits at the extreme of its 24h range (≥ EXTREME_PCT or ≤ 100 − EXTREME_PCT) in the trade's direction; BTC's
            24h tape is against the trade; or Claude doubts
  agree     none of the above

MODE (COPY_SECOND_LOOK): "veto" — disagree blocks the copy (recorded as a skip, judged like every skip), doubt halves the
leg (DOUBT_X); "record" — the read is written on the copy and nothing changes; "off" — not computed. Claude's read
(COPY_SECOND_LOOK_CLAUDE) is one short call per candidate copy, capped at CLAUDE_DAILY a day, and is recorded whatever
the mode. Every copy carries its read (second_look_view / _why / _x), so the audit on the Copy page says what each view
went on to earn — the rule is judged by its own record, and the thresholds are set now, before it has one.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone

from storage import state as kv

logger = logging.getLogger("experiments.copy_second_look")

MODE = os.getenv("COPY_SECOND_LOOK", "veto")                           # veto | record | off
CLAUDE_ON = os.getenv("COPY_SECOND_LOOK_CLAUDE", "true").lower() == "true"
CLAUDE_DAILY = int(os.getenv("COPY_SECOND_LOOK_CLAUDE_DAILY", "40"))
DOUBT_X = float(os.getenv("COPY_SECOND_LOOK_DOUBT_X", "0.5"))
PARABOLIC_PCT = float(os.getenv("COPY_SECOND_LOOK_PARABOLIC_PCT", "15"))
CHASE_PCT = float(os.getenv("COPY_SECOND_LOOK_CHASE_PCT", "3"))
EXTREME_PCT = float(os.getenv("COPY_SECOND_LOOK_EXTREME_PCT", "90"))
PARABOLIC_POS = 75.0                                                   # a parabolic coin faded from the top (bottom) quarter of its range
VIEWS = ("agree", "doubt", "disagree")

PROMPT = """You are the second pair of eyes on a copy trade. A vetted leader on a perpetuals venue has just opened the position
described; our book is about to mirror it with a tight stop (their typical loss) and will exit when they do. You see the
coin's tape over the last day and BTC's. Say whether you agree with taking it now, doubt it, or disagree, and give one
sentence of reason grounded in the numbers given — not a forecast, a judgement of whether anything about the entry is
plainly wrong (chasing an extended move, a parabolic coin, an exhausted range, the whole market leaning the other way).
Reply with JSON only: {"view": "agree" | "doubt" | "disagree", "reason": "<one sentence>"}."""


async def features(symbol: str, side: str, now: float | None = None) -> dict:
    """The coin's last day in a few numbers: the 4h and 24h moves, where it sits against its 24h VWAP and in its 24h range,
    the slope of its 20h mean, and BTC's 24h tape."""
    from data.binance_client import fetch_ohlcv
    from trading import tape
    bars = await fetch_ohlcv(symbol, "1h", 48)
    bars = [b for b in bars if b.get("close_time") and b["close_time"] <= int((now or time.time()) * 1000)] or bars[:-1]
    closes = [b["close"] for b in bars]
    px = closes[-1]
    last24 = bars[-24:]
    vol = sum(b["volume"] for b in last24) or 1e-9
    vwap = sum(b["quote_volume"] for b in last24) / vol if all("quote_volume" in b for b in last24) else sum(b["close"] * b["volume"] for b in last24) / vol
    hi, lo = max(b["high"] for b in last24), min(b["low"] for b in last24)
    ma20, ma_prev = sum(closes[-20:]) / 20, sum(closes[-40:-20]) / 20 if len(closes) >= 40 else None
    btc = await tape.btc_tape(now)
    return {"px": px, "r4h_pct": round((px / closes[-5] - 1) * 100, 2) if len(closes) >= 5 else None,
            "r24h_pct": round((px / closes[-25] - 1) * 100, 2) if len(closes) >= 25 else None,
            "vs_vwap24_pct": round((px / vwap - 1) * 100, 2), "pos24_pct": round((px - lo) / (hi - lo) * 100) if hi > lo else 50,
            "ma20_slope_pct": round((ma20 / ma_prev - 1) * 100, 2) if ma_prev else None,
            "btc_tilt": (btc or {}).get("tilt"), "btc_vs_vwap_pct": (btc or {}).get("pct")}


def own_opposite(symbol: str, side: str) -> str | None:
    """A live book holding the other side of this coin, or None."""
    try:
        from trading import history
        for book, w in history.WALLETS.items():
            for t in w.get_portfolio()["open_trades"]:
                if t.get("symbol") == symbol and t.get("direction") and t["direction"] != side:
                    return f"we are {t['direction']} {symbol} on {book}" + (f" ({t['lane']} lane)" if t.get("lane") else "")
    except Exception as e:
        logger.debug("own positions unavailable: %s", e)
    return None


def concerns(f: dict, side: str) -> list[str]:
    """The tape concerns a copy raises, in plain words (none → the tape has nothing against it)."""
    s = 1 if side == "long" else -1
    out = []
    r24, vs, pos = f.get("r24h_pct"), f.get("vs_vwap24_pct"), f.get("pos24_pct")
    if r24 is not None and abs(r24) >= PARABOLIC_PCT and (r24 > 0) != (s > 0) and pos is not None and (pos >= PARABOLIC_POS if s < 0 else pos <= 100 - PARABOLIC_POS):
        out.append(f"parabolic: {r24:+.1f}% on the day and {'shorting' if s < 0 else 'buying'} it at the {'top' if s < 0 else 'bottom'} of its range ({pos:.0f}%)")
    if vs is not None and s * vs >= CHASE_PCT:
        out.append(f"chasing: already {vs:+.1f}% {'above' if vs > 0 else 'below'} its 24h VWAP the trade's way")
    if pos is not None and ((s > 0 and pos >= EXTREME_PCT) or (s < 0 and pos <= 100 - EXTREME_PCT)):
        out.append(f"extreme: {'buying' if s > 0 else 'selling'} at {pos:.0f}% of its 24h range")
    if f.get("btc_tilt") and ((side == "long" and f["btc_tilt"] == "down") or (side == "short" and f["btc_tilt"] == "up")):
        out.append(f"BTC tape against: {f.get('btc_vs_vwap_pct', 0):+.2f}% vs its 24h VWAP")
    return out


async def claude_view(symbol: str, side: str, f: dict, leader: dict, cons: list[str]) -> dict | None:
    """Claude's one-sentence read, or None (off, capped, no key, or failed). Never raises."""
    if not CLAUDE_ON:
        return None
    try:
        day = kv.key("copy", "second_look", "claude", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        if int(kv.get_sync(day) or 0) >= CLAUDE_DAILY:
            return {"view": None, "reason": "daily cap reached"}
        await kv.incr_with_expire(day, 172800)
        from oracle.claude_oracle import MODEL, get_client, parse_json, response_text
        body = {"copy": {"symbol": symbol, "side": side}, "leader": leader, "tape": f, "rule_concerns": cons}
        resp = await get_client().messages.create(model=MODEL, max_tokens=1500, system=PROMPT,
                                                  messages=[{"role": "user", "content": json.dumps(body, default=str)}])
        ans = parse_json(response_text(resp))
        view = str(ans.get("view") or "").lower()
        return {"view": view if view in VIEWS else None, "reason": str(ans.get("reason") or "")[:240]}
    except Exception as e:
        logger.warning("second look: Claude read failed for %s %s: %s", side, symbol, e)
        return {"view": None, "reason": f"failed: {e.__class__.__name__}"}


def verdict(cons: list[str], opposite: str | None, claude: dict | None) -> tuple[str, str]:
    """(view, why) from the pieces — the rule in the docstring."""
    if opposite:
        return "disagree", f"opposite position: {opposite}"
    cv = (claude or {}).get("view")
    if cv == "disagree" and cons:
        return "disagree", f"Claude disagrees ({(claude or {}).get('reason', '')}) and the tape: " + "; ".join(cons)
    if cons:
        return "doubt", "; ".join(cons) + (f" · Claude: {cv} — {claude.get('reason', '')}" if cv else "")
    if cv in ("doubt", "disagree"):
        return "doubt", f"Claude {cv}s: {(claude or {}).get('reason', '')}"
    return "agree", ("tape clear" + (f" · Claude agrees — {claude.get('reason', '')}" if cv == "agree" else ""))


async def look(symbol: str, side: str, leader: dict | None = None, now: float | None = None) -> dict:
    """Our read of a copy about to be taken: {"view", "why", "x", "concerns", "claude", "features"}. Never raises; a read
    that cannot be computed is "agree" with the error noted, so a feed outage never silences the copy book."""
    if MODE == "off":
        return {"view": "agree", "why": "second look off", "x": 1.0, "concerns": [], "claude": None, "features": None}
    try:
        f = await features(symbol, side, now)
        cons = concerns(f, side)
        opposite = own_opposite(symbol, side)
        claude = await claude_view(symbol, side, f, leader or {}, cons)
        view, why = verdict(cons, opposite, claude)
    except Exception as e:
        logger.warning("second look failed for %s %s: %s", side, symbol, e)
        return {"view": "agree", "why": f"second look unavailable ({e.__class__.__name__})", "x": 1.0, "concerns": [], "claude": None, "features": None}
    x = DOUBT_X if (view == "doubt" and MODE == "veto") else 1.0
    return {"view": view, "why": why, "x": x, "concerns": cons, "claude": claude, "features": f}


def audit(closed: list[dict], skipped: list[dict]) -> dict:
    """What each view went on to earn, from the copies that carried a read, plus the vetoed skips' outcomes."""
    by = {v: {"n": 0, "pnl": 0.0, "wins": 0} for v in VIEWS}
    for t in closed:
        v = t.get("second_look_view")
        if v in by:
            by[v]["n"] += 1
            by[v]["pnl"] += float(t.get("pnl_usd") or 0.0)
            by[v]["wins"] += 1 if (t.get("pnl_usd") or 0) > 0 else 0
    vetoed = [e for e in skipped if str(e.get("reason", "")).startswith("second look")]
    judged = [e for e in vetoed if e.get("outcome_pct") is not None]
    return {"mode": MODE, "claude": CLAUDE_ON, "doubt_x": DOUBT_X,
            "by_view": {v: {**d, "pnl": round(d["pnl"], 1)} for v, d in by.items()},
            "vetoed": {"n": len(vetoed), "judged": len(judged),
                       "avg_outcome_pct": round(sum(e["outcome_pct"] for e in judged) / len(judged), 3) if judged else None,
                       "would_have_won_pct": round(100 * sum(e["outcome_pct"] > 0 for e in judged) / len(judged), 1) if judged else None}}
