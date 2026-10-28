"""Technical analysis engine — computes all indicators from OHLCV candles."""

import pandas as pd
import numpy as np


def ohlcv_to_df(candles: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(candles)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    df = df.set_index("open_time").sort_index()
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    return df


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period).mean()


def macd(close: pd.Series) -> dict:
    fast = ema(close, 12)
    slow = ema(close, 26)
    line = fast - slow
    signal_line = ema(line, 9)
    histogram = line - signal_line

    hist_val = float(histogram.iloc[-1])
    hist_roc = 0.0
    hist_expanding = False
    clean_hist = histogram.dropna()
    if len(clean_hist) >= 4:
        h = clean_hist.iloc[-4:]
        # Per-bar rate of change over last 3 bars
        hist_roc = float(h.iloc[-1] - h.iloc[-3]) / 2
        # Expanding: same sign and growing in magnitude for 2 consecutive bars
        # bool() is required — chained numpy scalar comparisons return numpy.bool_
        # which FastAPI's jsonable_encoder cannot serialize.
        hist_expanding = bool(
            (h.iloc[-1] > 0 and h.iloc[-1] > h.iloc[-2] > h.iloc[-3]) or
            (h.iloc[-1] < 0 and h.iloc[-1] < h.iloc[-2] < h.iloc[-3])
        )

    return {
        "macd_line": float(line.iloc[-1]),
        "signal_line": float(signal_line.iloc[-1]),
        "histogram": hist_val,
        "histogram_roc": round(hist_roc, 6),
        "histogram_expanding": hist_expanding,
        "cross": _macd_cross(line, signal_line),
    }


def _macd_cross(macd_line: pd.Series, signal: pd.Series) -> str:
    if len(macd_line) < 2:
        return "none"
    prev_diff = macd_line.iloc[-2] - signal.iloc[-2]
    curr_diff = macd_line.iloc[-1] - signal.iloc[-1]
    if prev_diff < 0 and curr_diff >= 0:
        return "bullish_cross"
    if prev_diff > 0 and curr_diff <= 0:
        return "bearish_cross"
    return "none"


def _rsi_series(close: pd.Series, period: int = 14) -> pd.Series:
    """Full RSI series — needed for divergence detection."""
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    # A window with no down-close is RSI 100 (and a flat one 50), not NaN: NaN here flowed into the
    # score as a +1 slope (min(1.0, nan) is 1.0) and broke JSON responses carrying the reading.
    no_loss = (loss == 0) & gain.notna()
    return out.mask(no_loss & (gain > 0), 100.0).mask(no_loss & (gain == 0), 50.0)


def rsi(close: pd.Series, period: int = 14) -> float:
    return float(_rsi_series(close, period).iloc[-1])


def rsi_divergence(close: pd.Series, rsi_s: pd.Series, lookback: int = 30) -> dict:
    """Detect bullish/bearish RSI divergence by comparing recent vs earlier extremes."""
    valid_mask = rsi_s.notna()
    close_v = close[valid_mask]
    rsi_v = rsi_s[valid_mask]

    if len(close_v) < lookback + 5:
        return {"bullish": False, "bearish": False, "type": "none"}

    recent_c = close_v.iloc[-10:].values
    earlier_c = close_v.iloc[-lookback:-10].values
    recent_r = rsi_v.iloc[-10:].values
    earlier_r = rsi_v.iloc[-lookback:-10].values

    if len(earlier_c) < 3 or len(recent_c) < 3:
        return {"bullish": False, "bearish": False, "type": "none"}

    r_low = recent_c.argmin()
    e_low = earlier_c.argmin()
    bullish = (float(recent_c[r_low]) < float(earlier_c[e_low]) and
               float(recent_r[r_low]) > float(earlier_r[e_low]))

    r_high = recent_c.argmax()
    e_high = earlier_c.argmax()
    bearish = (float(recent_c[r_high]) > float(earlier_c[e_high]) and
               float(recent_r[r_high]) < float(earlier_r[e_high]))

    div_type = "bullish" if bullish else "bearish" if bearish else "none"
    return {"bullish": bullish, "bearish": bearish, "type": div_type}


def stochastic(df: pd.DataFrame, k_period: int = 14, d_period: int = 3) -> dict:
    low_min = df["low"].rolling(k_period).min()
    high_max = df["high"].rolling(k_period).max()
    k = 100 * (df["close"] - low_min) / (high_max - low_min).replace(0, np.nan)
    d = k.rolling(d_period).mean()
    k_val, d_val = float(k.iloc[-1]), float(d.iloc[-1])
    # A flat window has no range: mid-scale, not NaN.
    return {"k": 50.0 if np.isnan(k_val) else k_val, "d": 50.0 if np.isnan(d_val) else d_val}


def bollinger_bands(close: pd.Series, period: int = 20, std_dev: float = 2.0) -> dict:
    mid = sma(close, period)
    std = close.rolling(period).std()
    upper = mid + std_dev * std
    lower = mid - std_dev * std
    price = float(close.iloc[-1])
    band = float(upper.iloc[-1] - lower.iloc[-1])
    width = band / float(mid.iloc[-1]) * 100 if mid.iloc[-1] else 0.0
    pct_b = (price - float(lower.iloc[-1])) / band if band else 0.5      # zero-width band: price sits on it
    return {
        "upper": float(upper.iloc[-1]),
        "mid": float(mid.iloc[-1]),
        "lower": float(lower.iloc[-1]),
        "width_pct": width,
        "pct_b": pct_b,
        "squeeze": width < 2.0,
        "position": "overbought" if pct_b > 1 else "oversold" if pct_b < 0 else "inside",
    }


