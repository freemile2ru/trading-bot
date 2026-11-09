"""Auction-level acceptance system, ported from a thinkScript study (NQ futures, 5-minute chart) — the trading logic only.

The study (a mentor's "NQESv1"): the market accepts beyond a reference level, confirms it on the next close, and is
traded toward the next reference level with the stop under the acceptance candle. Everything is decided on CLOSED
5-minute bars; the higher-timeframe context uses the last closed 15- and 30-minute candles. The chart's labels,
bubbles and colours are not ported; the decisions behind them are.

Levels (New York time): PDH/PDL = the prior session's high/low (09:30–17:00); ONH/ONL = the overnight range
(18:00–09:30), frozen at the open; PWH/PWL = last week's high/low; ORB = the first 30 minutes (09:30–10:00); IB = the
first hour (09:30–10:30) with 0.5×, 1× and 2× range extensions. Beyond those a round number (PSYCH_STEP) is the target.

Context: 15m/30m "master" bias (EMA50 slope + separation from the session VWAP, the HTF candle not indecisive), a 5m
bias (price vs EMA50/VWAP/EMA200; EMA9/20 health), momentum (15m close above the prior 15m high), volume pressure
(bar volume vs its 20-bar average, signed by the bar's direction), ATR regime, live divergence/pullback vs the master.

Trade: acceptance = a close beyond a level by BUFFER_BPS with the prior close not beyond it, during the NY session;
confirmation = the next close still beyond the level; entry = the open of the bar after confirmation; stop = the
acceptance bar's low/high ± STOP_OFFSET_BPS; target = the next level (frozen at acceptance); exit = a close back through
the level, the stop, the target, or the session's end. STAND DOWN at confirmation (master bias not aligned, live
divergence, or no 5m bias) means no trade. The trade carries a grade (0–8 alignment points) frozen at confirmation.

Units: the original's point buffers (2 pts, 10 ticks) are tiny fractions of NQ's price, so they are expressed here in
basis points of price and scale to any instrument (QQQ, NVDA, TSLA perps all work).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ET = ZoneInfo("America/New_York")

# ── parameters (the study's inputs, in price-relative units) ──────────────────────────────────────────────────────
EMA_LEN = 50
COMPRESSION = 0.15               # mfCompressionThreshold: |EMA50 − VWAP| / ATR below this is compression
PRICE_COMPRESSION = 0.5          # mfPriceCompressionThreshold: |close − VWAP| / ATR
INDECISION_BODY = 0.35           # 30m body / range below this: indecisive HTF candle
BUFFER_BPS = 1.0                 # minAcceptanceBuffer 2 NQ pts ≈ 1 bp of price
STOP_OFFSET_BPS = 1.0            # 10 ticks ≈ 2.5 NQ pts ≈ 1 bp
PSYCH_STEP_PCT = 0.4             # 100 NQ pts ≈ 0.4% of price: the round-number target when no level is above/below
VOL_STRONG = 40                  # vpStrongPressure / vpTrapPressure, % of the 20-bar average volume
MOM_STABILITY_BARS = 3
ATR_LEN = 14
ATR_LOW = 0.8                    # ATR below 0.8 × its 50-bar average: low volatility (a reduce-risk condition)
SESSION_OPEN, SESSION_END = (9, 30), (17, 0)
ORB_END, IB_END = (10, 0), (10, 30)
OVERNIGHT_START = (18, 0)
FLAT_AT_SESSION_END = True
MIN_GRADE = 0                    # grade below this at confirmation → no trade (0 = take every non-stand-down setup)

UP_LEVELS = ("PDH", "ONH", "WKH", "ORBH", "IBH")
DOWN_LEVELS = ("PDL", "ONL", "WKL", "ORBL", "IBL")


@dataclass
class Trade:
    symbol: str
    direction: str
    level_name: str
    level: float
    accept_ts: pd.Timestamp
    confirm_ts: pd.Timestamp
    entry_ts: pd.Timestamp
    entry: float
    stop: float
    target: float
    target_name: str
    grade: int
    context: dict = field(default_factory=dict)
    exit_ts: pd.Timestamp | None = None
    exit: float | None = None
    exit_reason: str | None = None

    @property
    def risk(self) -> float:
        return abs(self.entry - self.stop)

    @property
    def pnl_pct(self) -> float:
        if self.exit is None:
            return 0.0
        s = 1 if self.direction == "long" else -1
        return (self.exit / self.entry - 1) * s * 100

    @property
    def r(self) -> float | None:
        if self.exit is None or self.risk <= 0:
            return None
        s = 1 if self.direction == "long" else -1
        return (self.exit - self.entry) * s / self.risk

    def row(self) -> dict:
        return {"symbol": self.symbol, "direction": self.direction, "level": self.level_name, "level_px": self.level,
                "accept_ts": self.accept_ts, "entry_ts": self.entry_ts, "entry": self.entry, "stop": self.stop, "target": self.target,
                "target_name": self.target_name, "grade": self.grade, "exit_ts": self.exit_ts, "exit": self.exit, "exit_reason": self.exit_reason,
                "pnl_pct": round(self.pnl_pct, 4), "r": round(self.r, 3) if self.r is not None else None,
                "risk_pct": round(self.risk / self.entry * 100, 4) if self.entry else None, **self.context}


# ── helpers ──────────────────────────────────────────────────────────────────────────────────────────────────────
def _hm(ts: pd.Timestamp) -> tuple[int, int]:
    return ts.hour, ts.minute


def _ema(x: pd.Series, n: int) -> pd.Series:
    return x.ewm(span=n, adjust=False).mean()


def _atr(h: pd.Series, l: pd.Series, c: pd.Series, n: int) -> pd.Series:
    prev = c.shift(1)
    tr = pd.concat([h - l, (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def _psych(price: float, above: bool) -> float:
    step = max(round(price * PSYCH_STEP_PCT / 100, 2), 0.01)
    return (np.floor(price / step) + 1) * step if above else np.floor(price / step) * step


def _htf(df: pd.DataFrame, rule: str, atr_len: int) -> pd.DataFrame:
    """Closed higher-timeframe candles with EMA50, EMA200 and ATR; indexed by the candle's END time."""
    k = df[["open", "high", "low", "close"]].resample(rule, label="right", closed="left").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    k["ema50"] = _ema(k["close"], EMA_LEN)
    k["ema200"] = _ema(k["close"], 200)
    k["atr"] = _atr(k["high"], k["low"], k["close"], atr_len)
    k["slope"] = k["ema50"].diff()
    return k


