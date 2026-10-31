"""Position manager — periodically reviews open trades and scales them in or out.

Every POSITION_MANAGER_INTERVAL seconds per trade, using the scanner's fresh data for
that coin, it decides one of:

  reduce  — adverse de-risk:  price ≥0.6R against, fast timeframe against, conviction gone
            → cut half. Smaller position into a likely stop.
  reduce  — thesis fading:    in profit but conviction has faded → bank half.
  reduce  — overextended:     ≥1R in favour and the fast timeframe is stretched → trim a third.
  add     — pyramid:          ≥1R in favour, conviction and fast timeframe still agree, not
            stretched → add 50% (then 25%) of the initial size, and raise the stop so the
            whole position still loses no more than the original 1R if it's hit.
  add     — average down:     0.3–0.6R against, conviction strong, fast timeframe has turned
            back → add once, sized so the loss at the unchanged stop stays ≤ MAX_RISK_R.
  hold    — anything else, with the reason recorded on the trade.

The rules are asymmetric on purpose. Adding to winners keeps total risk at 1R; adding to
losers is the one move that turns a bounded loss into an unbounded one, so it's allowed
once, only on a confirmed bounce, and hard-capped. Reductions never take a position below
MIN_REMAINING of its initial size — exiting fully is the stop's and the flip rule's job.
"""

import asyncio
import logging
import os
import time

from autotrader import engine as autotrader
from autotrader import strategy
from scanner import bus
from trading import futures
from trading.paper_trader import PaperWallet, main_wallet

logger = logging.getLogger("position_manager")

ENABLED = os.getenv("POSITION_MANAGER_ENABLED", "true").lower() != "false"
WALLETS = [w.strip() for w in os.getenv("POSITION_MANAGER_WALLETS", "auto").split(",") if w.strip()]
INTERVAL = int(os.getenv("POSITION_MANAGER_INTERVAL", "300"))          # seconds between reviews per trade
MIN_GAP = int(os.getenv("POSITION_MANAGER_MIN_GAP", "1800"))           # seconds between adjustments per trade
AVERAGE_DOWN = os.getenv("POSITION_MANAGER_AVERAGE_DOWN", "true").lower() != "false"
MAX_RISK_R = float(os.getenv("POSITION_MANAGER_MAX_RISK_R", "1.5"))    # worst case at the stop, after averaging down

MAX_PYRAMIDS = 2
PYRAMID_SIZES = (0.50, 0.25)      # × initial quantity, first and second add
AVG_DOWN_SIZE = 0.50
MIN_REMAINING = 0.25              # never reduce below this × initial quantity
STOP_BUFFER_ATR = 0.75            # a raised stop must stay this many ATRs from price

_BULL, _BEAR = {"buy", "strong_buy"}, {"sell", "strong_sell"}


def _wallets() -> dict[str, PaperWallet]:
    """Configured wallets — each with its live ledger, which is managed the same way."""
    from live import swing as live_swing
    available = {"auto": autotrader.auto_wallet, "main": main_wallet,
                 "auto_live": live_swing.LIVE["auto"], "main_live": live_swing.LIVE["main"]}
    return {name: w for name, w in available.items() if name.removesuffix("_live") in WALLETS}


# ── Decision (pure) ─────────────────────────────────────────────────────────────

def _metrics(trade: dict, price: float) -> dict:
    s = 1 if trade["direction"] == "long" else -1
    initial_entry = trade.get("initial_entry_price", trade["entry_price"])
    initial_stop = trade.get("original_stop_loss", trade["stop_loss"])
    unit_risk = abs(initial_entry - initial_stop) or initial_entry * 0.01
    init_risk = trade.get("risk_usd") or unit_risk * trade.get("initial_quantity", trade["quantity"])
    open_pnl = (price - trade["entry_price"]) * s * trade["quantity"] + trade.get("realized_pnl_usd", 0.0)
    adj = trade.get("adjustments", [])
    return {
        "s": s,
        "move_r": (price - initial_entry) * s / unit_risk,     # how far price has travelled, in R
        "open_r": open_pnl / init_risk if init_risk else 0.0,  # what the position is worth, in R
        "init_risk": init_risk,
        "initial_qty": trade.get("initial_quantity", trade["quantity"]),
        "pyramids": sum(1 for a in adj if a["action"] == "add" and a.get("kind") != "average_down"),
        "avg_downs": sum(1 for a in adj if a.get("kind") == "average_down"),
        "reduces": sum(1 for a in adj if a["action"] == "reduce"),
        "last_adjusted": max((a["ts"] for a in adj), default=0.0),
    }


def _loss_at_stop(avg: float, qty: float, stop: float, s: int, realized: float) -> float:
    return (avg - stop) * s * qty - realized


