import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from news import classify, collector, scoring, sources, store

NOW = 1_790_700_000.0

RSS = b"""<?xml version="1.0"?><rss><channel>
<item><title>Solana ETF approved &amp; trading</title><link>https://x/1</link>
  <pubDate>Tue, 29 Sep 2026 18:00:00 +0000</pubDate><description>&lt;p&gt;SEC signs off&lt;/p&gt;</description></item>
<item><title>Bare & ampersand headline</title><link>https://x/2</link><pubDate>Tue, 29 Sep 2026 17:00:00 GMT</pubDate></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Aave v4 ships</title><link rel="alternate" href="https://b/1"/><updated>2026-09-29T20:13:19.245Z</updated>
<summary>Upgrade live</summary></entry></feed>"""


def test_feeds_parse_to_utc_and_survive_a_bare_ampersand():
    items = sources.parse_feed(RSS, "Test")
    assert [i["title"] for i in items] == ["Solana ETF approved & trading", "Bare & ampersand headline"]
    assert items[0]["published"] == pytest.approx(1790704800.0) and items[0]["summary"] == "SEC signs off"
    atom = sources.parse_feed(ATOM, "Blockworks")
    assert atom[0]["url"] == "https://b/1" and atom[0]["published"] == pytest.approx(1790712799.245)
    assert sources.parse_date("2026-09-29T18:00:00") == pytest.approx(1790704800.0)     # naive = UTC


def test_google_titles_lose_the_outlet_suffix():
    assert sources.strip_publisher("Solana hits record - CoinDesk", "CoinDesk") == "Solana hits record"


def test_binance_announcements_parse_with_millisecond_dates():
    data = {"data": {"catalogs": [{"articles": [{"title": "Binance Will Delist FOO", "code": "abc",
                                                 "releaseDate": 1790700000000}]}]}}
    it = sources.parse_binance(data, "Binance delistings")[0]
    assert it["published"] == 1790700000.0 and it["url"].endswith("/abc")


def test_date_rules_drop_stale_and_redate_future_items():
    assert collector.accept({"title": "old", "published": NOW - 49 * 3600}, NOW) is None
    fut = collector.accept({"title": "scheduled", "published": NOW + 3600}, NOW)
    assert fut["published"] is None and fut["seen"] == NOW
    ok = collector.accept({"title": "fresh", "published": NOW - 600}, NOW)
    assert ok["published"] == NOW - 600


def test_same_story_from_two_outlets_is_one_item_with_both_sources():
    a = {"title": "Binance will list FOO", "source": "CoinDesk", "url": "u1", "published": NOW, "seen": NOW}
    b = {"title": "Binance Will List FOO!", "source": "Google News", "publisher": "Decrypt", "url": "u2",
         "published": NOW, "seen": NOW + 60}
    assert len(store.add([a])) == 1 and store.add([b]) == []
    it = store.items(0)[0]
    assert {s["source"] for s in it["sources"]} == {"CoinDesk", "Decrypt"} and it["seen"] == NOW


def test_mentions_match_names_and_capital_tickers_only():
    match = scoring.mention_matcher({"SOL": "Solana", "NEAR": "NEAR Protocol", "OP": "Optimism"})
    assert match("Solana rallies as $SOL flips resistance") == ["SOL"]
    assert match("the deal is near completion") == []                  # lowercase word, not the ticker
    assert match("OP Stack upgrade") == []                              # 2-letter ticker never matches


def _item(**kw):
    base = {"id": kw.pop("id", "x"), "title": "t", "seen": NOW, "published": NOW, "classified_at": NOW,
            "scope": "coin", "coins": ["SOL"], "direction": 1.0, "importance": 4, "sources": [], "mentions": []}
    return {**base, **kw}


def test_scores_weight_importance_and_decay_with_age():
    fresh = scoring.compute([_item()], [], NOW)["coins"]["SOL"]
    stale = scoring.compute([_item(published=NOW - 12 * 3600, seen=NOW - 12 * 3600)], [], NOW)["coins"]["SOL"]
    trivial = scoring.compute([_item(importance=1)], [], NOW)["coins"]["SOL"]
    assert fresh["coin_score"] == pytest.approx(0.834, abs=0.01)
    assert 0 < stale["coin_score"] < fresh["coin_score"] / 2        # two half-lives later
    assert trivial["coin_score"] < 0.15