def session_frame(df5: pd.DataFrame, session_end: tuple[int, int] | None = None) -> pd.DataFrame:
    """Everything the decisions need, per closed 5-minute bar. `df5` has UTC-indexed open/high/low/close/volume.
    `session_end` overrides the study's 17:00 (NQ futures): the book passes the cash close for stock and ETF perps."""
    d = df5.copy()
    d = d[~d.index.duplicated()].sort_index()
    et = d.index.tz_convert(ET)
    d["et"] = et
    hm = np.array([h * 60 + m for h, m in zip(et.hour, et.minute)])
    so, se, oe, ie, os_ = [h * 60 + m for h, m in (SESSION_OPEN, session_end or SESSION_END, ORB_END, IB_END, OVERNIGHT_START)]
    d["in_session"] = (hm >= so) & (hm < se)
    d["in_orb"] = (hm >= so) & (hm < oe)
    d["in_ib"] = (hm >= so) & (hm < ie)
    d["orb_valid"] = d["in_session"] & (hm >= oe)
    d["ib_valid"] = d["in_session"] & (hm >= ie)
    d["overnight"] = (hm >= os_) | (hm < so)
    # Trading day: the 18:00 ET boundary (the futures session) — VWAP and the overnight range reset there.
    tday = pd.Series(et.date, index=d.index)
    tday = tday.where(hm < os_, tday + pd.Timedelta(days=1))
    d["tday"] = tday.astype(str)
    tp = (d["high"] + d["low"] + d["close"]) / 3
    pv = (tp * d["volume"]).groupby(d["tday"]).cumsum()
    vv = d["volume"].groupby(d["tday"]).cumsum()
    d["vwap"] = (pv / vv.replace(0, np.nan)).ffill().fillna(d["close"])
    # 5m indicators
    for n in (9, 20, 50, 200):
        d[f"ema{n}"] = _ema(d["close"], n)
    d["atr"] = _atr(d["high"], d["low"], d["close"], ATR_LEN)
    d["atr_avg"] = d["atr"].rolling(50).mean()
    vol_avg = d["volume"].rolling(20).mean()
    sign = np.sign(d["close"] - d["open"])
    d["vol_pressure"] = (100 * sign * d["volume"] / vol_avg.replace(0, np.nan)).fillna(0)
    # Higher timeframes, aligned to the last CLOSED candle at each 5m close.
    for rule, name, atr_len in (("15min", "15", ATR_LEN * 3), ("30min", "30", ATR_LEN * 6)):
        k = _htf(d, rule, atr_len)
        aligned = k.reindex(d.index, method="ffill")           # a candle counts once its end time ≤ this bar's close
        # the bar's own close time is index + 5min; use index so a candle ending exactly now is included on the next bar
        for col in ("open", "high", "low", "close", "ema50", "ema200", "atr", "slope"):
            d[f"{name}_{col}"] = aligned[col].values
        prev = k[["high", "low"]].shift(1).reindex(d.index, method="ffill")
        d[f"{name}_prev_high"], d[f"{name}_prev_low"] = prev["high"].values, prev["low"].values
    # Reference levels
    sess = d["in_session"]
    sday = pd.Series(np.where(sess, pd.Series(et.date, index=d.index).astype(str), None), index=d.index)
    rth_hi = d["high"].where(sess).groupby(sday).cummax()
    rth_lo = d["low"].where(sess).groupby(sday).cummin()
    day_hi = d["high"].where(sess).groupby(sday).max()
    day_lo = d["low"].where(sess).groupby(sday).min()
    days = sorted(k for k in day_hi.index if k)
    prior_hi = {days[i]: day_hi[days[i - 1]] for i in range(1, len(days))}
    prior_lo = {days[i]: day_lo[days[i - 1]] for i in range(1, len(days))}
    cur_day = pd.Series(et.date, index=d.index).astype(str)
    d["PDH"] = cur_day.map(prior_hi).astype(float)
    d["PDL"] = cur_day.map(prior_lo).astype(float)
    on_hi = d["high"].where(d["overnight"]).groupby(d["tday"]).max()
    on_lo = d["low"].where(d["overnight"]).groupby(d["tday"]).min()
    d["ONH"] = d["tday"].map(on_hi).astype(float)
    d["ONL"] = d["tday"].map(on_lo).astype(float)
    # last week's range (Monday–Sunday, New York time), known for the whole of this week
    wk = pd.Series(et.tz_localize(None).to_period("W-SUN").astype(str), index=d.index)
    wk_hi, wk_lo = d["high"].groupby(wk).max(), d["low"].groupby(wk).min()
    weeks = list(wk_hi.index)
    d["WKH"] = wk.map({weeks[i]: wk_hi[weeks[i - 1]] for i in range(1, len(weeks))}).astype(float)
    d["WKL"] = wk.map({weeks[i]: wk_lo[weeks[i - 1]] for i in range(1, len(weeks))}).astype(float)
    orb_hi = d["high"].where(d["in_orb"]).groupby(sday).cummax().groupby(sday).ffill()
    orb_lo = d["low"].where(d["in_orb"]).groupby(sday).cummin().groupby(sday).ffill()
    d["ORBH"], d["ORBL"] = orb_hi.where(d["orb_valid"]), orb_lo.where(d["orb_valid"])
    ib_hi = d["high"].where(d["in_ib"]).groupby(sday).cummax().groupby(sday).ffill()
    ib_lo = d["low"].where(d["in_ib"]).groupby(sday).cummin().groupby(sday).ffill()
    d["IBH"], d["IBL"] = ib_hi.where(d["ib_valid"]), ib_lo.where(d["ib_valid"])
    rng = d["IBH"] - d["IBL"]
    d["IBH05"], d["IBH1"], d["IBH2"] = d["IBH"] + rng * 0.5, d["IBH"] + rng, d["IBH"] + 2 * rng
    d["IBL05"], d["IBL1"], d["IBL2"] = d["IBL"] - rng * 0.5, d["IBL"] - rng, d["IBL"] - 2 * rng
    d["rth_hi"], d["rth_lo"] = rth_hi, rth_lo
    return d


