"""Event study — what Binance listing and delisting announcements do to prices afterwards.

Run: venv/bin/python -m research.event_study_lab   (pages Binance's public announcement API back ~18
months, fetches 1h perp candles around each event; caches under research_cache/events/; writes
research_cache/event_study_results.json)

Three event types, each measured from the first 1h close AFTER the announcement (realistic latency),
return in bps of the coin's perp minus BTC over +1h, +4h, +24h, +72h, +7d, plus the 24h BEFORE (leakage):
  spot listing      "Binance Will List X (TICKER)" — only where TICKERUSDT already trades on Binance Futures
  futures launch    "Binance Futures Will Launch USDⓈ-M TICKERUSDT Perpetual" — the perp's own first days,
                    from its first 1h close (is a fresh listing a short?)
  delisting         "Binance Will Delist …" / "Binance Futures Will Delist …USDT" — short from the announcement
Each event type: n, mean and median excess return per horizon, hit rate, t-stat (events as independent
observations), first vs second half of the sample. Costs are 16 bps a round trip (taker both ways).
"""
from __future__ import annotations

import json
import math
import os
import re
import statistics as st
import time

import httpx

from news.sources import BINANCE_URL, BROWSER_AGENT, parse_binance
from research.swing_lab import CACHE_DIR

OUT = os.path.join(CACHE_DIR, "event_study_results.json")
EVENTS_DIR = os.path.join(CACHE_DIR, "events")
FAPI = "https://fapi.binance.com/fapi/v1/klines"
HORIZONS = {"+1h": 1, "+4h": 4, "+24h": 24, "+72h": 72, "+7d": 168, "+14d": 336, "+21d": 504, "+30d": 720}
PAGES = {"48": 14, "161": 6}                    # listings ~700 back to 2025-03; delistings: all there is
COST_BPS = 16
STABLE = {"USDT", "USDC", "FDUSD", "BUSD", "TUSD", "DAI", "USD1", "RWUSD", "BFUSD", "USDE", "EUR", "TRY"}


def fetch_announcements() -> list[dict]:
    os.makedirs(EVENTS_DIR, exist_ok=True)
    path = os.path.join(EVENTS_DIR, "announcements.json")
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < 86400:
        return json.load(open(path))
    c = httpx.Client(timeout=20, headers={"User-Agent": BROWSER_AGENT, "Accept": "application/json"})
    out = []
    for cat, pages in PAGES.items():
        for page in range(1, pages + 1):
            r = c.get(BINANCE_URL, params={"type": 1, "pageNo": page, "pageSize": 50, "catalogId": cat})
            if r.status_code != 200:
                break
            items = parse_binance(r.json(), cat)
            if not items:
                break
            out += items
            time.sleep(0.4)
    json.dump(out, open(path, "w"))
    return out


def classify(title: str) -> tuple[str, list[str]] | None:
    t = title.replace("USDⓈ-Margined", "USDⓈ-M").replace("USDS-M", "USDⓈ-M")
    if "Pre-Market" in t or "Multiple" in t:
        return None
    m = re.search(r"Binance Futures Will Launch USDⓈ-M (\w+?)USDT Perpetual", t)
    if m:
        return "futures launch", [m.group(1).upper()]
    m = re.search(r"Binance Futures Will Delist (.+?) USDⓈ-M", t)
    if m:
        return "delisting", [x.strip().replace("USDT", "") for x in re.split(r",| and ", m.group(1)) if x.strip()]
    if t.startswith("Binance Will Delist"):
        body = t.split("Delist", 1)[1].split(" on ")[0]
        return "delisting", [x.strip() for x in re.split(r",| and ", body) if re.fullmatch(r"[A-Z0-9]{2,10}", x.strip())]
    if t.startswith("Binance Will List") or t.startswith("Binance Will Add"):
        ticks = re.findall(r"\(([A-Z0-9]{2,10})\)", t)
        return ("spot listing", ticks) if ticks else None
    return None


def klines(c: httpx.Client, symbol: str, start_ms: int, end_ms: int) -> list[list]:
    path = os.path.join(EVENTS_DIR, f"{symbol}_{start_ms}_{(end_ms - start_ms) // 86_400_000}d.json")
    if os.path.exists(path):
        return json.load(open(path))
    out, t = [], start_ms
    while t < end_ms:
        try:
            r = c.get(FAPI, params={"symbol": symbol, "interval": "1h", "startTime": t, "endTime": end_ms, "limit": 1000})
            if r.status_code != 200:
                break
            chunk = r.json()
        except Exception:
            break
        if not chunk:
            break
        out += chunk
        if len(chunk) < 1000:
            break
        t = chunk[-1][0] + 1
    json.dump(out, open(path, "w"))
    time.sleep(0.15)
    return out


def _close_at(bars: list[list], ts_ms: int) -> float | None:
    """Close of the first bar closing at or after ts_ms."""
    for b in bars:
        if b[6] >= ts_ms:
            return float(b[4])
    return None


