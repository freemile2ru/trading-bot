"""Entry lab — which of the scanner's own readings say anything about the next 4 and 24 hours?

The autotrader's entries lose (−0.13R over its last 20) while its exits make money (the "protect" policy +0.52R in
shadow). Its entry signal is the scanner's conviction, which the year-long rebuild found to be a 24h-momentum reading
with no information. But the scanner records far more than price every few minutes for every coin — positioning
(open interest change, long/short account share, top-trader long share), flow (taker buy/sell), funding (level, 7d,
z-score, basis), news (score, burst, story count), fundamentals (protocol fees, relative strength, momentum shape,
liquidity, supply) — and labels every scan with the coin's forward 4h / 24h / 72h return, raw and versus the
cross-section (storage/signals: scan_features ⨝ scan_labels, since 2026-09-27).

Method, decided before looking (2026-10-31):
  sample    one scan per coin per 4 hours (the first scan after each 4h mark), so the labels don't overlap
  score     each numeric reading's cross-sectional rank IC per timestamp (Spearman across coins), averaged; the same
            split into the first and second halves of the period; t-stat of the per-timestamp series
  targets   xs_4h and xs_24h (forward return minus the cross-section's mean) — what a long/short book would earn
  model     the readings that pass (|IC| ≥ 0.03, same sign in both halves, |t| ≥ 2 on xs_24h) ranked and averaged with
            their signs into one score, trained on the first half only; its IC on the second half, and a book that is
            long the top 5 / short the bottom 5 of that score each 4h for 24h, net of 8 bps a side
  adopt     only if the second-half IC of the score is ≥ 0.03 and the second-half book is positive after costs; the
            autotrader's scanner lane then enters on this score instead of conviction. Otherwise: no entry edge found
            in the scanner's readings, and the lane stays on measuring size until its verdict.

    ./venv/bin/python -m research.entry_lab
"""
from __future__ import annotations

import json
import os
import sqlite3
import time

import numpy as np
import pandas as pd

from storage.db import DATA_DIR

DB = os.path.join(DATA_DIR, "signals.db")
RESULTS = os.path.join(os.path.dirname(__file__), "..", "data", "research_cache", "entry_results.json")
STEP_H = 4
COST_BPS = 8.0
IC_MIN, T_MIN = 0.03, 2.0
TOP_K = 5
SKIP = {"setups", "regime", "btc_trend", "confidence"}          # text, or the scanner's own confidence (downstream of the rest)


