"""Pure parsers for the new data feeds: cross-venue funding rows and Upbit notices."""
from data import funding_venues as fv
from news import sources


def test_funding_rows_match_coins_across_venues_and_scale_hourly_to_8h():
    binance = fv.binance_rates([
        {"symbol": "BTCUSDT", "lastFundingRate": "0.0001", "markPrice": "80000"},
        {"symbol": "1000PEPEUSDT", "lastFundingRate": "-0.0003", "markPrice": "0.01"},
        {"symbol": "BTCUSDT_261226", "lastFundingRate": "0", "markPrice": "81000"},     # dated contract: skipped
        {"symbol": "BTCBUSD", "lastFundingRate": "0", "markPrice": "1"},
    ])
    hl = fv.hyperliquid_rates([{"universe": [{"name": "BTC"}, {"name": "kPEPE"}, {"name": "SOL"}]},
                               [{"funding": "0.0000125", "markPx": "80010"}, {"funding": "-0.00005", "markPx": "0.0101"}, {"funding": "0", "markPx": "150"}]])
    assert set(binance) == {"BTC", "1000PEPE"} and set(hl) == {"BTC", "PEPE", "SOL"}
    rows = fv.rows_for(1_800_000_000, binance, hl)
    by = {r[1]: r for r in rows}
    assert set(by) == {"BTC", "PEPE"}
    assert by["BTC"][2] == 0.0001 and by["BTC"][3] == 0.0001 and by["BTC"][4] == 0.0       # same 8h rate both sides
    assert by["PEPE"][3] == -0.0004 and by["PEPE"][4] == round(-0.0003 - -0.0004, 8)       # hourly ×8, gap = binance − hl


def test_upbit_parser_finds_notices_whatever_the_wrapper_shape():
    reply = {"success": True, "data": {"total_count": 2, "notices": [
        {"id": 123, "title": "[거래] 마켓 디지털 자산 추가 (ABC)", "first_listed_at": "2026-10-29T03:00:00+09:00"},
        {"id": 124, "title": "Market Support for XYZ (KRW, USDT Market)", "listed_at": "2026-10-28T09:00:00+09:00"},
    ]}}
    items = sources.parse_upbit(reply, "Upbit notices")
    assert [i["url"] for i in items] == ["https://upbit.com/service_center/notice?id=123", "https://upbit.com/service_center/notice?id=124"]
    assert items[0]["published"] == sources.parse_date("2026-10-29T03:00:00+09:00") and items[1]["source"] == "Upbit notices"
    assert sources.parse_upbit({"data": {"list": []}}, "Upbit notices") == []
