"""Persist oracle signal results so accuracy can be tracked over time."""

import json
import os
import threading
from datetime import datetime, timezone

HISTORY_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "signal_history.json")
MAX_ENTRIES = 100
_lock = threading.Lock()


def _load() -> list[dict]:
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE) as f:
            return json.load(f)
    return []


def _save(entries: list[dict]) -> None:
    os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    tmp = HISTORY_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(entries, f, indent=2, default=str)
    os.replace(tmp, HISTORY_FILE)


def save_signal(pipeline_result: dict) -> None:
    """Store a condensed snapshot of an oracle result."""
    pkg = pipeline_result.get("signal_package", {})
    oracle = pipeline_result.get("oracle", {})
    rec = oracle.get("trade_recommendation", {})
    status = pipeline_result.get("pipeline_status", {})

    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "symbol": pipeline_result.get("symbol"),
        "price": pkg.get("price"),
        "action": rec.get("action"),
        "direction": rec.get("direction"),
        "confidence": rec.get("confidence"),
        "timeframe": rec.get("timeframe"),
        "entry_price": rec.get("entry_price"),
        "stop_loss": rec.get("stop_loss"),
        "take_profit_1": rec.get("take_profit_1"),
        "composite_score": pkg.get("composite_score"),
        "elapsed_seconds": status.get("elapsed_seconds"),
        "sources_ok": sum(1 for v in status.get("data_sources", {}).values() if v.get("ok") is True),
        "sources_total": len(status.get("data_sources", {})),
        "primary_thesis": oracle.get("reasoning", {}).get("primary_thesis", ""),
    }

    with _lock:
        entries = _load()
        entries.append(entry)
        if len(entries) > MAX_ENTRIES:
            entries = entries[-MAX_ENTRIES:]
        _save(entries)


def get_history(symbol: str | None = None, limit: int = 20) -> list[dict]:
    with _lock:
        entries = _load()

    if symbol:
        entries = [e for e in entries if e.get("symbol") == symbol.upper()]

    return list(reversed(entries[-limit:]))
