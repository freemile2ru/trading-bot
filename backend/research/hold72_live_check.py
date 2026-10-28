"""Hold-72 live check — replay our real pairs on Binance 15m candles under different holds and the watcher.

Run: venv/bin/python -m research.hold72_live_check   (reads swing_trading.db read-only, fetches candles)

Each closed pair is replayed from its real entry: return at a 24h / 48h / 72h close (12% stop on the
way), and with the viability watcher (close at the first of two consecutive scanner readings below X in
the pair's direction). Pair return in % of notional after 4 taker fills. Used 2026-10-23 to set the
24h cap and the 0.25 watcher; rerun it to see whether they keep beating a longer hold.
"""
import asyncio
import sqlite3
import httpx
from storage.db import SWING_DB
from research import thesis_exit_lab as T
FAPI = "https://fapi.binance.com/fapi/v1/klines"
async def kl(c, sym, start):
    r = await c.get(FAPI, params={"symbol": sym, "interval": "15m", "startTime": start, "limit": 300}); r.raise_for_status()
    return {b[0]: float(b[4]) for b in r.json()}
async def main():
    con = sqlite3.connect(f"file:{SWING_DB}?mode=ro", uri=True); con.row_factory = sqlite3.Row
    pairs = [dict(r) for r in con.execute("SELECT * FROM hold72 WHERE status='closed' ORDER BY opened_ts")]
    rows = []
    async with httpx.AsyncClient(timeout=20) as c:
        for p in pairs:
            start = int(p["opened_ts"] * 1000) // 900_000 * 900_000
            coin, btc = await kl(c, p["symbol"], start), await kl(c, "BTCUSDT", start)
            s = 1 if p["direction"] == "long" else -1
            ts = sorted(t for t in coin if t in btc and t > start)
            path = [(t, s * (coin[t] / p["coin_entry"] - 1) - s * (btc[t] / p["btc_entry"] - 1)) for t in ts]
            def at(hours):
                lim = start + hours * 3600_000
                for t, x in path:
                    if x <= -0.12: return x
                    if t >= lim: return x
                return path[-1][1]
            reads = T._readings(p["symbol"], p["opened_ts"], p["opened_ts"] + 72 * 3600)
            def watch(below, hours=72):
                prev = False
                for rts, conv in reads:
                    ok = conv * s < below
                    if ok and prev:
                        for t, x in path:
                            if x <= -0.12: return x
                            if t / 1000 >= rts: return x
                    prev = ok
                return at(hours)
            actual = (p["gross"] or 0) / p["notional"]
            rows.append({"sym": p["symbol"], "dir": p["direction"], "actual": actual, "h24": at(24), "h48": at(48), "h72": at(72),
                         "w10": watch(0.10), "w25": watch(0.25), "w10_24": watch(0.10, 24), "n_reads": len(reads)})
            await asyncio.sleep(0.2)
    cost = 4 * 0.0008
    print(f"{len(rows)} live pairs (pair return % of notional, after 4 fills)")
    for k in ("actual", "h72", "h48", "h24", "w25", "w10", "w10_24"):
        v = [r[k] - (0 if k == "actual" else cost) for r in rows]
        wins = sum(x > 0 for x in v); losses = [x for x in v if x <= 0]
        print(f"  {k:7s} avg {sum(v)/len(v)*100:+.2f}%  wins {wins}/{len(v)}  avg loss {sum(losses)/max(len(losses),1)*100:+.2f}%  total {sum(v)*100:+.1f}%")
    print("readings per pair (median):", sorted(r["n_reads"] for r in rows)[len(rows)//2])
if __name__ == "__main__":
    asyncio.run(main())