def test_market_news_moves_every_coin_and_irrelevant_news_moves_nothing():
    out = scoring.compute([_item(id="m", scope="market", coins=[], direction=-1.0, importance=5),
                           _item(id="i", scope="irrelevant", importance=5)], [], NOW)
    assert out["market"]["score"] < -0.6 and out["market"]["stories"] == 1 and out["coins"] == {}


def test_unclassified_items_count_at_the_lowest_weight_with_halved_vader():
    rd = scoring.reading({"classified_at": None, "mentions": ["SOL"], "vader": 0.8})
    assert rd == {"coins": ["SOL"], "scope": "coin", "direction": 0.4, "importance": 1, "provisional": True}


def test_burst_compares_the_last_six_hours_with_the_week_before():
    history = ([{"seen": NOW - 3600, "coins": ["SOL"], "story": f"r{i}"} for i in range(9)]
               + [{"seen": NOW - 3 * 86400, "coins": ["SOL"], "story": f"b{i}"} for i in range(28)])
    burst = scoring.compute([], history, NOW, history_since=NOW - 7 * 86400)["coins"]["SOL"]
    assert burst["stories_6h"] == 9 and burst["burst"] == pytest.approx(5.0)
    # A day into collection there's no baseline: every coin would read as a huge burst.
    assert scoring.compute([], history, NOW, history_since=NOW - 86400)["coins"]["SOL"]["burst"] is None


def test_per_coin_search_results_need_crypto_context_unless_the_name_is_distinctive():
    assert not scoring.crypto_context("Coronation Street spoilers as casualty found", "DASH", "Dash")
    assert not scoring.crypto_context("S&P 500 Movers | Winners: PSKY, CRWD | Losers: DASH", "DASH", "Dash")
    assert scoring.crypto_context("Dash coin jumps on privacy demand", "DASH", "Dash")
    assert scoring.crypto_context("Hedera jumps 24% on IBM news", "HBAR", "Hedera")
    match = scoring.mention_matcher({"DASH": "Dash"})
    assert match("a dash of hope") == [] and match("DASH rallies") == ["DASH"]


def test_classification_validates_answers_and_marks_skipped_items(monkeypatch):
    now = time.time()
    store.add([{"title": "Binance Will Delist FOO", "source": "Binance delistings", "seen": now, "published": now},
               {"title": "Some listicle", "source": "Decrypt", "seen": now, "published": now}])
    first, second = store.pending(10)
    assert first["source"] == "Binance delistings"                   # urgent source first
    assert classify.due(now)

    async def create(**kw):
        body = [{"id": first["id"], "coins": ["FOO", "NOTLISTED"], "scope": "coin", "event_type": "delisting",
                 "direction": -3, "importance": 9, "note": "delisting"}]
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(body))], stop_reason="end_turn")
    from oracle import claude_oracle
    monkeypatch.setattr(claude_oracle, "get_client", lambda: SimpleNamespace(messages=SimpleNamespace(create=create)))
    assert asyncio.run(classify.run({"FOO": "Foo"})) == 2
    got = {i["title"]: i for i in store.items(0)}
    d = got["Binance Will Delist FOO"]
    assert d["coins"] == ["FOO"] and d["direction"] == -1.0 and d["importance"] == 5
    assert got["Some listicle"]["scope"] == "irrelevant"
    assert classify.calls_today() == 1 and not classify.due()


def test_news_feeds_the_signal_package_and_claudes_brief(monkeypatch):
    from analysis.scoring import build_signal_package, score_news_sentiment
    from data import cache, news_client

    async def no_redis():
        return None
    monkeypatch.setattr(cache, "get_client", no_redis)          # never write the app's real cache
    store.add([{"title": "Solana ETF approved", "source": "CoinDesk", "seen": time.time(), "published": time.time()}])
    store.save_labels({store.item_id("Solana ETF approved"): {"coins": ["SOL"], "scope": "coin",
                       "event_type": "etf_institutional", "direction": 0.9, "importance": 5, "note": "big"}})

    async def no_cp(*a, **k):
        return []
    monkeypatch.setattr(news_client, "fetch_cryptopanic", no_cp)
    news = asyncio.run(news_client.fetch_all_news("SOLUSDT"))
    assert news["scraped"]["score"] > 0.5 and score_news_sentiment(news) == news["aggregate_sentiment"]
    pkg = build_signal_package("SOLUSDT", {"timeframes": {}}, news, {}, {"value": 50}, {}, {})
    assert pkg["news_summary"]["key_events"][0]["event_type"] == "etf_institutional"
    assert pkg["component_scores"]["news"] > 0.5                      # reported to Claude and the scorer…
    assert pkg["component_scores"]["sentiment"] == 0.0                # …but not scored (NEWS_IN_SCORE off)


