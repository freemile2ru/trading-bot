"""Loss patterns — what our own losing trades have in common, and whether fixing it would have helped.

Run: venv/bin/python -m research.loss_patterns   (reads the live databases, writes nothing but
data/research_cache/loss_patterns.json)

Three questions, all on OUR trades (crypto swing books: autotrader, its rules twin, the signal
books, Claude swing — manual trades left out):

  1. Entry filters. Every trade is tagged with what was known at entry (direction, BTC regime,
     conviction, timeframe, hour, setups, Claude's say, how many same-direction entries were
     already open across the books…). For each candidate "skip trades where X", compare the R of
     the trades it removes with the R of those it keeps.
  2. Exits. Every entry also runs under the other exit policies in the shadow book, so each entry
     has its R under each policy — a paired comparison on identical entries.
  3. Claude's vetoes. Declined entries are shadowed too: did the declined ones do worse?

Honesty rules (the backtests flattered every idea we had, so these are strict):
  * the same move taken by several books is ONE decision (same symbol and direction opened within
    2h): stats use decisions, not raw trades, so one bad move can't count five times
  * chronological halves: a pattern is found on the older half and must hold on the newer half
  * luck baseline: the same search run on shuffled R values — how many "patterns" pass by chance
  * an idea is only worth trying if it passes both halves AND clearly beats the luck count
"""
from __future__ import annotations

import json
import os
import random
import statistics as st
from collections import defaultdict
from datetime import datetime, timezone

GROUP_S = 2 * 3600
MIN_REMOVED = 8
MIN_GAIN_R = 0.15
SHUFFLES = 300
OUT = os.path.join(os.path.dirname(__file__), "..", "data", "research_cache", "loss_patterns.json")


# ── Data ───────────────────────────────────────────────────────────────────────

def _ts(iso: str | None) -> float | None:
    return datetime.fromisoformat(iso).timestamp() if iso else None


def load_trades() -> list[dict]:
    from trading import guards
    rows = []
    for book, w in guards._wallets().items():
        if book.startswith("main"):
            continue
        for t in w.all_trades()["closed"]:
            if not t.get("risk_usd") or not t.get("opened_at"):
                continue
            reg = t.get("regime_at_entry") or {}
            opened = t.get("opened_ts") or _ts(t["opened_at"])
            rows.append({
                "book": book, "id": t["id"], "symbol": t["symbol"], "direction": t["direction"],
                "opened": opened, "closed": _ts(t.get("closed_at")), "r": t["pnl_usd"] / t["risk_usd"],
                "peak_r": t.get("peak_r"), "close_reason": t.get("close_reason"),
                "conviction": abs(t.get("conviction_at_entry") or 0), "timeframe": t.get("timeframe"),
                "btc_trend": reg.get("btc_trend"), "btc_7d": reg.get("btc_7d_pct"), "btc_vol": reg.get("btc_vol_30d"),
                "regime": reg.get("label"), "setups": tuple(sorted(t.get("setups_at_entry") or [])),
                "entry_type": t.get("entry_type"), "claude": t.get("claude_verdict"),
                "confidence": t.get("claude_confidence") or t.get("oracle_confidence"),
                "entry_score": t.get("entry_score"), "hour": datetime.fromtimestamp(opened, timezone.utc).hour,
                "rr": t.get("risk_reward"), "sl_pct": abs(t["entry_price"] - (t.get("original_stop_loss") or t["stop_loss"])) / t["entry_price"] * 100,
            })
    rows.sort(key=lambda r: r["opened"])
    # Crowding: distinct same-direction symbols any book had opened in the 6h before this entry.
    for i, r in enumerate(rows):
        r["crowd"] = len({x["symbol"] for x in rows[:i] if x["direction"] == r["direction"]
                          and r["opened"] - x["opened"] <= 6 * 3600 and x["symbol"] != r["symbol"]})
    return rows