def context(d: pd.DataFrame, i: int) -> dict:
    """The study's bias, momentum, pressure and regime readings at bar i (a closed bar)."""
    r = d.iloc[i]
    out: dict = {}
    # HTF master bias on the last closed 15m / 30m candles
    trend = {}
    for tf in ("15", "30"):
        c, e, atr, slope = r[f"{tf}_close"], r[f"{tf}_ema50"], r[f"{tf}_atr"], r[f"{tf}_slope"]
        if not (np.isfinite(c) and np.isfinite(e) and np.isfinite(atr) and atr > 0 and np.isfinite(slope)):
            trend[tf] = 0
            continue
        expanding = abs(e - r["vwap"]) / atr > COMPRESSION or abs(c - r["vwap"]) / atr > PRICE_COMPRESSION
        up = expanding and slope > 0 and c > e and c > r["vwap"]
        dn = expanding and slope < 0 and c < e and c < r["vwap"]
        trend[tf] = 1 if up else -1 if dn else 0
    body30, range30 = abs(r["30_close"] - r["30_open"]), r["30_high"] - r["30_low"]
    indecision30 = bool(range30 > 0 and body30 / range30 < INDECISION_BODY)
    master = 1 if trend["15"] == 1 and trend["30"] == 1 else -1 if trend["15"] == -1 and trend["30"] == -1 else 0
    out.update(trend15=trend["15"], trend30=trend["30"], indecision30=indecision30, master=master)
    # 5m bias
    c = r["close"]
    long_base = c > r["ema50"] and c > r["vwap"]
    short_base = c < r["ema50"] and c < r["vwap"]
    bias = 1 if long_base else -1 if short_base else 0
    out["bias"] = bias
    out["bias_kind"] = ("cont" if (c > r["ema200"]) == (bias == 1) else "scalp") if bias else "none"
    out["healthy"] = bool((bias == 1 and c > r["ema9"] and c > r["ema20"]) or (bias == -1 and c < r["ema9"] and c < r["ema20"]))
    out["lost9"] = bool((bias == 1 and c < r["ema9"]) or (bias == -1 and c > r["ema9"]))
    # live divergence / pullback / caution
    live15 = 1 if (c > r["vwap"] and c > r["15_ema50"]) else -1 if (c < r["vwap"] and c < r["15_ema50"]) else 0
    live30 = 1 if (c > r["vwap"] and c > r["30_ema50"]) else -1 if (c < r["vwap"] and c < r["30_ema50"]) else 0
    live = 1 if live15 == 1 and live30 == 1 else -1 if live15 == -1 and live30 == -1 else 0
    out["divergence"] = bool(master != 0 and live != 0 and live != master)
    out["pullback"] = bool(master != 0 and live == 0)
    out["caution"] = bool((master == 1 and r["ema9"] < r["ema20"]) or (master == -1 and r["ema9"] > r["ema20"]))
    # momentum: 15m close beyond the prior 15m extreme, in the master's direction
    out["reaccel"] = bool((master == 1 and r["15_close"] > r["15_prev_high"]) or (master == -1 and r["15_close"] < r["15_prev_low"]))
    # volume pressure and ATR regime
    vp = r["vol_pressure"]
    out["vol_pressure"] = float(vp)
    out["atr_low"] = bool(np.isfinite(r["atr_avg"]) and r["atr"] < ATR_LOW * r["atr_avg"])
    return out