def test_one_story_counts_once_however_many_headlines_cover_it():
    one = scoring.compute([_item(id="a", story="s1")], [], NOW)["coins"]["SOL"]
    many = scoring.compute([_item(id=f"a{i}", story="s1") for i in range(12)], [], NOW)["coins"]["SOL"]
    two = scoring.compute([_item(id="a", story="s1"), _item(id="b", story="s2")], [], NOW)["coins"]["SOL"]
    assert many["coin_score"] == one["coin_score"] and many["stories"] == 1
    assert many["top"][0]["similar"] == 11
    assert two["coin_score"] > one["coin_score"] and two["stories"] == 2


def test_conflicting_readings_of_one_story_average_out():
    out = scoring.compute([_item(id="a", story="s", direction=1.0), _item(id="b", story="s", direction=-1.0)], [], NOW)
    assert out["coins"]["SOL"]["coin_score"] == pytest.approx(0.0, abs=1e-9)


def test_reworded_headlines_join_the_same_story_at_intake():
    a = {"title": "SEC sues Cryptoaiml and TSAI over alleged $15M AI trading scams", "source": "Crypto Briefing",
         "seen": NOW, "published": NOW}
    b = {"title": "SEC Sues Cryptoaiml and TSAI Over Alleged $15 Million AI Trading Scams", "source": "The Defiant",
         "seen": NOW + 60, "published": NOW}
    c = {"title": "SEC approves new rules for exchange-traded products", "source": "SEC", "seen": NOW + 90, "published": NOW}
    ra, rb, rc = store.add([a])[0], store.add([b])[0], store.add([c])[0]
    assert ra["id"] != rb["id"] and rb["story"] == ra["id"] and rc["story"] == rc["id"]


def test_claude_links_a_story_only_to_ids_it_was_shown():
    known = {"s1": "s1"}
    assert classify.resolve_story({"id": "x", "story": "x"}, {"same_story_as": "s1"}, known) == "s1"
    assert classify.resolve_story({"id": "x", "story": "x"}, {"same_story_as": "made-up"}, known) == "x"
    assert classify.resolve_story({"id": "x", "story": "j"}, {"same_story_as": None}, known) == "j"


# ── Mood, App headlines, grouped feed ───────────────────────────────────────────

def test_mood_renormalises_over_available_components_and_labels_the_result():
    from news import mood
    m = mood.combine({"news": {"score": 0.6}, "claude": {"score": 0.6}, "regime": {"score": None}, "fa": {"score": -1.0}})
    # FA is context only (weight 0 since 2026-10-08): (0.35·0.6 + 0.30·0.6) / 0.65 — its −1 changes nothing
    assert m["score"] == pytest.approx(0.6, abs=1e-3) and m["label"] == "Risk-on"
    assert m["agreement"] == "2 of 2 point the same way"
    assert m["components"]["fa"]["weight"] == 0 and m["components"]["fa"]["score"] == -1.0   # still shown
    assert mood.label(-0.5) == "Risk-off" and mood.label(0.0) == "Neutral / mixed"


def _inp(label="Neutral / mixed", stories=(), positions=()):
    return {"mood": {"label": label, "score": 0.0, "agreement": "", "components": {}, "fear_greed": None},
            "stories": list(stories), "positions": list(positions), "signals": [], "coin_news": []}


def test_editions_are_written_on_change_or_important_news_not_on_every_tick(monkeypatch):
    from news import brief
    now = time.time()
    assert brief.due(_inp(), now) == "first"
    store.save_brief("first", {}, {"label": "x"}, [{"title": "t"}], brief.inputs_key(_inp()))
    later = now + brief.EVERY + 5
    assert brief.due(_inp(), later) is None                                   # nothing changed
    assert brief.due(_inp(label="Risk-off"), later) == "scheduled"            # the picture changed
    fresh = {"story": "s", "importance": 5, "age_h": 0.05}
    assert brief.due(_inp(stories=[fresh]), now + brief.MIN_GAP + 5) == "important"
    assert brief.due(_inp(stories=[fresh]), now + 60) is None                 # too soon after the last one
    assert brief.due(_inp(), now + 60, manual=True) is None
    assert brief.due(_inp(), now + brief.MANUAL_GAP + 1, manual=True) == "manual"