def decisions(trades: list[dict]) -> list[dict]:
    """One row per market move: same symbol and direction opened within GROUP_S across books."""
    out: list[dict] = []
    for t in trades:
        g = next((d for d in reversed(out[-40:]) if d["symbol"] == t["symbol"] and d["direction"] == t["direction"]
                  and t["opened"] - d["opened"] <= GROUP_S), None)
        if g:
            g["rs"].append(t["r"])
            g["books"].add(t["book"])
            g["peak_r"] = max(g["peak_r"] or 0, t["peak_r"] or 0)
        else:
            out.append({**t, "rs": [t["r"]], "books": {t["book"]}})
    for d in out:
        d["r"] = st.mean(d["rs"])
    return out


# ── Entry filters ──────────────────────────────────────────────────────────────

def _with_btc(d):
    return (d["direction"] == "long" and d["btc_trend"] == "up") or (d["direction"] == "short" and d["btc_trend"] == "down")


CANDIDATES = {
    "long": lambda d: d["direction"] == "long",
    "short": lambda d: d["direction"] == "short",
    "against BTC trend": lambda d: d["btc_trend"] in ("up", "down") and not _with_btc(d),
    "BTC trend flat/unknown": lambda d: d["btc_trend"] not in ("up", "down"),
    "BTC calm (vol < 35)": lambda d: (d["btc_vol"] or 99) < 35,
    "BTC volatile (vol >= 50)": lambda d: (d["btc_vol"] or 0) >= 50,
    "BTC moved > 5% in 7d": lambda d: abs(d["btc_7d"] or 0) > 5,
    "short after BTC fell > 4% in 7d (late)": lambda d: d["direction"] == "short" and (d["btc_7d"] or 0) < -4,
    "long after BTC rose > 4% in 7d (late)": lambda d: d["direction"] == "long" and (d["btc_7d"] or 0) > 4,
    "conviction < 0.5": lambda d: d["conviction"] < 0.5,
    "conviction >= 0.6": lambda d: d["conviction"] >= 0.6,
    "timeframe 1h": lambda d: d["timeframe"] == "1h",
    "timeframe 4h": lambda d: d["timeframe"] == "4h",
    "timeframe 15m/30m": lambda d: d["timeframe"] in ("15m", "30m"),
    "Asia hours (00-08 UTC)": lambda d: d["hour"] < 8,
    "Europe hours (08-13 UTC)": lambda d: 8 <= d["hour"] < 13,
    "US hours (13-21 UTC)": lambda d: 13 <= d["hour"] < 21,
    "late US (21-24 UTC)": lambda d: d["hour"] >= 21,
    "crowded (>= 2 same-direction entries in 6h)": lambda d: d["crowd"] >= 2,
    "very crowded (>= 4)": lambda d: d["crowd"] >= 4,
    "taken by 2+ books (consensus)": lambda d: len(d["books"]) >= 2,
    "limit/maker entry": lambda d: d["entry_type"] in ("limit", "post_only"),
    "no Claude (rules only)": lambda d: d["claude"] in ("skipped", None) and d["book"] in ("auto_rules", "signals_rules"),
    "wide stop (> 4%)": lambda d: d["sl_pct"] > 4,
    "tight stop (< 2%)": lambda d: d["sl_pct"] < 2,
    "reward:risk < 1.5": lambda d: (d["rr"] or 9) < 1.5,
    "ichimoku_breakout setup": lambda d: "ichimoku_breakout" in d["setups"],
    "divergence_reversal setup": lambda d: "divergence_reversal" in d["setups"],
    "only the 3-setup trend stack": lambda d: set(d["setups"]) == {"mtf_confluence", "trend_continuation", "ichimoku_breakout"},
    "mean_reversion setup": lambda d: any("revers" in s or "mean" in s for s in d["setups"]),
}


def _mean(xs):
    return round(st.mean(xs), 3) if xs else None


