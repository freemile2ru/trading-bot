"""The subscription tiers — the one catalogue the gates, the runner and the Plans tab read (2026-11-20).

Prices are the user's choice (Free / Pro $49 / Elite $199 a month). Features are keys; the API gates on them
(tenancy/gate.py), the frontend hides or locks tabs by them, the runner mirrors only the books a tier allows.
"""
from __future__ import annotations

import os

FREE, PRO, ELITE = "free", "pro", "elite"
ORDER = (FREE, PRO, ELITE)

# key → (label, group, note)
FEATURES: dict[str, tuple[str, str, str | None]] = {
    "signals.delayed": ("Signals, 15 minutes delayed", "Signals", "the fired-signal feed, delayed"),
    "signals.live": ("Live signals and the stream", "Signals", "every oracle read as it happens"),
    "news": ("News desk", "Signals", None),
    "calendar": ("Economic and earnings calendar", "Signals", None),
    "trades": ("Trades, History, Portfolio and Books", "Trading", "your own books' record"),
    "markets": ("Stock and commodity perps", "Trading", None),
    "paper.mirror": ("Your own paper books mirroring the engines", "Books", "sized to your capital at the same risk"),
    "copy.view": ("The copy-trading board", "Books", "the leaders and what was copied"),
    "alerts.email": ("Email alerts (coming soon)", "Alerts", "per-account delivery is not built yet"),
    "alerts.whatsapp": ("WhatsApp alerts (coming soon)", "Alerts", "per-account delivery is not built yet"),
    "alerts.webhook": ("Webhook alerts (coming soon)", "Alerts", "to your own endpoint; not built yet"),
    "mentor": ("Mentor's auction system", "Pro tools", None),
    "prop": ("Prop-firm book", "Pro tools", None),
    "one": ("The one-book composite", "Pro tools", None),
    "control": ("Control data (what-if replays)", "Pro tools", None),
    "lab": ("Research labs", "Pro tools", None),
    "retired": ("Retired experiments and their verdicts", "Pro tools", None),
    "claude.second_look": ("Claude's second look on your legs (coming soon)", "Pro tools", "not built yet"),
    "mirror.exchange": ("Mirroring to your own exchange keys (coming soon)", "Access", "keys are stored now; no orders are placed yet"),
    "api.access": ("API access (coming soon)", "Access", "60 requests a minute; no keys or quota yet"),
    "engines.own": ("Run the engines on your own account", "Access", "your own watchlist, switches and positions; behind a flag"),
}
GROUPS = ("Signals", "Trading", "Books", "Alerts", "Pro tools", "Access")

# Books a tenant's own wallets may mirror (PaperWallet-based engine books). Pro picks up to `paper_books` of CORE.
CORE_BOOKS = ("auto", "copy_trading", "copy_stocks", "composite", "weekly_rs")
ALL_BOOKS = CORE_BOOKS + ("auction", "prop", "cascade", "burst_up", "burst_dip", "burst_down", "burst_rip", "reversal_index", "copy_futures")
DEFAULT_BOOKS_ON = ("auto", "copy_stocks")

TIERS: dict[str, dict] = {
    FREE: {
        "id": FREE, "name": "Free", "price_usd_month": 0, "highlight": False,
        "tagline": "See what the engines see, a quarter of an hour late.",
        "features": ["signals.delayed", "news", "calendar"],
        "limits": {"paper_books": 0, "paper_capital_usd": 0, "alert_channels": 0, "signal_delay_min": 15, "api_rpm": 0},
        "books": (),
    },
    PRO: {
        "id": PRO, "name": "Pro", "price_usd_month": float(os.getenv("TIER_PRO_USD", "49")), "highlight": True,
        "tagline": "Live signals and your own paper books on the engines' entries.",
        "features": ["signals.delayed", "signals.live", "news", "calendar", "trades", "markets", "paper.mirror", "copy.view",
                     "alerts.email", "alerts.whatsapp"],
        "limits": {"paper_books": 5, "paper_capital_usd": 10_000, "alert_channels": 2, "signal_delay_min": 0, "api_rpm": 0},
        "books": CORE_BOOKS,
    },
    ELITE: {
        "id": ELITE, "name": "Elite", "price_usd_month": float(os.getenv("TIER_ELITE_USD", "199")), "highlight": False,
        "tagline": "Every book, every tool, your own keys and the API.",
        "features": list(FEATURES),
        "limits": {"paper_books": None, "paper_capital_usd": 50_000, "alert_channels": 3, "signal_delay_min": 0, "api_rpm": 60},
        "books": ALL_BOOKS,
    },
}


def tier(name: str | None) -> dict:
    return TIERS.get(name or FREE, TIERS[FREE])


def features(name: str | None) -> list[str]:
    return list(tier(name)["features"])


def has(name: str | None, feature: str) -> bool:
    return feature in tier(name)["features"]


def limits(name: str | None) -> dict:
    return dict(tier(name)["limits"])


def books_allowed(name: str | None) -> tuple[str, ...]:
    return tuple(tier(name)["books"])


def upgrade_for(feature: str) -> str | None:
    """The cheapest tier that includes the feature."""
    for t in ORDER:
        if feature in TIERS[t]["features"]:
            return t
    return None


def catalogue(current: str | None = None, billing: bool = False) -> dict:
    """What the Plans tab renders."""
    return {
        "tiers": [{k: v for k, v in TIERS[t].items() if k != "books"} | {"books": list(TIERS[t]["books"])} for t in ORDER],
        "features": [{"key": k, "label": lab, "group": g, **({"note": n} if n else {})} for k, (lab, g, n) in FEATURES.items()],
        "groups": list(GROUPS),
        "current_tier": current,
        "billing": billing,
    }