def _pyramid(trade: dict, price: float, m: dict, atr_pct: float, gross_room_pct: float, balance: float) -> dict | None:
    """Largest add (from the planned size, halving twice) that keeps total risk ≤ 1R."""
    s, realized = m["s"], trade.get("realized_pnl_usd", 0.0)
    buffer = price * atr_pct / 100 * STOP_BUFFER_ATR
    fill = futures.slipped(price, trade["direction"], opening=True)   # what the add will actually cost
    planned = PYRAMID_SIZES[min(m["pyramids"], len(PYRAMID_SIZES) - 1)] * m["initial_qty"]
    for qty in (planned, planned / 2, planned / 4):
        if qty * price / balance * 100 > gross_room_pct:
            continue
        total = trade["quantity"] + qty
        avg = (trade["entry_price"] * trade["quantity"] + fill * qty) / total
        # Stop at which the whole position loses exactly the original 1R.
        required = avg - s * (m["init_risk"] + realized) / total
        stop = max(trade["stop_loss"], required) if s > 0 else min(trade["stop_loss"], required)
        if (price - stop) * s >= buffer:
            return {"quantity": qty, "new_stop": round(stop, 8)}
    return None


def _average_down(trade: dict, price: float, m: dict, gross_room_pct: float, balance: float) -> float | None:
    s, realized, stop = m["s"], trade.get("realized_pnl_usd", 0.0), trade["stop_loss"]
    cap = MAX_RISK_R * m["init_risk"]
    fill = futures.slipped(price, trade["direction"], opening=True)
    planned = AVG_DOWN_SIZE * m["initial_qty"]
    for qty in (planned, planned / 2, planned / 4):
        if qty * price / balance * 100 > gross_room_pct:
            continue
        total = trade["quantity"] + qty
        avg = (trade["entry_price"] * trade["quantity"] + fill * qty) / total
        if _loss_at_stop(avg, total, stop, s, realized) <= cap:
            return qty
    return None


def decide(trade: dict, scan: dict, now: float, gross_room_pct: float, balance: float) -> dict:
    """Returns {"action": "add"|"reduce"|"hold", "reason", ...}."""
    price = scan["price"]
    m = _metrics(trade, price)
    s = m["s"]
    _, fast = strategy.fast_tf(scan)
    conv = (scan.get("conviction") or 0.0) * s                      # >0 = agrees with the position
    label = fast.get("signal")
    fast_with = label in (_BULL if s > 0 else _BEAR)
    fast_against = label in (_BEAR if s > 0 else _BULL)
    rsi = fast.get("rsi") or 50
    stretched = (rsi >= 75 if s > 0 else rsi <= 25) or fast.get("bb_position") == ("overbought" if s > 0 else "oversold")
    atr_pct = max((scan.get("triggers") or {}).get("atr_pct") or 1.0, 0.1)
    ctx = f"{m['move_r']:+.2f}R move, position {m['open_r']:+.2f}R, conviction {conv:+.2f}"

    if now - m["last_adjusted"] < MIN_GAP:
        return {"action": "hold", "reason": f"adjusted {int((now - m['last_adjusted']) / 60)}m ago — spacing out changes ({ctx})"}

    floor = MIN_REMAINING * m["initial_qty"]
    reducible = max(0.0, trade["quantity"] - floor)

    # ── Reductions first: protecting capital outranks adding to it ──
    if m["move_r"] <= -0.6 and fast_against and conv < 0.15 and reducible > 0:
        return {"action": "reduce", "quantity": min(trade["quantity"] * 0.5, reducible), "kind": "de_risk",
                "reason": f"de-risk: price against and signal turned ({ctx})"}
    if m["open_r"] >= 0.5 and conv < 0.10 and reducible > 0:
        return {"action": "reduce", "quantity": min(trade["quantity"] * 0.5, reducible), "kind": "bank",
                "reason": f"bank half: conviction faded while in profit ({ctx})"}
    if m["move_r"] >= 1.0 and stretched and m["reduces"] < 2 and reducible > 0:
        return {"action": "reduce", "quantity": min(trade["quantity"] / 3, reducible), "kind": "trim",
                "reason": f"trim a third into overextension, RSI {rsi:.0f} ({ctx})"}

    # ── Additions ──
    if m["move_r"] >= 1.0 and conv >= 0.25 and fast_with and not stretched and m["pyramids"] < MAX_PYRAMIDS:
        add = _pyramid(trade, price, m, atr_pct, gross_room_pct, balance)
        if add:
            return {"action": "add", "kind": "pyramid", **add,
                    "reason": f"pyramid #{m['pyramids'] + 1}: trend intact, stop raised so total risk stays 1R ({ctx})"}
        return {"action": "hold", "reason": f"trend intact but no room to add within 1R risk ({ctx})"}

    bounced = fast_with and ((fast.get("macd_histogram") or 0) * s > 0)
    # Adding to a loser is only defensible when the fundamentals aren't pushing against it.
    if (AVERAGE_DOWN and -0.6 < m["move_r"] <= -0.3 and conv >= 0.35 and bounced
            and m["avg_downs"] == 0 and m["pyramids"] == 0):
        qty = _average_down(trade, price, m, gross_room_pct, balance)
        if qty:
            return {"action": "add", "kind": "average_down", "quantity": qty,
                    "reason": f"average down once on a confirmed bounce; worst case capped at {MAX_RISK_R}R ({ctx})"}

    return {"action": "hold", "reason": f"no change — {ctx}"}


