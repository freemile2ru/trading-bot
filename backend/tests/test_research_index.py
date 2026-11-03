"""The research shelf: every entry well-formed; findings read from results files where they exist."""
from experiments import research_index as ri


def test_shelf_entries_are_complete_and_readable():
    rows = ri.shelf()
    assert len(rows) >= 12
    for r in rows:
        assert r["title"] and r["question"] and r["decision"] and isinstance(r["finding"], str) and r["finding"]
        assert r["status"] in ("promoted", "rejected", "mixed", "pending")
        if r["status"] == "pending":
            assert r["file"] is None and r["ran_at"] is None
    by = {r["key"]: r for r in rows}
    assert by["vrp"]["status"] == "promoted" and by["vrp_regime"]["status"] == "rejected"
    # The results files live in the research environment (data/research_cache, not in git): where they exist the
    # findings carry numbers, not placeholders. CI has none, so this part runs only when they are on disk.
    if any(r["ran_at"] for r in rows):
        for k in ("stall_exit", "vrp_regime", "weekly_funding", "funding_reversal", "monday", "vrp"):
            assert by[k]["ran_at"] and "not found" not in by[k]["finding"] and "unreadable" not in by[k]["finding"], (k, by[k]["finding"])
    # newest first
    dated = [r["ran_at"] for r in rows if r["ran_at"]]
    assert dated == sorted(dated, reverse=True)