def test_editions_page_back_and_forth():
    a = store.save_brief("first", {}, {"label": "A"}, [{"title": "one"}], "k1")
    b = store.save_brief("scheduled", {}, {"label": "B"}, [{"title": "two"}], "k2")
    latest = store.brief()
    assert latest["id"] == b and latest["prev_id"] == a and latest["latest"]
    old = store.brief(a)
    assert old["next_id"] == b and old["prev_id"] is None and not old["latest"]
    assert [e["lead"] for e in store.briefs()] == ["two", "one"]


def test_edition_answers_are_validated_against_the_inputs():
    from news import brief
    inp = _inp(stories=[{"story": "s1", "importance": 4}])
    verdict, heads = brief.validate({"verdict": {"label": "Moon", "summary": "x"},
                                     "headlines": [{"title": "T", "stance": "yolo", "stories": ["s1", "made-up"], "coins": ["sol"]},
                                                   {"body": "no title"}]}, inp)
    assert verdict["label"] == "Neutral / mixed"                               # unknown label → the gauge's
    assert heads == [{"title": "T", "body": "", "stance": "neutral", "relevance": "market", "coins": ["SOL"], "stories": ["s1"]}]


def test_feed_groups_a_story_and_sorts(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import main
    now = time.time()
    store.add([{"title": "SEC sues Cryptoaiml and TSAI over alleged $15M AI trading scams", "source": "A", "seen": now - 300, "published": now - 300},
               {"title": "SEC Sues Cryptoaiml and TSAI Over Alleged $15 Million AI Trading Scams", "source": "B", "seen": now - 200, "published": now - 200},
               {"title": "Binance will list FOO perpetual", "source": "Binance listings", "seen": now - 100, "published": now - 100}])
    ids = {i["title"]: i["id"] for i in store.items(0)}
    store.save_labels({i: {"coins": [], "scope": "market", "event_type": "enforcement_legal", "direction": -0.2, "importance": 2}
                       for t, i in ids.items() if "SEC" in t})
    store.save_labels({ids["Binance will list FOO perpetual"]: {"coins": ["FOO"], "scope": "coin", "event_type": "listing",
                                                                 "direction": 0.6, "importance": 4}})
    c = TestClient(main.app)
    grouped = c.get("/api/news").json()["items"]
    assert len(grouped) == 2 and grouped[0]["title"].startswith("Binance")      # newest first
    sec = grouped[1]
    assert sec["title"].startswith("SEC sues") and len(sec["similar"]) == 1      # first reported leads
    flat = c.get("/api/news?group=false&sort=importance").json()["items"]
    assert len(flat) == 3 and flat[0]["importance"] == 4
    assert c.get("/api/news?sort=bogus").status_code == 422


def test_consolidation_merges_one_event_split_across_stories(monkeypatch):
    now = time.time()
    store.add([{"title": "The Bitwise NEAR ETF (NRR) launches in the U.S.", "source": "A", "seen": now - 600, "published": now - 600},
               {"title": "America gets its first spot fund for NEAR as trading starts", "source": "B", "seen": now - 300, "published": now - 300},
               {"title": "Aave v4 goes live on mainnet", "source": "C", "seen": now - 200, "published": now - 200}])
    ids = {i["title"]: i["id"] for i in store.items(0)}
    store.save_labels({i: {"coins": ["NEAR"] if "NEAR" in t else ["AAVE"], "scope": "coin", "event_type": "etf_institutional",
                           "direction": 0.7, "importance": 4} for t, i in ids.items()})
    near_a, near_b = ids["The Bitwise NEAR ETF (NRR) launches in the U.S."], ids["America gets its first spot fund for NEAR as trading starts"]

    async def create(**kw):
        body = [[near_b, near_a], ["made-up", ids["Aave v4 goes live on mainnet"]]]
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(body))], stop_reason="end_turn")
    from oracle import claude_oracle
    monkeypatch.setattr(claude_oracle, "get_client", lambda: SimpleNamespace(messages=SimpleNamespace(create=create)))
    assert asyncio.run(classify.consolidate(force=True)) == 1
    stories = {i["title"]: i["story"] for i in store.items(0)}
    assert stories["America gets its first spot fund for NEAR as trading starts"] == near_a      # the first reported keeps its id
    assert stories["Aave v4 goes live on mainnet"] == ids["Aave v4 goes live on mainnet"]   # invented ids are ignored
    assert asyncio.run(classify.consolidate()) == 0                                      # not due again yet
