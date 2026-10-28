"""Per-symbol live market state built from the websocket feed.

Raw events are folded into 1-second bars as they arrive (mid OHLC, spread, depth
imbalance, aggressor buy/sell volume, trade count, VWAP inputs). Every feature the
strategy uses is derived from those bars, so the same numbers can be recomputed later
from the market database for research and replay.
"""

import math
import time
from collections import deque
from dataclasses import dataclass, field

BAR_HISTORY = 3600          # 1h of 1-second bars in memory


@dataclass
class SecondBar:
    ts: int                 # epoch second
    o: float
    h: float
    l: float
    c: float
    spread_sum: float = 0.0
    obi_sum: float = 0.0
    book_n: int = 0
    buy_vol: float = 0.0    # aggressor-buy base volume
    sell_vol: float = 0.0
    trades: int = 0
    pv: float = 0.0         # Σ price × qty (VWAP)
    vol: float = 0.0

    @property
    def spread_bps(self) -> float | None:
        return self.spread_sum / self.book_n if self.book_n else None

    @property
    def obi(self) -> float | None:
        return self.obi_sum / self.book_n if self.book_n else None


@dataclass
class MarketState:
    symbol: str
    bids: list = field(default_factory=list)     # [(price, qty)] best first
    asks: list = field(default_factory=list)
    bars: deque = field(default_factory=lambda: deque(maxlen=BAR_HISTORY))
    cur: SecondBar | None = None
    last_trade_price: float | None = None
    next_funding_ms: int | None = None
    lag_ms: deque = field(default_factory=lambda: deque(maxlen=200))
    started: float = field(default_factory=time.time)

    # ── Ingest ────────────────────────────────────────────────────────────────

    @property
    def best_bid(self) -> float | None:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0][0] if self.asks else None

    @property
    def mid(self) -> float | None:
        if self.bids and self.asks:
            return (self.bids[0][0] + self.asks[0][0]) / 2
        return self.last_trade_price

    def _bar(self, now: float) -> SecondBar | None:
        sec = int(now)
        mid = self.mid
        if mid is None:
            return None
        if self.cur is None and self.bars and self.bars[-1].ts < sec:
            # Resuming after reloaded history: bridge the restart gap with flat bars.
            last = self.bars[-1]
            for k in range(min(sec - last.ts - 1, 120)):
                self.bars.append(SecondBar(last.ts + k + 1, last.c, last.c, last.c, last.c))
        if self.cur is None or self.cur.ts != sec:
            if self.cur is not None:
                self.bars.append(self.cur)
                # Fill silent seconds with flat bars so time-based windows stay honest.
                gap = min(sec - self.cur.ts - 1, 30)
                for k in range(gap):
                    c = self.cur.c
                    self.bars.append(SecondBar(self.cur.ts + k + 1, c, c, c, c))
            self.cur = SecondBar(sec, mid, mid, mid, mid)
        return self.cur

    def _touch(self, event_ms: int | None, now: float) -> None:
        if event_ms:
            # Signed: negative means the local clock is behind the exchange's.
            self.lag_ms.append(now * 1000 - event_ms)

    def on_depth(self, bids: list, asks: list, event_ms: int | None = None, now: float | None = None) -> None:
        now = now or time.time()
        self.bids = [(float(p), float(q)) for p, q in bids if float(q) > 0]
        self.asks = [(float(p), float(q)) for p, q in asks if float(q) > 0]
        self._touch(event_ms, now)
        bar = self._bar(now)
        if bar is None or not (self.bids and self.asks):
            return
        mid = self.mid
        bar.h, bar.l, bar.c = max(bar.h, mid), min(bar.l, mid), mid
        bar.spread_sum += (self.asks[0][0] - self.bids[0][0]) / mid * 1e4
        bar.obi_sum += self.depth_imbalance()
        bar.book_n += 1

    def on_trade(self, price: float, qty: float, buyer_is_maker: bool,
                 event_ms: int | None = None, now: float | None = None) -> None:
        now = now or time.time()
        self.last_trade_price = price
        self._touch(event_ms, now)
        bar = self._bar(now)
        if bar is None:
            return
        if buyer_is_maker:          # seller crossed the spread
            bar.sell_vol += qty
        else:
            bar.buy_vol += qty
        bar.trades += 1
        bar.pv += price * qty
        bar.vol += qty

    def on_mark(self, next_funding_ms: int,
                event_ms: int | None = None, now: float | None = None) -> None:
        # Only the next settlement time is kept here; the book charges funding from the event itself.
        self.next_funding_ms = next_funding_ms
        self._touch(event_ms, now or time.time())

    def roll(self, now: float | None = None) -> None:
        """Close the current second even when nothing traded (called by the engine clock)."""
        self._bar(now or time.time())

    # ── Features ──────────────────────────────────────────────────────────────

    def depth_imbalance(self) -> float:
        b = sum(q for _, q in self.bids[:5])
        a = sum(q for _, q in self.asks[:5])
        return (b - a) / (b + a) if b + a else 0.0

    def level_qty(self, side: str, price: float) -> float:
        levels = self.bids if side == "bid" else self.asks
        return next((q for p, q in levels if p == price), 0.0)

    def history_seconds(self) -> int:
        return len(self.bars)

    def minute_bars(self, minutes: int) -> list[tuple[float, float, float, float]]:
        """(o, h, l, c) of mid per minute, oldest first, from 1s bars."""
        out: dict[int, list[float]] = {}
        for b in list(self.bars)[-minutes * 60:]:
            m = b.ts // 60
            if m not in out:
                out[m] = [b.o, b.h, b.l, b.c]
            else:
                x = out[m]
                x[1], x[2], x[3] = max(x[1], b.h), min(x[2], b.l), b.c
        return [tuple(v) for _, v in sorted(out.items())]

    def features(self) -> dict | None:
        """Everything the strategy (and the research log) needs, or None while warming up."""
        mid = self.mid
        bars = list(self.bars)
        if mid is None or len(bars) < 120:
            return None

        flows = [b.buy_vol - b.sell_vol for b in bars[-600:]]
        prefix = [0.0]
        for x in flows:
            prefix.append(prefix[-1] + x)
        roll30 = [prefix[i] - prefix[i - 30] for i in range(30, len(prefix))]
        flow_30 = roll30[-1] if roll30 else 0.0
        mu = sum(roll30) / len(roll30) if roll30 else 0.0
        sd = math.sqrt(sum((x - mu) ** 2 for x in roll30) / len(roll30)) if len(roll30) > 1 else 0.0
        flow_z = (flow_30 - mu) / sd if sd > 0 else 0.0
        flow_10 = sum(flows[-10:])

        last3 = [b.obi for b in bars[-3:] if b.obi is not None]
        obi = sum(last3) / len(last3) if last3 else self.depth_imbalance()

        mins = self.minute_bars(30)
        closes = [m[3] for m in mins]
        trs = [max(h - l, abs(h - pc), abs(l - pc)) for (o, h, l, c), pc in zip(mins[1:], closes[:-1])]
        atr = sum(trs[-14:]) / len(trs[-14:]) if trs else None
        atr_bps = atr / mid * 1e4 if atr else None

        def ema(xs: list[float], n: int) -> float | None:
            if len(xs) < n:
                return None
            k, e = 2 / (n + 1), xs[0]
            for x in xs[1:]:
                e = x * k + e * (1 - k)
            return e

        ema9, ema21 = ema(closes, 9), ema(closes, 21)

        last15 = bars[-900:]
        pv, vol = sum(b.pv for b in last15), sum(b.vol for b in last15)
        vwap = pv / vol if vol else None
        vwap_dev_atr = (mid - vwap) / atr if vwap and atr else None

        rets = [math.log(b2.c / b1.c) for b1, b2 in zip(bars[-61:-1], bars[-60:]) if b1.c and b2.c]
        rv_bps = math.sqrt(sum(r * r for r in rets) / len(rets)) * 1e4 if rets else 0.0

        spreads = [b.spread_bps for b in bars[-600:] if b.spread_bps is not None]
        med_spread = sorted(spreads)[len(spreads) // 2] if spreads else None
        spread_bps = (self.asks[0][0] - self.bids[0][0]) / mid * 1e4 if self.bids and self.asks else None

        if self.bids and self.asks:
            bq, aq = self.bids[0][1], self.asks[0][1]
            micro = (self.bids[0][0] * aq + self.asks[0][0] * bq) / (bq + aq)
            micro_skew_bps = (micro - mid) / mid * 1e4
        else:
            micro_skew_bps = 0.0

        return {
            "mid": mid, "best_bid": self.best_bid, "best_ask": self.best_ask,
            "spread_bps": spread_bps, "median_spread_bps": med_spread,
            "obi": obi, "flow_10s": flow_10, "flow_30s": flow_30, "flow_z": flow_z,
            "atr_bps": atr_bps, "atr": atr, "ema9": ema9, "ema21": ema21,
            "vwap": vwap, "vwap_dev_atr": vwap_dev_atr, "rv_60s_bps": rv_bps,
            "micro_skew_bps": micro_skew_bps,
            "trades_per_s": sum(b.trades for b in bars[-30:]) / 30,
            "history_s": len(bars),
        }

