"""Swing review — what the live system's own decisions are worth, from its databases.

  1. Claude's value   Every autotrader entry Claude approved was traded; every one it declined
                      was tracked as a rules-only shadow under the same exit policy. The gap
                      between the two, per decision, set against what the calls cost.
  2. Interventions    Real R minus the same trade's untouched shadow (its own exit policy, no
                      adjustments), grouped by what was done to it: stop tightened, pyramided,
                      averaged down, trimmed, closed by hand, left the watchlist.
  3. Conditions       Closed trades of both wallets by direction, BTC's 7-day trend at entry,
                      hour of day and weekday.

Small samples are printed with their size and a bootstrap 90% interval — read the interval,
not the average. This is a tool to rerun as trades accumulate (weekly is plenty).

Usage (from backend/):
    ./venv/bin/python -m research.swing_review
    ./venv/bin/python -m research.swing_review --days 30

Claude cost assumes Opus list prices; override with CLAUDE_USD_PER_MTOK_IN / _OUT.
"""

import argparse
import asyncio
import json
import os
import sqlite3
import time

import numpy as np
import pandas as pd

from storage.db import SIGNALS_DB, SWING_DB

USD_IN = float(os.getenv("CLAUDE_USD_PER_MTOK_IN", "5"))
USD_OUT = float(os.getenv("CLAUDE_USD_PER_MTOK_OUT", "25"))


def _ci(x: pd.Series, n_boot: int = 2000) -> str:
    x = x.dropna().to_numpy()
    if len(x) < 3:
        return "n/a"
    rng = np.random.default_rng(0)
    means = rng.choice(x, (n_boot, len(x))).mean(axis=1)
    return f"{np.percentile(means, 5):+.2f} … {np.percentile(means, 95):+.2f}"


def _summary(r: pd.Series) -> dict:
    return {"n": int(r.notna().sum()), "avg R": r.mean(), "win%": (r > 0).mean() * 100 if len(r) else np.nan,
            "total R": r.sum(), "90% range of avg": _ci(r)}


def load(days: float | None):
    since = time.time() - days * 86400 if days else 0
    con = sqlite3.connect(SWING_DB)
    trades = [dict(json.loads(d), wallet=label) for label, d in con.execute(
        "SELECT label, data FROM trades WHERE status='closed' AND closed_at >= ?",
        (pd.Timestamp(since, unit="s", tz="UTC").isoformat(),))]
    shadows = [json.loads(d) for (d,) in con.execute(
        "SELECT data FROM shadow_results WHERE closed_ts >= ?", (since,))]
    return pd.DataFrame(trades), pd.DataFrame(shadows)


# ── 1. Claude ──────────────────────────────────────────────────────────────────

def claude_value(trades: pd.DataFrame, shadows: pd.DataFrame) -> None:
    from trading.exits import DEFAULT_POLICY

    print("\n1. IS CLAUDE WORTH IT? — autotrader entries, approved (traded) vs declined (rules-only shadow), "
          f"both under the '{DEFAULT_POLICY}' exit policy, R per trade")
    if shadows.empty or "claude_verdict" not in shadows:
        print("   no shadow results yet")
        return
    s = shadows[(shadows.policy == DEFAULT_POLICY) & shadows.claude_verdict.notna()]
    rows = [{"verdict": v, **_summary(g.r)} for v, g in s.groupby("claude_verdict")]
    print(pd.DataFrame(rows).round(2).to_string(index=False) if rows else "   none yet")
    if not trades.empty and "claude_verdict" in trades:
        real = trades[(trades.wallet == "auto") & (trades.claude_verdict == "approved") & (trades.risk_usd > 0)]
        if len(real):
            r = real.pnl_usd / real.risk_usd
            print(f"   (approved, as actually traded incl. interventions: n {len(r)}, avg {r.mean():+.2f}R, "
                  f"90% range {_ci(r)})")
    d = s[s.claude_verdict == "declined"].r
    if len(d):
        # Against taking every entry the rules proposed, declining is worth −(sum of declined R):
        # positive when the declined trades would have lost.
        verb = "saved" if d.sum() < 0 else "cost"
        print(f"\n   Declining {verb} {abs(d.sum()):.2f}R over {len(d)} declines "
              f"({(d < 0).mean() * 100:.0f}% of declined entries would have lost). Declines on the same coin "
              f"repeat after each 1h veto, so these overlap — treat as fewer independent samples.")
    con = sqlite3.connect(SIGNALS_DB)
    metas = [json.loads(x).get("oracle", {}).get("_meta") or {} for (x,) in con.execute("SELECT data FROM fired_signals")]
    metas = [m for m in metas if m.get("input_tokens")]
    if metas:
        tin = np.mean([m["input_tokens"] - m.get("cache_read_tokens", 0) * 0.9 for m in metas])
        tout = np.mean([m["output_tokens"] for m in metas])
        per_call = tin / 1e6 * USD_IN + tout / 1e6 * USD_OUT
        print(f"   Claude costs ≈ ${per_call:.3f} per call ({tin:,.0f} effective input / {tout:,.0f} output tokens, "
              f"cache reads at 10%). At 1% risk on a $10k wallet 1R = $100, so a call pays for itself "
              f"if it adds ≥ {per_call / 100:.4f}R.")