def grade(ctx: dict, direction: int) -> tuple[int, bool, list[str]]:
    """The trade light at confirmation: (alignment score 0–8, stand down?, reasons)."""
    core = ctx["master"] == direction
    mom_ok = ctx["master"] != 0 and core
    vol_ok = not ((direction == 1 and ctx["vol_pressure"] <= -VOL_STRONG) or (direction == -1 and ctx["vol_pressure"] >= VOL_STRONG))
    bias_ok = ctx["bias"] == direction
    score = sum([core, mom_ok, not ctx["caution"], not ctx["divergence"], vol_ok, bias_ok, not ctx["indecision30"], not ctx["atr_low"]])
    reasons = []
    if not core:
        reasons.append("HTF")
    if ctx["divergence"]:
        reasons.append("DIV")
    if ctx["bias"] == 0:
        reasons.append("5M")
    return int(score), bool(reasons), reasons


def next_levels(r: pd.Series) -> tuple[tuple[float, str], tuple[float, str]]:
    """(next level above, name), (next level below, name) from the bar's close, the study's candidate set."""
    c = r["close"]
    names = ["PDH", "ONH", "WKH", "WKL", "PDL", "ONL", "ORBH", "IBH", "IBH05", "IBH1", "IBH2", "ORBL", "IBL", "IBL05", "IBL1", "IBL2"]
    above = [(r[n], n) for n in names if np.isfinite(r[n]) and r[n] > c]
    below = [(r[n], n) for n in names if np.isfinite(r[n]) and r[n] < c]
    up = min(above) if above else (_psych(c, True), "PSYCH")
    dn = max(below) if below else (_psych(c, False), "PSYCH")
    return up, dn