# ── Execution ───────────────────────────────────────────────────────────────────

def added_risk_usd(trade: dict, quantity: float, price: float, new_stop: float | None) -> float:
    """How much an add raises the position's loss-at-stop (what portfolio heat must absorb)."""
    s = 1 if trade["direction"] == "long" else -1
    realized = trade.get("realized_pnl_usd", 0.0)
    now = max(_loss_at_stop(trade["entry_price"], trade["quantity"], trade["stop_loss"], s, realized), 0.0)
    total = trade["quantity"] + quantity
    fill = futures.slipped(price, trade["direction"], opening=True)
    avg = (trade["entry_price"] * trade["quantity"] + fill * quantity) / total
    stop = trade["stop_loss"] if new_stop is None else new_stop
    return max(_loss_at_stop(avg, total, stop, s, realized), 0.0) - now


async def _review_wallet(name: str, wallet: PaperWallet, scans: dict[str, dict], now: float) -> int:
    """Returns how many trades were adjusted."""
    changed = 0
    portfolio = wallet.get_portfolio()
    balance = portfolio["balance"]
    gross = sum(t.get("size_pct", 0) for t in portfolio["open_trades"])
    if name.startswith("auto"):
        gross += sum(o.get("size_pct", 0) for o in wallet.get_orders()["pending"])

    for trade in portfolio["open_trades"]:
        scan = scans.get(trade["symbol"])
        last = (trade.get("last_review") or {}).get("ts", 0)
        if not scan or not scan.get("price") or now - last < INTERVAL:
            continue
        if trade.get("exit_policy") == "manual" or trade.get("lane") not in (None, "ta") or trade.get("event_id"):
            continue                        # book-managed positions (mirrors, trend, weekly): the book sizes them, not the scan

        d = decide(trade, scan, now, strategy.MAX_GROSS_PCT - gross, balance)
        if d["action"] == "add" and name.startswith("auto"):
            # Portfolio heat: an add must fit under the wallet-wide open-risk cap.
            room_usd = autotrader._heat_room_pct(wallet) / 100 * (portfolio["total_equity"] or balance)
            extra = added_risk_usd(trade, d["quantity"], scan["price"], d.get("new_stop"))
            if extra > room_usd:
                d = {"action": "hold", "reason": f"would add ${extra:.2f} risk; portfolio open-risk cap "
                                                  f"leaves ${max(room_usd, 0):.2f} — {d['reason']}"}
        review = {"ts": now, "action": d["action"], "reason": d["reason"]}

        if d["action"] in ("add", "reduce"):
            adjusted = await asyncio.to_thread(
                wallet.adjust_trade, trade["id"], d["action"], scan["price"], d["quantity"],
                d["reason"], d.get("new_stop"), d.get("kind"),
            )
            if not adjusted and d["action"] == "add":
                review.update(action="hold", reason=f"not enough free margin to add — {d['reason']}")
            if adjusted:
                changed += 1
                gross += (adjusted["size_pct"] - trade.get("size_pct", 0))
                logger.info("Position %s %s %s: %s %.6g @ %s — %s", name, trade["symbol"], trade["direction"],
                            d["action"], d["quantity"], scan["price"], d["reason"])
                bus.publish("auto_trade" if name == "auto" else "trade_adjusted",
                            {"event": "adjusted", "wallet": name, "trade": adjusted, "decision": d})

        await asyncio.to_thread(wallet.annotate, trade["id"], last_review=review)
    return changed


async def review(scans: list[dict]) -> None:
    """Hook called by the scheduler after each sweep; each trade is reviewed at most every INTERVAL."""
    if not ENABLED or not scans:
        return
    by_symbol = {s["symbol"]: s for s in scans}
    now = time.time()
    for name, wallet in _wallets().items():
        try:
            if name.startswith("auto"):
                # Serialise with the autotrader, which fills and closes this wallet's trades.
                async with autotrader._lock:
                    changed = await _review_wallet(name, wallet, by_symbol, now)
                if changed:
                    bus.publish("auto", await autotrader.summary())
            else:
                await _review_wallet(name, wallet, by_symbol, now)
        except Exception as e:
            logger.warning("Position review failed for %s wallet: %s", name, e)


def config() -> dict:
    return {
        "enabled": ENABLED, "wallets": list(_wallets()), "interval": INTERVAL, "min_gap": MIN_GAP,
        "average_down": AVERAGE_DOWN, "max_risk_r": MAX_RISK_R,
    }
