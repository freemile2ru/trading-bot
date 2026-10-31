"""The capital plan from the evidence: a score rule for drift, a verdict ladder for promotion — applied daily.

The plan in force (trading/capital.py) started as a table someone chose. This module says what the record would move,
using written rules, and — since 2026-10-31, "the profit machine" — the tracker applies the result once a day
(CAPITAL_AUTO=false turns that off and leaves the Portfolio panel's button). Two rules, both pre-registered:

  score rule (2026-10-29): see RULE below — slow drift of a quarter of a losing book's capital to the winners.
  ladder (2026-10-31): a book whose 40-trade verdict shows an edge (95% range above zero) is promoted at once to
      PROMOTE_FRAC of the total; one whose verdict shows no edge is cut to FLOOR_USD at once. Promotions are paid for
      by the demotions, then by the main wallet's reserve above its floor, then by the books with no verdict yet,
      pro rata above their floors. The total never changes. Nothing is promoted on a backtest, a hunch or a good week —
      only on its own closed trades.

  rule (set 2026-10-29)
    score      a book's P&L per day over the last 30 days as a share of its capital, blended half-and-half with the
               evidence's expected $/day per share where the book has one (realised alone where it hasn't)
    eligible   books with at least MIN_DAYS of equity history; younger books are left where they are
    move       from every eligible book with a negative score, MOVE_FRAC of its capital, never below FLOOR_USD
               (so it keeps being measured); to the eligible books with a positive score, pro rata to their score,
               never above CAP_FRAC of the total
    total      unchanged
"""
from __future__ import annotations

MIN_DAYS = 14
MOVE_FRAC = 0.25
FLOOR_USD = 500.0
CAP_FRAC = 0.30
PROMOTE_FRAC = 0.10                      # a book with a proven edge is raised to this share of the total at once
RESERVE_BOOK = "main"                    # the hand wallet: its balance above the floor is the ladder's first source of funds
LADDER_RULE = (f"A book whose 40-trade verdict shows an edge is promoted at once to {PROMOTE_FRAC:.0%} of the total; one whose verdict shows "
               f"no edge is cut to ${FLOOR_USD:,.0f}. Promotions are funded by demotions, then the main wallet above ${FLOOR_USD:,.0f}, "
               f"then pro rata from the books still collecting evidence. Set 2026-10-31.")
RULE = (f"Score = 30-day realised $/day per $ of capital, blended 50/50 with the evidence's expected $/day where a book has one. "
        f"Books with {MIN_DAYS}+ days of history and a negative score give up {MOVE_FRAC:.0%} of their capital (never below "
        f"${FLOOR_USD:,.0f}); books with a positive score receive it pro rata (never above {CAP_FRAC:.0%} of the total). "
        f"Total unchanged. Set 2026-10-29.")


def score(row: dict) -> float | None:
    cap = row.get("planned") or 0
    if not cap or (row.get("days") or 0) < MIN_DAYS:
        return None
    realised = (row.get("avg30") or 0.0) / cap
    exp = row.get("expected")
    if exp is None:
        return realised
    return 0.5 * realised + 0.5 * exp / cap


def suggest(rows: list[dict]) -> dict:
    """rows: the tracker's per-book rows (book, planned, avg30, expected, days). Returns the suggestion."""
    plan = {r["book"]: float(r["planned"]) for r in rows if r.get("planned")}
    total = sum(plan.values())
    scores = {r["book"]: score(r) for r in rows if r.get("planned")}
    givers = {b: s for b, s in scores.items() if s is not None and s < 0}
    takers = {b: s for b, s in scores.items() if s is not None and s > 0}
    new = dict(plan)
    moves = []
    if givers and takers:
        pool = 0.0
        for b in givers:
            out = min(plan[b] * MOVE_FRAC, max(plan[b] - FLOOR_USD, 0.0))
            if out >= 1:
                new[b] = round(plan[b] - out, 2)
                pool += out
                moves.append({"book": b, "from": plan[b], "to": new[b], "delta": round(-out, 2),
                              "why": f"score {scores[b] * 100:+.2f}% a day over 30 days"})
        weight_total = sum(takers.values())
        remaining = pool
        for b, s in sorted(takers.items(), key=lambda kv: -kv[1]):
            room = max(total * CAP_FRAC - plan[b], 0.0)
            give = min(pool * s / weight_total, room, remaining)
            if give >= 1:
                new[b] = round(plan[b] + give, 2)
                remaining -= give
                moves.append({"book": b, "from": plan[b], "to": new[b], "delta": round(give, 2),
                              "why": f"score {s * 100:+.2f}% a day over 30 days"})
        if remaining >= 1:
            # Caps left some of the pool unplaced: it goes back to the givers pro rata, so the total holds.
            for m in [m for m in moves if m["delta"] < 0]:
                back = remaining * (-m["delta"]) / pool
                new[m["book"]] = round(new[m["book"]] + back, 2)
                m["to"], m["delta"] = new[m["book"]], round(new[m["book"]] - m["from"], 2)
    # Rounding drift lands on the largest book so the totals match to the cent.
    drift = round(total - sum(new.values()), 2)
    if abs(drift) >= 0.01 and new:
        biggest = max(new, key=new.get)
        new[biggest] = round(new[biggest] + drift, 2)
    return {"rule": RULE, "current": plan, "suggested": new, "moves": sorted(moves, key=lambda m: m["delta"]),
            "scores": {b: (round(s * 100, 3) if s is not None else None) for b, s in scores.items()},
            "changed": any(abs(new[b] - plan[b]) >= 1 for b in plan)}