RETEST_BARS = 3                  # the study's retest window after confirmation


def second_levels(r: pd.Series) -> tuple[tuple[float, str], tuple[float, str]]:
    """The level BEYOND the nearest one each way — the nearest is often inside the costs; a round number past it if none."""
    c = r["close"]
    names = ["PDH", "ONH", "WKH", "WKL", "PDL", "ONL", "ORBH", "IBH", "IBH05", "IBH1", "IBH2", "ORBL", "IBL", "IBL05", "IBL1", "IBL2"]
    above = sorted((r[n], n) for n in names if np.isfinite(r[n]) and r[n] > c)
    below = sorted(((r[n], n) for n in names if np.isfinite(r[n]) and r[n] < c), reverse=True)
    up = above[1] if len(above) > 1 else (_psych(above[0][0] if above else c, True), "PSYCH")
    dn = below[1] if len(below) > 1 else (_psych(below[0][0] if below else c, False), "PSYCH")
    return up, dn


def simulate(df5: pd.DataFrame, symbol: str, min_grade: int = MIN_GRADE, flat_at_end: bool = FLAT_AT_SESSION_END,
             min_target_bps: float = 0.0, cont_only: bool = False, min_rr: float = 0.0,
             entry: str = "next_open", target_mode: str = "next", trail: bool = False, max_from_open_pct: float | None = None) -> list[Trade]:
    """Walk the closed bars, one trade at a time, exactly as the study sequences acceptance → confirmation → entry → exit.

    The replay found the study's targets sit a dozen basis points away on index perps — inside the costs. The filters
    (2026-10-31, "build around the weaknesses"): `min_target_bps` skips a setup whose next level is closer than that;
    `cont_only` takes only continuation setups (price on the EMA200's side of the trade); `min_rr` needs target distance
    ≥ that multiple of the risk.

    The mentor's reading of his own study (2026-11-01): "wait for confirmation, then the best entry and exit".
      entry="retest"      after confirmation, wait up to RETEST_BARS bars for price to come back and touch the level and
                          close beyond it again (the study's retest, cyan candle) — enter at that close, stop under the
                          retest bar; no retest within the window → no trade
      target_mode="second" aim at the level beyond the nearest one (the nearest is often inside the costs)
      trail=True          once the trade is +1R, the stop follows the 5m EMA20 (closes through it exit the trade)
      max_from_open_pct   the live book's chase gate (experiments/auction, 2026-11-05): no acceptance once price is this far
                          from the session's open in the trade's direction; None takes every acceptance"""
    d = session_frame(df5)
    session_open = d["open"].where(d["in_session"]).groupby(d["tday"]).transform("first")
    trades: list[Trade] = []
    pending: dict | None = None            # accepted, waiting for the confirmation bar
    entering: dict | None = None           # confirmed, entering at the next bar's open
    open_trade: Trade | None = None
    n = len(d)
    closes = d["close"].values
    for i in range(201, n):
        r = d.iloc[i]
        ts = d.index[i]
        if open_trade is not None:
            t = open_trade
            s = 1 if t.direction == "long" else -1
            if trail:
                gain = (r["close"] - t.entry) * s
                if gain >= t.risk and not t.context.get("trailing"):
                    t.context["trailing"] = True
                if t.context.get("trailing"):
                    ema = float(r["ema20"])
                    if (s > 0 and ema > t.stop) or (s < 0 and ema < t.stop):
                        t.stop = ema                                        # the stop only ever moves in the trade's favour
            hit_stop = r["low"] <= t.stop if s > 0 else r["high"] >= t.stop
            hit_target = r["high"] >= t.target if s > 0 else r["low"] <= t.target
            through = (r["close"] < t.level) if s > 0 else (r["close"] > t.level)
            if hit_stop:
                t.exit, t.exit_reason = (min(t.stop, r["open"]) if s > 0 else max(t.stop, r["open"])), ("trail" if t.context.get("trailing") else "stop")
            elif hit_target:
                t.exit, t.exit_reason = t.target, "target"
            elif through:
                t.exit, t.exit_reason = r["close"], "exit_through_level"
            elif flat_at_end and (not r["in_session"] or i == n - 1):
                t.exit, t.exit_reason = r["close"], "session_end"
            if t.exit is not None:
                t.exit_ts = ts
                trades.append(t)
                open_trade = None
        if entering is not None and open_trade is None:
            e = entering
            px, stop = None, e["stop"]
            if entry == "retest":
                e["waited"] = e.get("waited", 0) + 1
                s_ = 1 if e["direction"] == "long" else -1
                touched = r["low"] <= e["level"] if s_ > 0 else r["high"] >= e["level"]
                held = r["close"] > e["level"] + r["close"] * BUFFER_BPS / 1e4 if s_ > 0 else r["close"] < e["level"] - r["close"] * BUFFER_BPS / 1e4
                if touched and held:
                    px = float(r["close"])
                    off = r["close"] * STOP_OFFSET_BPS / 1e4
                    stop = float(r["low"] - off) if s_ > 0 else float(r["high"] + off)
                elif e["waited"] >= RETEST_BARS or not r["in_session"]:
                    entering = None                                      # no retest came: the study's "best entry" never showed
                    continue
            else:
                px = float(r["open"])
            if px is not None:
                entering = None
                if r["in_session"]:
                    t = Trade(symbol=symbol, direction=e["direction"], level_name=e["level_name"], level=e["level"], accept_ts=e["accept_ts"],
                              confirm_ts=e["confirm_ts"], entry_ts=ts, entry=px, stop=stop, target=e["target"],
                              target_name=e["target_name"], grade=e["grade"], context={**e["context"], "entry_mode": entry, "trailing": False})
                    if (t.direction == "long" and t.stop < t.entry < t.target) or (t.direction == "short" and t.target < t.entry < t.stop):
                        open_trade = t
        if pending is not None:
            p = pending
            pending = None
            held = r["close"] > p["level"] if p["direction"] == "long" else r["close"] < p["level"]
            if held and r["in_session"]:
                ctx = context(d, i)
                g, stand_down, reasons = grade(ctx, 1 if p["direction"] == "long" else -1)
                target_bps = abs(p["target"] / r["close"] - 1) * 1e4
                risk = abs(r["close"] - p["stop"])
                rr = abs(p["target"] - r["close"]) / risk if risk > 0 else 0.0
                ok = (not stand_down and g >= min_grade and target_bps >= min_target_bps and rr >= min_rr
                      and (not cont_only or ctx["bias_kind"] == "cont"))
                if ok:
                    entering = {**p, "confirm_ts": ts, "grade": g, "context": {**ctx, "stand_down_reasons": reasons, "target_bps": round(target_bps, 1), "rr": round(rr, 2)}}
        if not r["in_session"] or open_trade is not None or entering is not None:
            continue
        # acceptance on this closed bar
        buf = r["close"] * BUFFER_BPS / 1e4
        prev_c = closes[i - 1]
        ups = [(r[n], n) for n in UP_LEVELS if np.isfinite(r[n]) and r["close"] > r[n] + buf and prev_c <= r[n] + buf]
        dns = [(r[n], n) for n in DOWN_LEVELS if np.isfinite(r[n]) and r["close"] < r[n] - buf and prev_c >= r[n] - buf]
        if not ups and not dns:
            continue
        if max_from_open_pct is not None:
            so = session_open.iloc[i]
            sd = 1 if ups else -1
            if np.isfinite(so) and so > 0 and (float(r["close"]) / so - 1) * 100 * sd > max_from_open_pct:
                continue                                                 # the chase gate: the move has already run from the open
        up_lv, dn_lv = next_levels(r) if target_mode == "next" else second_levels(r)
        off = r["close"] * STOP_OFFSET_BPS / 1e4
        if ups:
            level, name = max(ups)
            pending = {"direction": "long", "level": float(level), "level_name": name, "accept_ts": ts, "stop": float(r["low"] - off),
                       "target": float(up_lv[0]), "target_name": up_lv[1]}
        else:
            level, name = min(dns)
            pending = {"direction": "short", "level": float(level), "level_name": name, "accept_ts": ts, "stop": float(r["high"] + off),
                       "target": float(dn_lv[0]), "target_name": dn_lv[1]}
    return trades


