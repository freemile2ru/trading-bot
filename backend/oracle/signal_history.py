"""Persist oracle signal results so accuracy can be tracked over time.

Stored in signals.db (storage/signals.py), uncapped. This used to be data/signal_history.json,
which kept only the last 100 entries; that file is imported once by storage/migrate.py.
"""

from datetime import datetime, timezone

from storage import signals


def summarize(pipeline_result: dict) -> dict:
    """A condensed snapshot of an oracle result."""
    pkg = pipeline_result.get("signal_package", {})
    oracle = pipeline_result.get("oracle", {})
    rec = oracle.get("trade_recommendation", {})
    status = pipeline_result.get("pipeline_status", {})
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "symbol": pipeline_result.get("symbol"),
        "source": pipeline_result.get("source", "oracle"),
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


def save_signal(pipeline_result: dict) -> None:
    signals.add_oracle_signal(summarize(pipeline_result))