# ── 2. Interventions ───────────────────────────────────────────────────────────

def interventions(trades: pd.DataFrame, shadows: pd.DataFrame) -> None:
    print("\n2. DO INTERVENTIONS HELP? — real R minus the same trade's untouched shadow (own exit policy)")
    if trades.empty or shadows.empty:
        print("   not enough closed trades with shadows yet")
        return
    own = {(r.trade_id, r.policy): r.r for r in shadows.itertuples()}
    rows = []
    for t in trades.itertuples():
        base = own.get((t.id, getattr(t, "exit_policy", None)))
        if base is None or not t.risk_usd:
            continue
        adj = t.adjustments if isinstance(t.adjustments, list) else []
        kinds = sorted({a.get("kind") or a.get("action") for a in adj} - {"tp1_partial", None})
        touched = kinds + ([t.close_reason] if t.close_reason in ("manual", "left_watchlist", "signal_flip") else [])
        rows.append({"what": ", ".join(touched) or "untouched", "delta": t.pnl_usd / t.risk_usd - base})
    if not rows:
        print("   no trades matched to their shadows yet")
        return
    df = pd.DataFrame(rows)
    out = [{"intervention": k, **_summary(g.delta)} for k, g in df.groupby("what")]
    print(pd.DataFrame(out).rename(columns={"avg R": "avg ΔR", "total R": "total ΔR"}).round(2).to_string(index=False))
    print("   'untouched' should sit near 0 — it checks that the shadows replay trades faithfully.")


# ── 3. Conditions ──────────────────────────────────────────────────────────────

async def _btc_hourly(start_ms: int) -> pd.Series:
    from data.derivatives_client import fetch_klines_range

    k = await fetch_klines_range("BTCUSDT", "1h", start_ms - 8 * 86_400_000, int(time.time() * 1000))
    return pd.Series([r[4] for r in k], index=pd.to_datetime([r[0] for r in k], unit="ms", utc=True))


def conditions(trades: pd.DataFrame) -> None:
    print("\n3. WHEN DOES IT WORK? — closed trades, both wallets, R per trade")
    if trades.empty:
        print("   no closed trades")
        return
    t = trades[trades.risk_usd.fillna(0) > 0].copy()
    t["R"] = t.pnl_usd / t.risk_usd
    t["opened"] = pd.to_datetime(t.opened_at, utc=True, format="ISO8601")
    btc = asyncio.run(_btc_hourly(int(t.opened.min().timestamp() * 1000)))
    trend = (btc / btc.shift(24 * 7) - 1).reindex(t.opened.dt.floor("1h"), method="ffill").values
    t["BTC 7d"] = np.where(trend > 0.03, "up >3%", np.where(trend < -0.03, "down >3%", "flat ±3%"))
    t["hour (UTC)"] = (t.opened.dt.hour // 8 * 8).map(lambda h: f"{h:02d}–{h + 8:02d}")
    t["day"] = np.where(t.opened.dt.weekday >= 5, "weekend", "weekday")
    for col in ("wallet", "direction", "BTC 7d", "hour (UTC)", "day"):
        rows = [{col: k, **_summary(g.R)} for k, g in t.groupby(col)]
        print(pd.DataFrame(rows).round(2).to_string(index=False))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--days", type=float, default=None)
    args = ap.parse_args()
    trades, shadows = load(args.days)
    pd.set_option("display.width", 200)
    print(f"Swing review — {len(trades)} closed trades, {len(shadows)} shadow results")
    claude_value(trades, shadows)
    interventions(trades, shadows)
    conditions(trades)


if __name__ == "__main__":
    main()