def load() -> pd.DataFrame:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    q = ("SELECT f.id, f.ts, f.symbol, f.price, f.conviction, f.composite, f.ta_score, f.fa_score, f.fa_components, f.features, "
         "l.fwd_4h, l.fwd_24h, l.fwd_72h, l.xs_4h, l.xs_24h, l.xs_72h FROM scan_features f JOIN scan_labels l ON l.scan_id = f.id")
    rows = con.execute(q).fetchall()
    con.close()
    cols = ["id", "ts", "symbol", "price", "conviction", "composite", "ta_score", "fa_score", "fa_components", "features",
            "fwd_4h", "fwd_24h", "fwd_72h", "xs_4h", "xs_24h", "xs_72h"]
    df = pd.DataFrame(rows, columns=cols)
    feats = pd.DataFrame([{k: v for k, v in (json.loads(x) or {}).items() if k not in SKIP} for x in df["features"]])
    fa = pd.DataFrame([{f"fa_{k}": v for k, v in (json.loads(x) or {}).items()} for x in df["fa_components"]]).apply(pd.to_numeric, errors="coerce")
    feats = feats.apply(pd.to_numeric, errors="coerce")
    out = pd.concat([df.drop(columns=["features", "fa_components"]), feats, fa], axis=1)
    out["bucket"] = (out["ts"] // (STEP_H * 3600)) * (STEP_H * 3600)
    out = out.sort_values("ts").groupby(["symbol", "bucket"], as_index=False).first()        # the first scan after each 4h mark
    for c in ("fwd_4h", "fwd_24h", "fwd_72h", "xs_4h", "xs_24h", "xs_72h"):
        out[c] = pd.to_numeric(out[c], errors="coerce")
    return out


def feature_columns(df: pd.DataFrame) -> list[str]:
    base = ["conviction", "composite", "ta_score", "fa_score"]
    skip = set(base) | {"id", "ts", "symbol", "price", "bucket", "fwd_4h", "fwd_24h", "fwd_72h", "xs_4h", "xs_24h", "xs_72h"}
    cols = [c for c in df.columns if c not in skip and df[c].notna().mean() > 0.5 and df[c].nunique() > 5]
    return base + cols


def rank_ic(df: pd.DataFrame, col: str, target: str) -> pd.Series:
    """Per-timestamp Spearman across coins; needs 8 coins with both readings."""
    out = {}
    for b, g in df.groupby("bucket"):
        x, y = g[col], g[target]
        m = x.notna() & y.notna()
        if m.sum() < 8:
            continue
        out[b] = x[m].rank().corr(y[m].rank())
    return pd.Series(out).dropna()


def ic_table(df: pd.DataFrame, cols: list[str], target: str) -> dict:
    out = {}
    for c in cols:
        s = rank_ic(df, c, target)
        if len(s) < 20:
            continue
        half = len(s) // 2
        t = float(s.mean() / (s.std(ddof=1) / np.sqrt(len(s)))) if s.std(ddof=1) > 0 else 0.0
        out[c] = {"ic": round(float(s.mean()), 4), "first": round(float(s.iloc[:half].mean()), 4), "second": round(float(s.iloc[half:].mean()), 4),
                  "t": round(t, 2), "n": int(len(s))}
    return dict(sorted(out.items(), key=lambda kv: -abs(kv[1]["ic"])))


def passes(row: dict) -> bool:
    return abs(row["ic"]) >= IC_MIN and abs(row["t"]) >= T_MIN and np.sign(row["first"]) == np.sign(row["second"]) == np.sign(row["ic"])


def score_from(df: pd.DataFrame, chosen: dict[str, float]) -> pd.Series:
    """Average of the chosen readings' cross-sectional ranks, each signed by its IC — one number per scan."""
    parts = []
    for c, sign in chosen.items():
        r = df.groupby("bucket")[c].rank(pct=True)
        parts.append(sign * (r - 0.5))
    return pd.concat(parts, axis=1).mean(axis=1) if parts else pd.Series(np.nan, index=df.index)


def book(df: pd.DataFrame, score: pd.Series, target: str, k: int = TOP_K, cost_bps: float = COST_BPS) -> dict:
    """Long the top k / short the bottom k of the score each 4h, held for the target's horizon; returns per bucket."""
    rets = []
    for b, g in df.assign(score=score).dropna(subset=["score", target]).groupby("bucket"):
        if len(g) < 2 * k + 2:
            continue
        g = g.sort_values("score")
        r = (g[target].iloc[-k:].mean() - g[target].iloc[:k].mean()) / 2 - 2 * cost_bps / 100      # labels are in percent
        rets.append((b, r))
    if not rets:
        return {"buckets": 0}
    s = pd.Series(dict(rets)).sort_index()
    half = len(s) // 2
    return {"buckets": int(len(s)), "avg_pct": round(float(s.mean()), 4), "first_half_pct": round(float(s.iloc[:half].mean()), 4),
            "second_half_pct": round(float(s.iloc[half:].mean()), 4), "positive_pct": round(float((s > 0).mean() * 100), 1),
            "t": round(float(s.mean() / (s.std(ddof=1) / np.sqrt(len(s)))), 2) if s.std(ddof=1) > 0 else None}


def year_check() -> dict:
    """The same idea on the year-long 4h panel (research/signal_rebuild_lab): fade 24h momentum, long the 5 weakest / short
    the 5 strongest every 4h for 24h vs BTC, net 8 bps a side. The scanner's history is five weeks; this says whether the
    reversal is a regime or a feature of the market."""
    try:
        from research import signal_rebuild_lab as SR
        big = SR.build(pd.read_pickle(SR.DATA))
    except Exception as e:
        return {"error": str(e)}
    rets = []
    for ts, g in big.dropna(subset=["mom_24h", "target"]).groupby("ts"):
        if len(g) < 12:
            continue
        g = g.sort_values("mom_24h")
        rets.append((ts, (g["target"].iloc[:5].mean() - g["target"].iloc[-5:].mean()) / 2 * 100 - 2 * COST_BPS / 100))   # target is a log return
    s = pd.Series(dict(rets)).sort_index()
    half = len(s) // 2
    return {"slots": int(len(s)), "avg_pct": round(float(s.mean()), 4), "first_half_pct": round(float(s.iloc[:half].mean()), 4),
            "second_half_pct": round(float(s.iloc[half:].mean()), 4), "positive_pct": round(float((s > 0).mean() * 100), 1),
            "t": round(float(s.mean() / (s.std(ddof=1) / np.sqrt(len(s)))), 2) if len(s) > 2 else None}


def main() -> dict:
    t0 = time.time()
    df = load()
    cols = feature_columns(df)
    buckets = sorted(df["bucket"].unique())
    split = buckets[len(buckets) // 2]
    train, test = df[df["bucket"] < split], df[df["bucket"] >= split]
    ics = {tgt: ic_table(df, cols, tgt) for tgt in ("xs_4h", "xs_24h", "fwd_24h")}
    # the model is chosen on the TRAIN half's xs_24h table and judged on the test half
    train_ic = ic_table(train, cols, "xs_24h")
    chosen = {c: float(np.sign(v["ic"])) for c, v in train_ic.items() if passes(v)}
    out = {"rows": int(len(df)), "coins": int(df["symbol"].nunique()), "buckets": len(buckets), "from": time.strftime("%Y-%m-%d", time.gmtime(df["ts"].min())),
           "to": time.strftime("%Y-%m-%d", time.gmtime(df["ts"].max())), "ic": ics, "train_ic": train_ic, "chosen": chosen}
    if chosen:
        sc = score_from(df, chosen)
        test_ic = rank_ic(test.assign(score=sc.loc[test.index]), "score", "xs_24h")
        out["score_test_ic"] = round(float(test_ic.mean()), 4) if len(test_ic) else None
        out["score_test_t"] = round(float(test_ic.mean() / (test_ic.std(ddof=1) / np.sqrt(len(test_ic)))), 2) if len(test_ic) > 2 and test_ic.std(ddof=1) > 0 else None
        out["book_train"] = book(train, sc.loc[train.index], "xs_24h")
        out["book_test"] = book(test, sc.loc[test.index], "xs_24h")
        out["book_conviction_test"] = book(test, test["conviction"], "xs_24h")
        out["year_check"] = year_check()
        ok = (out["score_test_ic"] or 0) >= IC_MIN and (out["book_test"].get("avg_pct") or 0) > 0
        year_ok = (out["year_check"].get("avg_pct") or 0) > 0
        if ok and year_ok:
            out["verdict"] = f"adopt — the score has second-half IC {out['score_test_ic']}, the 5/5 book nets {out['book_test']['avg_pct']:+.3f}% per 4h slot, and the year-long fade check agrees"
        elif ok:
            out["verdict"] = (f"regime-dependent — on the scanner's five weeks the score has second-half IC {out['score_test_ic']} and the 5/5 book nets "
                              f"{out['book_test']['avg_pct']:+.3f}% per 4h slot, but fading 24h momentum over the year-long panel loses "
                              f"{out['year_check'].get('avg_pct'):+.3f}% a slot: it runs as a measuring lane sized by its own record per regime, not as a promoted rule")
        else:
            out["verdict"] = f"no entry edge adopted — second-half IC {out['score_test_ic']}, book {out['book_test'].get('avg_pct')}% per slot after costs"
    else:
        out["year_check"] = year_check()
        out["verdict"] = "no entry edge found — no reading passes |IC| ≥ 0.03, |t| ≥ 2 with the same sign in both halves on the training half"
    out["rule"] = "adopt only if the chosen score's second-half IC ≥ 0.03 and the second-half 5/5 book is positive after 8 bps a side"
    out["seconds"] = round(time.time() - t0, 1)
    json.dump(out, open(RESULTS, "w"), indent=1, default=str)
    return out


if __name__ == "__main__":
    rep = main()
    print(f"{rep['rows']} scans, {rep['coins']} coins, {rep['buckets']} 4h slots from {rep['from']} to {rep['to']} ({rep['seconds']}s)\n")
    for tgt in ("xs_4h", "xs_24h", "fwd_24h"):
        print(f"== rank IC vs {tgt}  (ic · first half · second half · t)")
        for c, v in list(rep["ic"][tgt].items())[:18]:
            flag = " *" if passes(v) else ""
            print(f"  {c:28} {v['ic']:+.4f}  {v['first']:+.4f}  {v['second']:+.4f}  t {v['t']:+.2f}{flag}")
    print("\nchosen on the training half:", rep["chosen"])
    for k in ("score_test_ic", "score_test_t", "book_train", "book_test", "book_conviction_test", "year_check"):
        if k in rep:
            print(f"  {k}: {rep[k]}")
    print(f"\nVerdict: {rep['verdict']}\nWritten to {RESULTS}")