# ── Trend day (2026-11-09) ───────────────────────────────────────────────────────────────────────────────────────────
# Registered after a gap-and-go session (AMD +4%, TSLA +5%, NVDA +3% by mid-morning) on which the study's sequence took
# nothing: the retest never came and the chase gate refused every opening-range break. The study's own context already
# names such a day — master bias aligned, 5m bias aligned, no divergence — so this arrangement trades it the only way a
# trend day can be entered without chasing: on the pullback. Once the initial balance has closed, a close beyond it by
# TREND_EXT × its range with that context ARMS the day in that direction; the entry is the first bar whose low (high)
# comes within TREND_PULLBACK_ATR ATRs of the 5m EMA20 and closes back beyond it in the trend's direction; the stop is
# under that bar, at least TREND_MIN_STOP_ATR ATRs away; the target is the far extension (IB + 2× range) and must be at
# least min_target_bps away; a close back through the session VWAP before the entry disarms the day (it may re-arm), and
# after the entry it exits the trade. One trade a symbol a day. Everything else (fills at the close, the book's cutoff,
# the costs) is the forward shadow's. Verdict: the same 40 trades as the other arrangements.
TREND_EXT = 0.5
TREND_PULLBACK_ATR = 0.5
TREND_MIN_STOP_ATR = 1.0