def atr(df: pd.DataFrame, period: int = 14) -> float:
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return float(tr.ewm(span=period, adjust=False).mean().iloc[-1])


def adx(df: pd.DataFrame, period: int = 14) -> dict:
    high = df["high"]
    low = df["low"]
    close = df["close"]

    plus_dm = high.diff()
    minus_dm = -low.diff()
    plus_dm[plus_dm < 0] = 0
    minus_dm[minus_dm < 0] = 0
    plus_dm[plus_dm < minus_dm] = 0
    minus_dm[minus_dm < plus_dm] = 0

    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)

    atr_s = tr.ewm(span=period, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(span=period, adjust=False).mean() / atr_s
    minus_di = 100 * minus_dm.ewm(span=period, adjust=False).mean() / atr_s
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx_val = dx.ewm(span=period, adjust=False).mean()

    v = float(adx_val.iloc[-1])
    if not np.isfinite(v):                   # a series with no range at all: no trend to measure
        return {"adx": 0.0, "adx_slope": 0.0, "trend_momentum": "flat", "plus_di": 0.0, "minus_di": 0.0,
                "trend_strength": "weak", "direction": "neutral"}

    # 3-bar slope: positive = trend strengthening, negative = trend exhausting
    clean = adx_val.dropna()
    slope = float(clean.iloc[-1] - clean.iloc[-4]) if len(clean) >= 4 else 0.0
    if slope > 2:       momentum = "strengthening"
    elif slope < -2:    momentum = "weakening"
    else:               momentum = "flat"

    return {
        "adx": v,
        "adx_slope": round(slope, 2),
        "trend_momentum": momentum,
        "plus_di": float(plus_di.iloc[-1]),
        "minus_di": float(minus_di.iloc[-1]),
        "trend_strength": "strong" if v > 25 else "weak",
        "direction": "bullish" if plus_di.iloc[-1] > minus_di.iloc[-1] else "bearish",
    }


def volume_analysis(df: pd.DataFrame) -> dict:
    vol = df["volume"]
    avg_vol_20 = float(vol.rolling(20).mean().iloc[-1])
    current_vol = float(vol.iloc[-1])
    ratio = current_vol / avg_vol_20 if avg_vol_20 else 1.0
    obv = (np.sign(df["close"].diff()) * vol).fillna(0).cumsum()
    return {
        "current_volume": current_vol,
        "avg_volume_20": avg_vol_20,
        "volume_ratio": round(ratio, 2),
        "volume_signal": "high" if ratio > 1.5 else "low" if ratio < 0.5 else "normal",
        "obv": float(obv.iloc[-1]),
        "obv_trend": "rising" if obv.iloc[-1] > obv.iloc[-5] else "falling",
    }


def vwap(df: pd.DataFrame) -> dict:
    """Volume Weighted Average Price — primary institutional reference level."""
    typical = (df["high"] + df["low"] + df["close"]) / 3
    cum_tp_vol = (typical * df["volume"]).cumsum()
    cum_vol = df["volume"].cumsum().replace(0, np.nan)
    vwap_series = cum_tp_vol / cum_vol
    price = float(df["close"].iloc[-1])
    vwap_val = float(vwap_series.iloc[-1])
    dev_pct = (price - vwap_val) / vwap_val * 100 if vwap_val else 0.0
    return {
        "vwap": round(vwap_val, 4),
        "price_above_vwap": price > vwap_val,
        "deviation_pct": round(dev_pct, 2),
    }


def chaikin_money_flow(df: pd.DataFrame, period: int = 20) -> float:
    """CMF: measures buying/selling pressure. +0.1 = mild buying, −0.1 = mild selling."""
    high = df["high"]
    low = df["low"]
    close = df["close"]
    vol = df["volume"]
    hl_range = (high - low).replace(0, np.nan)
    clv = ((close - low) - (high - close)) / hl_range
    cmf = (clv * vol).rolling(period).sum() / vol.rolling(period).sum().replace(0, np.nan)
    val = float(cmf.iloc[-1])
    return val if not np.isnan(val) else 0.0


def support_resistance(df: pd.DataFrame) -> dict:
    price = float(df["close"].iloc[-1])
    prev = df.iloc[-2]
    pivot = (float(prev["high"]) + float(prev["low"]) + float(prev["close"])) / 3
    r1 = 2 * pivot - float(prev["low"])
    r2 = pivot + (float(prev["high"]) - float(prev["low"]))
    s1 = 2 * pivot - float(prev["high"])
    s2 = pivot - (float(prev["high"]) - float(prev["low"]))
    return {
        "pivot": round(pivot, 4),
        "resistance_1": round(r1, 4),
        "resistance_2": round(r2, 4),
        "support_1": round(s1, 4),
        "support_2": round(s2, 4),
        "distance_to_r1_pct": round((r1 - price) / price * 100, 2),
        "distance_to_s1_pct": round((price - s1) / price * 100, 2),
    }


def ema_cloud(close: pd.Series) -> dict:
    e9 = ema(close, 9)
    e21 = ema(close, 21)
    e50 = ema(close, 50)
    e200 = ema(close, 200)
    price = float(close.iloc[-1])
    return {
        "ema_9": float(e9.iloc[-1]),
        "ema_21": float(e21.iloc[-1]),
        "ema_50": float(e50.iloc[-1]),
        "ema_200": float(e200.iloc[-1]),
        "price_above_200ema": price > float(e200.iloc[-1]),
        "golden_cross": float(e50.iloc[-1]) > float(e200.iloc[-1]),
        "death_cross": float(e50.iloc[-1]) < float(e200.iloc[-1]),
        "ema_alignment": _ema_alignment(price, e9.iloc[-1], e21.iloc[-1], e50.iloc[-1]),
    }


def _ema_alignment(price: float, e9: float, e21: float, e50: float) -> str:
    if price > e9 > e21 > e50:
        return "bullish_aligned"
    if price < e9 < e21 < e50:
        return "bearish_aligned"
    return "mixed"


def ichimoku(df: pd.DataFrame) -> dict:
    """Ichimoku Kinko Hyo — tenkan, kijun, cloud, TK cross."""
    h = df["high"]
    l = df["low"]
    c = df["close"]

    tenkan = (h.rolling(9).max() + l.rolling(9).min()) / 2
    kijun = (h.rolling(26).max() + l.rolling(26).min()) / 2
    span_a_series = (tenkan + kijun) / 2
    span_b_series = (h.rolling(52).max() + l.rolling(52).min()) / 2

    price = float(c.iloc[-1])
    tenkan_val = float(tenkan.iloc[-1])
    kijun_val = float(kijun.iloc[-1])

    # Cloud at current position = span_a/b from 26 bars ago (they're plotted 26 bars forward)
    cloud_idx = -27
    def _safe(series, idx, fallback):
        try:
            v = series.iloc[idx]
            return float(v) if not pd.isna(v) else fallback
        except Exception:
            return fallback

    span_a_curr = _safe(span_a_series, cloud_idx, tenkan_val)
    span_b_curr = _safe(span_b_series, cloud_idx, kijun_val)
    cloud_top = max(span_a_curr, span_b_curr)
    cloud_bottom = min(span_a_curr, span_b_curr)
    above_cloud = price > cloud_top
    below_cloud = price < cloud_bottom

    # TK cross detection
    tk_cross = "none"
    if len(tenkan) >= 2 and len(kijun) >= 2:
        prev_diff = float(tenkan.iloc[-2]) - float(kijun.iloc[-2])
        curr_diff = tenkan_val - kijun_val
        if prev_diff < 0 and curr_diff >= 0:
            tk_cross = "bullish"
        elif prev_diff > 0 and curr_diff <= 0:
            tk_cross = "bearish"

    # Future cloud color (forward-projected span_a vs span_b at current bar)
    span_a_now = float(span_a_series.iloc[-1])
    span_b_now = _safe(span_b_series, -1, span_a_now)
    cloud_color = "green" if span_a_now > span_b_now else "red"

    if above_cloud and tenkan_val >= kijun_val:
        signal = "bullish"
    elif below_cloud and tenkan_val <= kijun_val:
        signal = "bearish"
    else:
        signal = "neutral"

    return {
        "tenkan": round(tenkan_val, 4),
        "kijun": round(kijun_val, 4),
        "cloud_top": round(cloud_top, 4),
        "cloud_bottom": round(cloud_bottom, 4),
        "cloud_color": cloud_color,
        "above_cloud": above_cloud,
        "below_cloud": below_cloud,
        "in_cloud": not above_cloud and not below_cloud,
        "tk_cross": tk_cross,
        "signal": signal,
    }


def fibonacci_levels(df: pd.DataFrame, lookback: int = 50) -> dict:
    """Fibonacci retracement levels from the swing high/low of the last N bars."""
    window = df.tail(lookback)
    swing_high = float(window["high"].max())
    swing_low = float(window["low"].min())
    price = float(df["close"].iloc[-1])
    diff = swing_high - swing_low

    if diff == 0:
        return {"error": "no_price_range"}

    # Retracements run back from the end of the swing: down from the high after an
    # up-swing, up from the low after a down-swing. Always measuring from the high framed
    # every swing as a rally, so the levels of a sell-off weren't the mirror of a rally's.
    up_swing = window["low"].idxmin() <= window["high"].idxmax()
    end, sign = (swing_high, -1) if up_swing else (swing_low, 1)
    levels = {r: round(end + sign * float(r) * diff, 4) for r in ("0.0", "0.236", "0.382", "0.5", "0.618", "0.786")}
    levels["1.0"] = round(swing_low if up_swing else swing_high, 4)

    fib_values = sorted(levels.values())
    nearest_support = max((v for v in fib_values if v <= price), default=swing_low)
    nearest_resistance = min((v for v in fib_values if v >= price), default=swing_high)
    price_position = (price - swing_low) / diff

    # Count how many bars have tested (touched within 0.5%) each key level.
    # Use 2× lookback so tested levels are recognised even if formed earlier.
    touch_window = df.tail(lookback * 2)
    tol = 0.005
    sup_touches = int(
        ((touch_window["low"] >= nearest_support * (1 - tol)) &
         (touch_window["low"] <= nearest_support * (1 + tol))).sum()
    )
    res_touches = int(
        ((touch_window["high"] >= nearest_resistance * (1 - tol)) &
         (touch_window["high"] <= nearest_resistance * (1 + tol))).sum()
    )

    # In a tight range the levels sit under 1% apart, so price is often "near" both. Only
    # the closer one counts — checking support first used to turn every such tie bullish.
    d_sup = (price - nearest_support) / price
    d_res = (nearest_resistance - price) / price
    near_support = d_sup < 0.01 and d_sup < d_res
    near_resistance = d_res < 0.01 and d_res < d_sup

    return {
        "swing_high": round(swing_high, 4),
        "swing_low": round(swing_low, 4),
        "levels": levels,
        "nearest_support": round(nearest_support, 4),
        "nearest_resistance": round(nearest_resistance, 4),
        "price_position": round(price_position, 3),
        "near_support": near_support,
        "near_resistance": near_resistance,
        "nearest_support_touches": min(sup_touches, 5),
        "nearest_resistance_touches": min(res_touches, 5),
    }


def market_structure(df: pd.DataFrame, lookback: int = 60) -> dict:
    """Detect bullish (HH+HL) or bearish (LH+LL) price structure using pivot swing points.

    Pivot size of 3 means a bar must be the highest/lowest within ±3 bars to qualify.
    Score magnitude reflects how much higher/lower the new swings are relative to prior ones.
    """
    window = df.tail(lookback)
    highs = window["high"].values
    lows = window["low"].values
    n = len(highs)

    if n < 15:
        return {"structure": "unclear", "score": 0.0}

    pivot = 3
    swing_highs: list[float] = []
    swing_lows: list[float] = []

    for i in range(pivot, n - pivot):
        local_hi = max(highs[i - pivot: i + pivot + 1])
        local_lo = min(lows[i - pivot: i + pivot + 1])
        if highs[i] >= local_hi:
            swing_highs.append(float(highs[i]))
        if lows[i] <= local_lo:
            swing_lows.append(float(lows[i]))

    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return {"structure": "unclear", "score": 0.0}

    sh1, sh2 = swing_highs[-2], swing_highs[-1]
    sl1, sl2 = swing_lows[-2], swing_lows[-1]

    hh = sh2 > sh1
    hl = sl2 > sl1
    lh = sh2 < sh1
    ll = sl2 < sl1

    if hh and hl:
        structure = "bullish"
        score = min(1.0, ((sh2 - sh1) / sh1 + (sl2 - sl1) / sl1) * 10)
    elif lh and ll:
        structure = "bearish"
        score = -min(1.0, ((sh1 - sh2) / sh1 + (sl1 - sl2) / sl1) * 10)
    elif lh and hl:
        structure = "contracting"
        score = 0.0
    elif hh and ll:
        structure = "expanding"
        score = 0.0
    else:
        structure = "unclear"
        score = 0.0

    return {"structure": structure, "score": round(score, 3)}


def candlestick_patterns(df: pd.DataFrame) -> dict:
    """Detect key single and multi-candle reversal patterns."""
    if len(df) < 3:
        return {"detected": [], "signal": "neutral", "count": 0}

    o = df["open"]
    h = df["high"]
    l = df["low"]
    c = df["close"]

    c0, o0, h0, l0 = float(c.iloc[-1]), float(o.iloc[-1]), float(h.iloc[-1]), float(l.iloc[-1])
    c1, o1 = float(c.iloc[-2]), float(o.iloc[-2])
    c2, o2 = float(c.iloc[-3]), float(o.iloc[-3])

    body0 = abs(c0 - o0)
    rng0 = h0 - l0
    lower_wick = min(c0, o0) - l0
    upper_wick = h0 - max(c0, o0)

    detected = []

    if rng0 > 0 and body0 / rng0 < 0.1:
        detected.append("doji")

    if rng0 > 0 and lower_wick > 2 * body0 and upper_wick < body0 * 0.5:
        detected.append("hammer")

    if rng0 > 0 and upper_wick > 2 * body0 and lower_wick < body0 * 0.5:
        detected.append("shooting_star")

    # Bullish engulfing
    if c1 < o1 and c0 > o0 and o0 <= c1 and c0 >= o1:
        detected.append("bullish_engulfing")

    # Bearish engulfing
    if c1 > o1 and c0 < o0 and o0 >= c1 and c0 <= o1:
        detected.append("bearish_engulfing")

    body1 = abs(c1 - o1)
    body2 = abs(c2 - o2)

    # Morning star (3-candle bullish reversal)
    if (c2 < o2 and body1 < body2 * 0.3 and c0 > o0 and c0 > (o2 + c2) / 2):
        detected.append("morning_star")

    # Evening star (3-candle bearish reversal)
    if (c2 > o2 and body1 < body2 * 0.3 and c0 < o0 and c0 < (o2 + c2) / 2):
        detected.append("evening_star")

    bullish_set = {"hammer", "bullish_engulfing", "morning_star"}
    bearish_set = {"shooting_star", "bearish_engulfing", "evening_star"}
    b_count = sum(1 for p in detected if p in bullish_set)
    s_count = sum(1 for p in detected if p in bearish_set)

    signal = "bullish" if b_count > s_count else "bearish" if s_count > b_count else "neutral"
    return {"detected": detected, "signal": signal, "count": len(detected)}


def market_regime(adx_data: dict, bb_data: dict, atr_pct: float) -> dict:
    """Classify market as trending | ranging | volatile."""
    adx_val = adx_data.get("adx", 0)
    bb_width = bb_data.get("width_pct", 0)

    if adx_val > 25 and bb_width > 3.0:
        regime = "trending"
        confidence = min(1.0, (adx_val - 25) / 25)
    elif adx_val < 20 and bb_width < 2.5:
        regime = "ranging"
        confidence = min(1.0, (20 - adx_val) / 20)
    elif atr_pct > 3.0 or bb_width > 6.0:
        regime = "volatile"
        confidence = min(1.0, max(atr_pct / 5, bb_width / 8))
    else:
        regime = "ranging"
        confidence = 0.5

    trend_dir = adx_data.get("direction", "neutral") if regime == "trending" else "neutral"
    return {
        "regime": regime,
        "confidence": round(confidence, 2),
        "trend_direction": trend_dir,
        "adx": round(adx_val, 1),
        "bb_width_pct": round(bb_width, 2),
        "atr_pct": round(atr_pct, 2),
    }


def compute_tf_alignment(scores: list[float]) -> dict:
    """Consensus multiplier based on how well timeframes agree on direction."""
    if not scores:
        return {"multiplier": 1.0, "direction": "mixed", "bullish_tfs": 0, "bearish_tfs": 0, "total_tfs": 0, "aligned": False}

    bullish = sum(1 for s in scores if s > 0.1)
    bearish = sum(1 for s in scores if s < -0.1)
    total = len(scores)

    if bullish == total:
        mult, direction = (1.4 if total >= 3 else 1.2), "bullish"
    elif bearish == total:
        mult, direction = (1.4 if total >= 3 else 1.2), "bearish"
    elif max(bullish, bearish) >= round(total * 0.75):
        mult = 1.2
        direction = "bullish" if bullish > bearish else "bearish"
    else:
        mult, direction = 0.75, "mixed"

    return {
        "multiplier": mult,
        "direction": direction,
        "bullish_tfs": bullish,
        "bearish_tfs": bearish,
        "total_tfs": total,
        "aligned": mult >= 1.2,
    }


def _regime_indicator_weights(regime: str) -> dict:
    """Per-indicator weights tuned to the current market regime.

    Trending  → boost trend-following (EMA, MACD, Ichimoku, ADX, Structure); penalise mean-reversion
    Ranging   → boost mean-reversion (RSI, Stoch, BB, Fib, Patterns); penalise trend-following
    Volatile  → boost flow/cloud (VWAP, CMF, Ichimoku, divergence); reduce everything noisy
    """
    if regime == "trending":
        return {
            "rsi":       1.0,   # Level signals lie in strong trends
            "rsi_div":   2.0,   # Divergence = early exhaustion warning — important
            "macd":      3.0,   # Excellent trend-following signal
            "bb":        0.8,   # Squeeze useful; overbought/oversold not
            "adx":       2.5,   # Trend strength + direction confirmation
            "stoch":     0.5,   # Mean-reversion oscillator, unreliable in trends
            "ema":       3.5,   # Primary trend filter — highest weight
            "ichimoku":  3.0,   # Cloud confluence is conviction multiplier
            "patterns":  1.0,   # Less reliable in strong persistent trends
            "fib":       0.5,   # Retracement levels less relevant
            "vwap":      2.0,   # Institutional bias reference
            "cmf":       2.0,   # Flow confirms the trend
            "structure": 3.0,   # HH/HL or LH/LL is the primary trend confirmation
        }
    elif regime == "ranging":
        return {
            "rsi":       3.0,   # Primary oscillator for range-bound price
            "rsi_div":   1.5,   # Divergence signals ahead of breakout
            "macd":      1.0,   # False crossovers plague ranging markets
            "bb":        2.5,   # %B overbought/oversold reliable in ranges
            "adx":       0.5,   # Low ADX → direction signal is noise
            "stoch":     3.0,   # Excellent mean-reversion oscillator
            "ema":       1.0,   # Often crisscrossing, mixed signal
            "ichimoku":  1.0,   # Kumo thin / unreliable in sideways
            "patterns":  2.0,   # Reversal patterns at range extremes shine
            "fib":       2.5,   # S/R levels define range boundaries
            "vwap":      1.5,   # Mean-reversion anchor
            "cmf":       1.5,   # Flow shows breakout direction potential
            "structure": 0.5,   # Ranging: no sustained HH/HL → low weight
        }
    else:  # volatile
        return {
            "rsi":       1.0,   # Whipsaws; level signals unreliable
            "rsi_div":   2.5,   # Key divergence warning in volatile markets
            "macd":      1.5,   # Histogram direction > crossovers
            "bb":        1.0,   # Wide bands already priced in
            "adx":       1.0,   # High ADX but direction flips fast
            "stoch":     0.8,   # Mean-reversion unreliable with high volatility
            "ema":       1.5,   # Long EMAs as S/R reference
            "ichimoku":  3.5,   # Wide cloud = best S/R zone in volatility
            "patterns":  2.0,   # Reversal candles at extremes most reliable
            "fib":       0.8,   # Fast moves skip levels
            "vwap":      2.5,   # Critical anchor during volatile sessions
            "cmf":       2.5,   # Volume flow under pressure = direction signal
            "structure": 1.5,   # Some swing structure survives high volatility
        }


def _finite(obj):
    """NaN/inf → None, recursively. FastAPI refuses to serialise NaN (allow_nan=False), so one
    degenerate indicator (a zero range, a zero ATR) must not take a whole response down."""
    if isinstance(obj, dict):
        return {k: _finite(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_finite(v) for v in obj]
    if isinstance(obj, (float, np.floating)) and not np.isfinite(obj):
        return None
    return obj


def compute_ta(candles: list[dict], interval: str = "1h") -> dict:
    """Compute all indicators for a set of candles."""
    if len(candles) < 60:
        return {"error": "insufficient_data", "interval": interval}
    return _finite(_compute_ta(candles, interval))


def _compute_ta(candles: list[dict], interval: str) -> dict:

    df = ohlcv_to_df(candles)
    close = df["close"]
    price = float(close.iloc[-1])

    rsi_series = _rsi_series(close)
    rsi_val = float(rsi_series.iloc[-1])
    div = rsi_divergence(close, rsi_series)
    macd_data = macd(close)
    bb = bollinger_bands(close)
    atr_val = atr(df)
    atr_pct_val = atr_val / price * 100
    adx_data = adx(df)
    stoch = stochastic(df)
    vol = volume_analysis(df)
    sr = support_resistance(df)
    emas = ema_cloud(close)
    ichi = ichimoku(df)
    fibs = fibonacci_levels(df)
    patterns = candlestick_patterns(df)
    regime = market_regime(adx_data, bb, atr_pct_val)

    vwap_data = vwap(df)
    cmf_val = chaikin_money_flow(df)
    ms = market_structure(df)

    score = _compute_signal_score(
        rsi_val=rsi_val,
        rsi_recent=rsi_series.tail(20),
        macd_data=macd_data,
        bb=bb,
        adx_data=adx_data,
        stoch=stoch,
        emas=emas,
        patterns=patterns,
        divergence=div,
        ichimoku_data=ichi,
        fibs=fibs,
        volume_data=vol,
        vwap_data=vwap_data,
        cmf_val=cmf_val,
        regime=regime.get("regime", "ranging"),
        structure_data=ms,
    )

    return {
        "interval": interval,
        "price": price,
        "rsi": round(rsi_val, 2),
        "rsi_signal": "overbought" if rsi_val > 70 else "oversold" if rsi_val < 30 else "neutral",
        "rsi_divergence": div,
        "macd": macd_data,
        "bollinger_bands": bb,
        "atr": round(atr_val, 4),
        "atr_pct": round(atr_pct_val, 2),
        "adx": adx_data,
        "stochastic": stoch,
        "volume": vol,
        "support_resistance": sr,
        "ema_cloud": emas,
        "ichimoku": ichi,
        "fibonacci": fibs,
        "candlestick_patterns": patterns,
        "market_regime": regime,
        "market_structure": ms,
        "vwap": vwap_data,
        "cmf": round(cmf_val, 4),
        "signal_score": round(score, 3),
        "signal_label": _score_label(score),
    }


def _compute_signal_score(
    rsi_val: float,
    rsi_recent: "pd.Series",
    macd_data: dict,
    bb: dict,
    adx_data: dict,
    stoch: dict,
    emas: dict,
    patterns=None,
    divergence=None,
    ichimoku_data=None,
    fibs=None,
    volume_data=None,
    vwap_data=None,
    cmf_val=None,
    regime: str = "ranging",
    structure_data=None,
) -> float:
    """Regime-aware signal scoring.

    Each indicator produces a raw score. Scores are weighted by regime, summed,
    then divided by total weight for a normalised average in [-1, +1].
    Volume is applied last as a direction-aware conviction multiplier.

    Five improvements over a naive weighted average:
    1. Market structure (HH/HL vs LH/LL) as an explicit indicator.
    2. MACD histogram rate-of-change: building momentum > stale positive histogram.
    3. Fibonacci proximity scaled by how many times price has tested that level.
    4. Direction-aware volume: low volume on a pullback in an uptrend is healthy.
    5. Candlestick patterns multiplied by location context (key level vs mid-range).
    """
    wt = _regime_indicator_weights(regime)
    points = 0.0
    total_weight = 0.0

    # ── RSI: level + slope blended by regime ────────────────────────────────
    w = wt["rsi"]
    if rsi_val < 30:       rsi_level = 1.0
    elif rsi_val > 70:     rsi_level = -1.0
    elif rsi_val < 45:     rsi_level = 0.35
    elif rsi_val > 55:     rsi_level = -0.35
    else:                  rsi_level = 0.0

    rsi_slope = 0.0
    if len(rsi_recent) >= 5:
        slope = float(rsi_recent.iloc[-1]) - float(rsi_recent.iloc[-5])
        rsi_slope = max(-1.0, min(1.0, slope / 20.0))

    if regime == "trending":
        rsi_score = rsi_level * 0.3 + rsi_slope * 0.7
    else:
        rsi_score = rsi_level * 0.7 + rsi_slope * 0.3

    points += w * rsi_score
    total_weight += w

    # ── RSI Divergence ───────────────────────────────────────────────────────
    if divergence:
        w = wt["rsi_div"]
        if divergence["bullish"]:    points += w
        elif divergence["bearish"]:  points -= w
        total_weight += w

    # ── MACD: cross quality + histogram momentum (Fix 2) ────────────────────
    # A fresh cross is highest conviction. When no cross, distinguish between
    # an expanding histogram (momentum building) and a shrinking one (fading).
    w = wt["macd"]
    cross = macd_data.get("cross", "none")
    hist = macd_data.get("histogram", 0)
    hist_roc = macd_data.get("histogram_roc", 0)
    hist_expanding = macd_data.get("histogram_expanding", False)

    if cross == "bullish_cross":
        macd_score = 1.0
    elif cross == "bearish_cross":
        macd_score = -1.0
    elif hist > 0:
        if hist_expanding:       macd_score = 0.75   # momentum building
        elif hist_roc < 0:       macd_score = 0.20   # histogram shrinking → fading
        else:                    macd_score = 0.50   # stable positive
    else:
        if hist_expanding:       macd_score = -0.75  # bearish momentum building
        elif hist_roc > 0:       macd_score = -0.20  # bearish histogram shrinking
        else:                    macd_score = -0.50
    points += w * macd_score
    total_weight += w

    # ── Bollinger Bands ──────────────────────────────────────────────────────
    w = wt["bb"]
    pct_b = bb.get("pct_b", 0.5)
    if pct_b < 0.05:          bb_score = 1.0
    elif pct_b < 0.2:         bb_score = 0.65
    elif pct_b > 0.95:        bb_score = -1.0
    elif pct_b > 0.8:         bb_score = -0.65
    else:                     bb_score = 0.0   # a squeeze says a move is coming, not which way
    points += w * bb_score
    total_weight += w

    # ── ADX: continuous gradient × direction × slope ─────────────────────────
    w = wt["adx"]
    adx_val_raw = adx_data.get("adx", 0)
    if adx_val_raw >= 20:
        strength = min(1.0, (adx_val_raw - 20) / 30.0)
        adx_score = strength if adx_data["direction"] == "bullish" else -strength
        slope = adx_data.get("adx_slope", 0)
        slope_mult = max(0.6, min(1.4, 1.0 + slope / 15.0))
        adx_score = max(-1.0, min(1.0, adx_score * slope_mult))
    else:
        adx_score = 0.0
    points += w * adx_score
    total_weight += w

    # ── Stochastic: level + K/D cross in extreme zones ──────────────────────
    w = wt["stoch"]
    k = stoch.get("k", 50)
    d = stoch.get("d", 50)
    if k < 20:       stoch_score = 1.0
    elif k > 80:     stoch_score = -1.0
    elif k < 35:     stoch_score = 0.4
    elif k > 65:     stoch_score = -0.4
    else:            stoch_score = 0.0
    if k > d and k < 40:    stoch_score = max(stoch_score, 0.65)
    elif k < d and k > 60:  stoch_score = min(stoch_score, -0.65)
    points += w * stoch_score
    total_weight += w

    # ── EMA Cloud + 200 EMA long-term anchor ────────────────────────────────
    w = wt["ema"]
    alignment = emas.get("ema_alignment", "mixed")
    if alignment == "bullish_aligned":    ema_base = 0.8
    elif alignment == "bearish_aligned":  ema_base = -0.8
    elif emas.get("golden_cross"):        ema_base = 0.4
    elif emas.get("death_cross"):         ema_base = -0.4
    else:                                 ema_base = 0.0
    ema_200_adj = 0.2 if emas.get("price_above_200ema") else -0.2
    ema_score = max(-1.0, min(1.0, ema_base + ema_200_adj))
    points += w * ema_score
    total_weight += w

    # ── Ichimoku: signal + TK cross + cloud color ────────────────────────────
    if ichimoku_data:
        w = wt["ichimoku"]
        sig = ichimoku_data.get("signal", "neutral")
        tk = ichimoku_data.get("tk_cross", "none")
        cloud_color = ichimoku_data.get("cloud_color", "")
        if sig == "bullish":
            ichi_score = 0.70
            if tk == "bullish":         ichi_score += 0.15
            if cloud_color == "green":  ichi_score += 0.15
        elif sig == "bearish":
            ichi_score = -0.70
            if tk == "bearish":         ichi_score -= 0.15
            if cloud_color == "red":    ichi_score -= 0.15
        else:
            ichi_score = 0.0
        points += w * max(-1.0, min(1.0, ichi_score))
        total_weight += w

    # ── Candlestick Patterns × Location Context (Fix 5) ─────────────────────
    # A reversal pattern at a Fibonacci level or S/R extreme is high-conviction.
    # The same pattern in the middle of the range is mostly noise.
    # Score range: [-2, +2] before normalisation (intentional — key-level patterns
    # should outweigh a single non-confluent indicator).
    if patterns and patterns.get("count", 0) > 0:
        w = wt["patterns"]
        sig = patterns.get("signal", "neutral")
        base_score = 1.0 if sig == "bullish" else -1.0 if sig == "bearish" else 0.0

        if base_score != 0.0 and fibs and not fibs.get("error"):
            pos = fibs.get("price_position", 0.5)
            bullish_at_support = base_score > 0 and (fibs.get("near_support") or pos < 0.25)
            bearish_at_resistance = base_score < 0 and (fibs.get("near_resistance") or pos > 0.75)
            in_midrange = 0.35 < pos < 0.65

            if bullish_at_support or bearish_at_resistance:
                base_score *= 2.0   # pattern at key level: double conviction
            elif in_midrange:
                base_score *= 0.35  # mid-range pattern: mostly noise

        points += w * base_score
        total_weight += w

    # ── Fibonacci S/R proximity × Level Strength (Fix 3) ────────────────────
    # Levels tested 3+ times are well-established S/R; untested levels carry less weight.
    # Strength multiplier: 1.0 (0 tests) → 1.6 (3+ tests).
    if fibs and not fibs.get("error"):
        w = wt["fib"]
        pos = fibs.get("price_position", 0.5)
        if fibs.get("near_support"):          fib_score = 0.9
        elif fibs.get("near_resistance"):     fib_score = -0.9
        elif pos < 0.25:                      fib_score = 0.5
        elif pos > 0.75:                      fib_score = -0.5
        else:                                 fib_score = 0.0

        if fib_score != 0.0:
            touches = (
                fibs.get("nearest_support_touches", 0) if fib_score > 0
                else fibs.get("nearest_resistance_touches", 0)
            )
            strength_mult = 1.0 + min(touches, 3) * 0.2  # 1.0 → 1.6
            fib_score = max(-1.0, min(1.0, fib_score * strength_mult))

        points += w * fib_score
        total_weight += w

    # ── VWAP: institutional price reference ──────────────────────────────────
    if vwap_data:
        w = wt["vwap"]
        dev = abs(vwap_data.get("deviation_pct", 0))
        above = vwap_data.get("price_above_vwap", False)
        if above:
            vwap_score = 0.8 if dev <= 3 else 0.4 if dev <= 7 else 0.1
        else:
            vwap_score = -0.8 if dev <= 3 else -0.4 if dev <= 7 else -0.1
        points += w * vwap_score
        total_weight += w

    # ── CMF: money flow pressure ─────────────────────────────────────────────
    if cmf_val is not None:
        w = wt["cmf"]
        cmf_score = max(-1.0, min(1.0, float(cmf_val) * 3.0))
        points += w * cmf_score
        total_weight += w

    # ── Market Structure: HH/HL vs LH/LL (Fix 1) ────────────────────────────
    if structure_data:
        w = wt["structure"]
        points += w * structure_data.get("score", 0.0)
        total_weight += w

    # ── Volume: direction-aware confidence multiplier (Fix 4) ────────────────
    # High volume → higher conviction (standard).
    # Low volume on a pullback AGAINST the composite direction = healthy consolidation
    # (smart money not participating) → minimal penalty.
    # Low volume confirming the composite direction = concerning → full penalty.
    raw = points / total_weight if total_weight else 0.0

    vol_mult = 1.0
    if volume_data:
        ratio = volume_data.get("volume_ratio", 1.0)
        if ratio >= 2.5:
            vol_mult = 1.20
        elif ratio >= 1.5:
            vol_mult = 1.10
        elif ratio <= 0.35:
            obv_trend = volume_data.get("obv_trend", "falling")
            # Low-volume pullback: OBV still rising while price dips in uptrend (or vice versa)
            is_healthy_pullback = (
                (obv_trend == "rising" and raw > 0.1) or
                (obv_trend == "falling" and raw < -0.1)
            )
            vol_mult = 0.92 if is_healthy_pullback else 0.80
        elif ratio <= 0.65:
            vol_mult = 0.92

    return max(-1.0, min(1.0, raw * vol_mult))


def _score_label(score: float) -> str:
    if score >= 0.6:
        return "strong_buy"
    if score >= 0.2:
        return "buy"
    if score <= -0.6:
        return "strong_sell"
    if score <= -0.2:
        return "sell"
    return "neutral"


# Higher timeframes carry more weight in the consensus score
_TF_WEIGHTS = {"4h": 0.35, "1h": 0.35, "30m": 0.20, "15m": 0.10}


def compute_multi_timeframe_ta(ohlcv_by_interval: dict[str, list[dict]]) -> dict:
    """Run TA across multiple timeframes and return combined picture."""
    results = {}
    weighted_sum = 0.0
    weight_total = 0.0
    scores = []

    for interval, candles in ohlcv_by_interval.items():
        if candles:
            ta = compute_ta(candles, interval)
            results[interval] = ta
            if "signal_score" in ta:
                w = _TF_WEIGHTS.get(interval, 0.15)
                weighted_sum += ta["signal_score"] * w
                weight_total += w
                scores.append(ta["signal_score"])

    consensus = weighted_sum / weight_total if weight_total else 0.0
    alignment = compute_tf_alignment(scores)

    # Use regime from most reliable timeframe (4h preferred, then 1h, 30m, 15m)
    dominant_regime = {}
    for tf in ["4h", "1h", "30m", "15m"]:
        if tf in results and "market_regime" in results[tf]:
            dominant_regime = results[tf]["market_regime"]
            break

    return {
        "timeframes": results,
        "consensus_score": round(consensus, 3),
        "consensus_label": _score_label(consensus),
        "best_timeframe": _pick_best_timeframe(results),
        "tf_alignment": alignment,
        "dominant_regime": dominant_regime,
    }


def _pick_best_timeframe(results: dict) -> str:
    """Select the timeframe with the strongest signal weighted by reliability.
    Prefers higher timeframes when scores are similar (4h > 1h > 30m > 15m)."""
    def tf_quality(item):
        tf, data = item
        score = abs(data.get("signal_score", 0))
        reliability = _TF_WEIGHTS.get(tf, 0.10)
        # ADX-weighted quality: strong trend on higher TF > weak signal on lower TF
        adx_bonus = min(0.2, ((data.get("adx") or {}).get("adx") or 0) / 200)
        return score * reliability + adx_bonus

    valid = [(tf, d) for tf, d in results.items() if "signal_score" in d and not d.get("error")]
    if not valid:
        return "1h"
    return max(valid, key=tf_quality)[0]