def judge_filter(pred, rows) -> dict:
    removed = [d["r"] for d in rows if pred(d)]
    kept = [d["r"] for d in rows if not pred(d)]
    gain = (st.mean(kept) - st.mean(rows_r)) if kept and (rows_r := [d["r"] for d in rows]) else None
    return {"removed": len(removed), "removed_r": _mean(removed), "kept": len(kept), "kept_r": _mean(kept),
            "gain_r": round(gain, 3) if gain is not None else None, "total_r_saved": round(-sum(removed), 2)}


def _passes(a: dict, b: dict) -> bool:
    return all(x["removed"] >= MIN_REMOVED and x["gain_r"] is not None and x["gain_r"] >= MIN_GAIN_R and x["removed_r"] < 0
               for x in (a, b))


def entry_filters(rows: list[dict], rng: random.Random) -> dict:
    half = len(rows) // 2
    old, new = rows[:half], rows[half:]
    res = {}
    for name, pred in CANDIDATES.items():
        a, b = judge_filter(pred, old), judge_filter(pred, new)
        res[name] = {"older": a, "newer": b, "all": judge_filter(pred, rows), "passes": _passes(a, b)}
    passed = [k for k, v in res.items() if v["passes"]]
    # Luck: shuffle R across decisions (keeps every feature, breaks any link to the result).
    luck = []
    for _ in range(SHUFFLES):
        rs = [d["r"] for d in rows]
        rng.shuffle(rs)
        sh = [{**d, "r": r} for d, r in zip(rows, rs)]
        luck.append(sum(_passes(judge_filter(p, sh[:half]), judge_filter(p, sh[half:])) for p in CANDIDATES.values()))
    luck.sort()
    return {"results": res, "passed": passed, "luck_mean": round(st.mean(luck), 2),
            "luck_p95": luck[int(0.95 * len(luck)) - 1], "candidates": len(CANDIDATES)}


# ── Exits: the shadow book, paired by entry ────────────────────────────────────

def exit_comparison(rng: random.Random) -> dict:
    from trading import exits, guards
    by_entry: dict[tuple, dict] = defaultdict(dict)
    live = {}
    for book, w in guards._wallets().items():
        if book.startswith("main"):
            continue
        for r in w.shadow_report()["results"]:
            if r.get("claude_verdict") in exits.NOT_TAKEN:
                continue
            by_entry[(book, r["trade_id"])][r["policy"]] = (r["r"], r["closed_ts"], r.get("claude_verdict"))
        for t in w.all_trades()["closed"]:
            if t.get("risk_usd") and t.get("exit_policy"):
                live[(book, t["id"])] = (t["exit_policy"], t["pnl_usd"] / t["risk_usd"], _ts(t.get("closed_at")))
    # The live policy's result is the trade itself.
    for k, (pol, r, ts) in live.items():
        by_entry[k].setdefault(pol, (r, ts, "live"))
    policies = sorted({p for e in by_entry.values() for p in e})
    entries = sorted(by_entry.items(), key=lambda kv: min(v[1] or 0 for v in kv[1].values()))
    out = {}
    base = "protect"
    for p in policies:
        if p == base:
            continue
        pairs = [(e[p][0], e[base][0], min(e[p][1] or 0, e[base][1] or 0)) for _, e in entries if p in e and base in e]
        if len(pairs) < 20:
            continue
        diffs = [a - b for a, b, _ in pairs]
        h = len(diffs) // 2
        boot = sorted(st.mean(rng.choices(diffs, k=len(diffs))) for _ in range(2000))
        out[p] = {"pairs": len(pairs), "avg_r": _mean([a for a, _, _ in pairs]), "protect_avg_r": _mean([b for _, b, _ in pairs]),
                  "diff_r": _mean(diffs), "diff_older": _mean(diffs[:h]), "diff_newer": _mean(diffs[h:]),
                  "ci90": [round(boot[100], 3), round(boot[1899], 3)]}
    return {"baseline": base, "vs_protect": out}