def simulate_trend_day(df5: pd.DataFrame, symbol: str, min_target_bps: float = 0.0, flat_at_end: bool = FLAT_AT_SESSION_END) -> list[Trade]:
    """The trend-day arrangement above, on closed 5-minute bars. Trades carry level_name IBH05/IBL05 (the arming level),
    target_name IBH2/IBL2, exit_reason stop | target | vwap_lost | session_end, and context entry_mode="trend_day"."""
    d = session_frame(df5)
    trades: list[Trade] = []
    open_trade: Trade | None = None
    armed: dict | None = None
    traded_days: set[str] = set()
    n = len(d)
    for i in range(201, n):
        r = d.iloc[i]
        ts = d.index[i]
        day = r["tday"]
        if open_trade is not None:
            t = open_trade
            s = 1 if t.direction == "long" else -1
            hit_stop = r["low"] <= t.stop if s > 0 else r["high"] >= t.stop
            hit_target = r["high"] >= t.target if s > 0 else r["low"] <= t.target
            lost = (r["close"] < r["vwap"]) if s > 0 else (r["close"] > r["vwap"])
            if hit_stop:
                t.exit, t.exit_reason = (min(t.stop, r["open"]) if s > 0 else max(t.stop, r["open"])), "stop"
            elif hit_target:
                t.exit, t.exit_reason = t.target, "target"
            elif lost:
                t.exit, t.exit_reason = float(r["close"]), "vwap_lost"
            elif flat_at_end and (not r["in_session"] or i == n - 1):
                t.exit, t.exit_reason = float(r["close"]), "session_end"
            if t.exit is not None:
                t.exit_ts = ts
                trades.append(t)
                open_trade = None
            continue
        if armed is not None and armed["day"] != day:
            armed = None
        if not r["in_session"] or not r["ib_valid"] or day in traded_days:
            continue
        rng = float(r["IBH"] - r["IBL"]) if np.isfinite(r["IBH"]) and np.isfinite(r["IBL"]) else 0.0
        atr = float(r["atr"]) if np.isfinite(r["atr"]) else 0.0
        if rng <= 0 or atr <= 0:
            continue
        if armed is None:
            c = float(r["close"])
            if c >= r["IBH"] + TREND_EXT * rng or c <= r["IBL"] - TREND_EXT * rng:
                s = 1 if c >= r["IBH"] + TREND_EXT * rng else -1
                ctx = context(d, i)
                if ctx["master"] == s and ctx["bias"] == s and not ctx["divergence"]:
                    armed = {"day": day, "direction": s, "arm_ts": ts, "level": float(r["IBH"] + TREND_EXT * rng) if s > 0 else float(r["IBL"] - TREND_EXT * rng)}
            continue                                                     # the arming bar is never the entry
        s = armed["direction"]
        if (r["close"] < r["vwap"]) if s > 0 else (r["close"] > r["vwap"]):
            armed = None                                                 # the trend broke before an entry: disarm, it may re-arm
            continue
        ema = float(r["ema20"])
        touched = r["low"] <= ema + TREND_PULLBACK_ATR * atr if s > 0 else r["high"] >= ema - TREND_PULLBACK_ATR * atr
        held = (r["close"] > ema and r["close"] > r["open"]) if s > 0 else (r["close"] < ema and r["close"] < r["open"])
        if not (touched and held):
            continue
        px = float(r["close"])
        off = px * STOP_OFFSET_BPS / 1e4
        stop = float(r["low"] - off) if s > 0 else float(r["high"] + off)
        if abs(px - stop) < TREND_MIN_STOP_ATR * atr:
            stop = px - s * TREND_MIN_STOP_ATR * atr
        target = float(r["IBH2"]) if s > 0 else float(r["IBL2"])
        target_bps = (target / px - 1) * 1e4 * s
        if not np.isfinite(target) or target_bps < max(min_target_bps, 0.0) or target_bps <= 0:
            traded_days.add(day)                                         # the move has already run to the far extension: no trade today
            armed = None
            continue
        ctx = context(d, i)
        g, _, reasons = grade(ctx, s)
        t = Trade(symbol=symbol, direction="long" if s > 0 else "short", level_name="IBH05" if s > 0 else "IBL05", level=armed["level"],
                  accept_ts=armed["arm_ts"], confirm_ts=armed["arm_ts"], entry_ts=ts, entry=px, stop=stop, target=target,
                  target_name="IBH2" if s > 0 else "IBL2", grade=g,
                  context={**ctx, "stand_down_reasons": reasons, "entry_mode": "trend_day", "trailing": False, "target_bps": round(target_bps, 1),
                           "rr": round(abs(target - px) / abs(px - stop), 2) if px != stop else None})
        open_trade = t
        traded_days.add(day)
        armed = None
    return trades