def verdicts(rows_by_system: dict[str, list[dict]], min_trades: int = 40) -> dict[str, dict]:
    """Each book's own 40-trade verdict from its closed trades (the Lab's rule): edge / no_edge / inconclusive, or
    collecting while it has fewer than `min_trades`."""
    import random
    from trading import trade_analytics
    out = {}
    for book, rows in rows_by_system.items():
        closed = [r for r in rows if r.get("status") == "closed"]
        n = len(closed)
        if n < min_trades:
            out[book] = {"status": "collecting", "trades": n}
            continue
        c = trade_analytics._confidence(closed, random.Random(7))
        lo, hi = c.get("expectancy_ci") or (None, None)
        status = "edge" if lo is not None and lo > 0 else "no_edge" if hi is not None and hi < 0 else "inconclusive"
        out[book] = {"status": status, "trades": n, "expectancy_usd": c.get("expectancy_usd"), "ci": [lo, hi]}
    return out


def ladder(plan: dict[str, float], verdicts_: dict[str, dict], floors: dict[str, float] | None = None) -> tuple[dict[str, float], list[dict]]:
    """Apply the verdict ladder to a plan: returns (new plan, moves). Total preserved to the cent."""
    total = sum(plan.values())
    new = dict(plan)
    moves: list[dict] = []
    promote_to = round(total * PROMOTE_FRAC, 2)
    want = {}                                                    # book → amount it should gain
    for b, v in verdicts_.items():
        if b not in plan:
            continue
        if v.get("status") == "no_edge" and plan[b] > FLOOR_USD:
            new[b] = FLOOR_USD
            moves.append({"book": b, "from": plan[b], "to": FLOOR_USD, "delta": round(FLOOR_USD - plan[b], 2),
                          "why": f"verdict: no edge over {v.get('trades')} trades — cut to the floor"})
        elif v.get("status") == "edge" and plan[b] < promote_to:
            want[b] = round(promote_to - plan[b], 2)
    if not want:
        return new, moves
    pool = sum(-m["delta"] for m in moves)                       # what the demotions freed
    reserve = max(new.get(RESERVE_BOOK, 0.0) - FLOOR_USD, 0.0) if RESERVE_BOOK in new else 0.0
    need = sum(want.values())
    take_reserve = min(reserve, max(need - pool, 0.0))
    if take_reserve >= 1:
        new[RESERVE_BOOK] = round(new[RESERVE_BOOK] - take_reserve, 2)
        moves.append({"book": RESERVE_BOOK, "from": plan[RESERVE_BOOK], "to": new[RESERVE_BOOK], "delta": round(-take_reserve, 2),
                      "why": "the reserve pays for a promotion"})
        pool += take_reserve
    if need - pool >= 1:
        # The books still collecting evidence chip in pro rata, above their floors; a proven edge outranks a hunch.
        donors = {b: max(new[b] - FLOOR_USD, 0.0) for b in new if b not in want and b != RESERVE_BOOK
                  and verdicts_.get(b, {}).get("status") not in ("edge", "no_edge") and new[b] > FLOOR_USD}
        room = sum(donors.values())
        short = round(min(need - pool, room), 2)
        if short >= 1 and room > 0:
            order = sorted(donors.items(), key=lambda kv: -kv[1])
            given = 0.0
            for i, (b, r) in enumerate(order):
                give = round(short - given, 2) if i == len(order) - 1 else round(short * r / room, 2)   # the last donor takes the rounding
                if give >= 1:
                    new[b] = round(new[b] - give, 2)
                    moves.append({"book": b, "from": plan[b], "to": new[b], "delta": round(-give, 2), "why": "collecting evidence — chips in for a proven edge"})
                    given = round(given + give, 2)
            pool += given
    pool = round(pool, 2)
    for b, w in sorted(want.items(), key=lambda kv: -kv[1]):
        give = round(min(w, pool), 2)
        if give >= 1:
            new[b] = round(new[b] + give, 2)
            pool = round(pool - give, 2)
            moves.append({"book": b, "from": plan[b], "to": new[b], "delta": give,
                          "why": f"verdict: edge shown over {verdicts_[b].get('trades')} trades — promoted toward {PROMOTE_FRAC:.0%} of the total"})
    drift = round(total - sum(new.values()), 2)
    if abs(drift) >= 0.01:
        biggest = max(new, key=new.get)
        new[biggest] = round(new[biggest] + drift, 2)
    return new, moves


def plan_for(rows: list[dict], verdicts_: dict[str, dict] | None = None) -> dict:
    """The full suggestion: the score rule's drift, then the verdict ladder on top of it."""
    base = suggest(rows)
    if not verdicts_:
        return {**base, "ladder_rule": LADDER_RULE, "verdicts": {}}
    new, lmoves = ladder(base["suggested"], verdicts_)
    moves = {m["book"]: m for m in base["moves"]}
    for m in lmoves:
        if m["book"] in moves:
            prev = moves[m["book"]]
            moves[m["book"]] = {**m, "from": prev["from"], "delta": round(m["to"] - prev["from"], 2), "why": prev["why"] + "; " + m["why"]}
        else:
            moves[m["book"]] = m
    moves = [m for m in moves.values() if abs(m["delta"]) >= 1]
    for m in moves:
        m["to"] = new[m["book"]]
        m["delta"] = round(m["to"] - m["from"], 2)
    return {**base, "suggested": new, "moves": sorted(moves, key=lambda m: m["delta"]), "ladder_rule": LADDER_RULE,
            "verdicts": {b: v.get("status") for b, v in verdicts_.items()},
            "changed": any(abs(new[b] - base["current"][b]) >= 1 for b in base["current"])}