def mfe_profile(rows: list[dict]) -> dict:
    """How far losers got before losing: the case for an earlier lock-in."""
    losers = [d for d in rows if d["r"] < -0.5 and d["peak_r"] is not None]
    buckets = {"never above +0.25R": 0, "+0.25 to +0.5R": 0, "+0.5 to +1R": 0, "above +1R": 0}
    for d in losers:
        p = d["peak_r"]
        k = "never above +0.25R" if p < 0.25 else "+0.25 to +0.5R" if p < 0.5 else "+0.5 to +1R" if p < 1 else "above +1R"
        buckets[k] += 1
    return {"losers": len(losers), "peak_before_loss": buckets}


def claude_vetoes() -> dict:
    """Declined entries (shadowed under the live policy) against approved ones."""
    from trading import exits, guards
    declined, approved = [], []
    for book, w in guards._wallets().items():
        for r in w.shadow_report()["results"]:
            if r.get("policy") != exits.DEFAULT_POLICY:
                continue
            if r.get("claude_verdict") == "declined":
                declined.append(r["r"])
        for t in w.all_trades()["closed"]:
            if t.get("claude_verdict") == "approved" and t.get("risk_usd"):
                approved.append(t["pnl_usd"] / t["risk_usd"])
    return {"declined": len(declined), "declined_r": _mean(declined), "approved": len(approved), "approved_r": _mean(approved)}


def main() -> dict:
    rng = random.Random(11)
    trades = load_trades()
    rows = decisions(trades)
    rs = [d["r"] for d in rows]
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "trades": len(trades), "decisions": len(rows), "avg_r": _mean(rs),
        "win_rate": round(sum(r > 0 for r in rs) / len(rs) * 100, 1),
        "by_close_reason": {k: {"n": len(v), "avg_r": _mean(v)} for k, v in
                            sorted(_group(rows, lambda d: d["close_reason"]).items(), key=lambda kv: -len(kv[1]))},
        "mfe": mfe_profile(rows),
        "entry_filters": entry_filters(rows, rng),
        "exits": exit_comparison(rng),
        "claude_vetoes": claude_vetoes(),
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(out, open(OUT, "w"), indent=1, default=str)
    return out


def _group(rows, key):
    g = defaultdict(list)
    for d in rows:
        g[key(d)].append(d["r"])
    return g


if __name__ == "__main__":
    res = main()
    ef = res["entry_filters"]
    print(f"{res['trades']} trades → {res['decisions']} decisions · avg {res['avg_r']}R · win {res['win_rate']}%")
    print("close reasons:", {k: (v["n"], v["avg_r"]) for k, v in res["by_close_reason"].items()})
    print("losers' peak before losing:", res["mfe"])
    print(f"\nENTRY FILTERS — passed both halves: {ef['passed']}  (luck: {ef['luck_mean']} on average, 95th pct {ef['luck_p95']}, of {ef['candidates']})")
    for k, v in sorted(ef["results"].items(), key=lambda kv: -(kv[1]["all"]["gain_r"] or -9)):
        a, o, n = v["all"], v["older"], v["newer"]
        print(f"  {'PASS' if v['passes'] else '    '} skip {k:45s} removes {a['removed']:3d} (avg {a['removed_r']}R) → kept avg {a['kept_r']}R "
              f"gain {a['gain_r']} | older {o['gain_r']} newer {n['gain_r']}")
    print("\nEXITS vs protect (same entries):")
    for k, v in sorted(res["exits"]["vs_protect"].items(), key=lambda kv: -kv[1]["diff_r"]):
        print(f"  {k:14s} pairs {v['pairs']:4d} avg {v['avg_r']}R vs protect {v['protect_avg_r']}R diff {v['diff_r']} "
              f"(older {v['diff_older']}, newer {v['diff_newer']}) ci90 {v['ci90']}")
    print("\nClaude vetoes:", res["claude_vetoes"])