def measure(c: httpx.Client, ticker: str, ann_ms: int, kind: str) -> dict | None:
    sym = f"{ticker}USDT"
    start = ann_ms - 30 * 3600_000
    end = ann_ms + 32 * 24 * 3600_000
    bars = klines(c, sym, start, end)
    btc = klines(c, "BTCUSDT", start, end)
    if not bars or not btc:
        return None
    if kind == "futures launch":
        if bars[0][0] < ann_ms - 2 * 3600_000:          # the perp already existed: not a launch we can read
            return None
        base_ms = bars[0][6]                            # the first 1h close after launch
    else:
        if bars[0][0] > ann_ms - 25 * 3600_000:         # needs to have traded before the announcement
            return None
        base_ms = ann_ms
    p0, b0 = _close_at(bars, base_ms), _close_at(btc, base_ms)
    if not p0 or not b0:
        return None
    out = {"ticker": ticker, "kind": kind, "ann": ann_ms / 1000}
    for name, h in HORIZONS.items():
        p1, b1 = _close_at(bars, base_ms + h * 3600_000), _close_at(btc, base_ms + h * 3600_000)
        out[name] = (math.log(p1 / p0) - math.log(b1 / b0)) * 1e4 if p1 and b1 else None
    if kind != "futures launch":
        pp, bb = _close_at(bars, ann_ms - 24 * 3600_000), _close_at(btc, ann_ms - 24 * 3600_000)
        out["-24h"] = (math.log(p0 / pp) - math.log(b0 / bb)) * 1e4 if pp and bb else None
    try:
        fr = c.get(f"{FAPI.rsplit('/', 1)[0]}/fundingRate", params={"symbol": sym, "startTime": base_ms, "limit": 1000}).json()
        for name, h in HORIZONS.items():
            paid = -sum(float(x["fundingRate"]) for x in fr if base_ms <= int(x["fundingTime"]) <= base_ms + h * 3600_000) * 1e4
            out[f"fund{name}"] = paid                     # bps a short paid (negative rates) over the horizon
    except Exception:
        pass
    return out


def summarize(events: list[dict]) -> dict:
    res = {"n": len(events)}
    events = sorted(events, key=lambda e: e["ann"])
    half = len(events) // 2
    for h in HORIZONS:
        paid = [e[f"fund{h}"] for e in events if e.get(f"fund{h}") is not None]
        if paid:
            res[f"fund{h}"] = {"mean_bps_paid_by_short": round(st.mean(paid), 1), "median": round(st.median(paid), 1)}
    for h in ["-24h", *HORIZONS]:
        v = [e[h] for e in events if e.get(h) is not None]
        if len(v) < 5:
            continue
        m, sd = st.mean(v), (st.pstdev(v) if len(v) > 1 else 0)
        a = [e[h] for e in events[:half] if e.get(h) is not None]
        b = [e[h] for e in events[half:] if e.get(h) is not None]
        res[h] = {"n": len(v), "mean_bps": round(m, 1), "median_bps": round(st.median(v), 1),
                  "hit_pct": round(sum(x > 0 for x in v) / len(v) * 100, 1),
                  "t": round(m / (sd / math.sqrt(len(v))), 2) if sd else None,
                  "first_half": round(st.mean(a), 1) if a else None, "second_half": round(st.mean(b), 1) if b else None}
    return res


def main() -> dict:
    anns = fetch_announcements()
    c = httpx.Client(timeout=20)
    events, seen = [], set()
    for a in anns:
        if not a.get("published"):
            continue
        cl = classify(a["title"])
        if not cl:
            continue
        kind, tickers = cl
        for tk in tickers:
            if tk in STABLE or (tk, kind) in seen:
                continue
            seen.add((tk, kind))
            e = measure(c, tk, int(a["published"] * 1000), kind)
            if e:
                events.append(e)
    out = {"announcements": len(anns), "events": len(events), "cost_bps_round_trip": COST_BPS,
           "period": [time.strftime("%Y-%m-%d", time.gmtime(min(e["ann"] for e in events))),
                      time.strftime("%Y-%m-%d", time.gmtime(max(e["ann"] for e in events)))] if events else None,
           "by_kind": {k: summarize([e for e in events if e["kind"] == k]) for k in ("spot listing", "futures launch", "delisting")}}
    json.dump(out, open(OUT, "w"), indent=1)
    return out


if __name__ == "__main__":
    r = main()
    print(f"{r['announcements']} announcements → {r['events']} measurable events {r['period']} (excess return vs BTC, bps; costs {COST_BPS} bps round trip)")
    for kind, s in r["by_kind"].items():
        print(f"== {kind}: n={s['n']}")
        for h, v in s.items():
            if isinstance(v, dict) and not h.startswith("fund"):
                f = s.get(f"fund{h}", {}).get("mean_bps_paid_by_short")
                net = f"net to a short after funding ≈ {-v['mean_bps'] - f:+.0f} bps" if f is not None else ""
                v = {**v, "net": net}
                print(f"   {h:5s} n={v['n']:3d} mean {v['mean_bps']:+7.0f} median {v['median_bps']:+7.0f} hit {v['hit_pct']:5.1f}% t {v['t']} | halves {v['first_half']} / {v['second_half']} | {v.get('net', '')}")