def summarize(trades: list[Trade], cost_bps: float = 8.0) -> dict:
    """Per-trade % after costs (taker in and out), win rate, average R, both halves, by grade and by level."""
    if not trades:
        return {"trades": 0}
    rows = pd.DataFrame([t.row() for t in trades])
    rows["net_pct"] = rows["pnl_pct"] - 2 * cost_bps / 100
    rows["net_r"] = rows["net_pct"] / rows["risk_pct"]
    half = len(rows) // 2

    def block(x: pd.DataFrame) -> dict:
        return {"n": int(len(x)), "win_pct": round(float((x["net_pct"] > 0).mean() * 100), 1) if len(x) else None,
                "avg_net_pct": round(float(x["net_pct"].mean()), 3) if len(x) else None, "avg_r": round(float(x["net_r"].mean()), 3) if len(x) else None,
                "sum_net_pct": round(float(x["net_pct"].sum()), 2) if len(x) else None}
    return {"trades": int(len(rows)), "all": block(rows), "first_half": block(rows.iloc[:half]), "second_half": block(rows.iloc[half:]),
            "by_grade": {int(g): block(x) for g, x in rows.groupby("grade")},
            "by_level": {str(l): block(x) for l, x in rows.groupby("level")},
            "by_exit": {str(e): block(x) for e, x in rows.groupby("exit_reason")},
            "by_direction": {str(e): block(x) for e, x in rows.groupby("direction")},
            "avg_risk_pct": round(float(rows["risk_pct"].mean()), 3), "avg_hold_bars": None}
