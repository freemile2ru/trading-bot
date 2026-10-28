"""Fundamentals sources: DefiLlama token matching, research features, the scan recorder."""

from pytest import approx

from data import defillama_client as llama
from scanner.fundamentals import research_features
from storage import signals

PROTOCOLS = [
    {"id": "1", "name": "Aave V3", "symbol": "AAVE", "parentProtocol": "parent#aave", "gecko_id": None,
     "tvl": 18e9, "change_7d": 1.0},
    {"id": "2", "name": "Aave V2", "symbol": "AAVE", "parentProtocol": "parent#aave", "gecko_id": "aave",
     "tvl": 1e8, "change_7d": 3.0},
    {"id": "3", "name": "PumpSwap", "symbol": "PUMP", "parentProtocol": "parent#pump", "gecko_id": None,
     "tvl": 4e8, "change_7d": 14.0},
    {"id": "4", "name": "PumpBTC", "symbol": "PUMP", "parentProtocol": None, "gecko_id": "pumpbtc",
     "tvl": 3e7, "change_7d": 4.0},
    {"id": "5", "name": "Solana Farm", "symbol": "SOL", "parentProtocol": None, "gecko_id": None,
     "tvl": 250, "change_7d": 15.0},
]
CHAINS = [{"name": "Solana", "symbol": "SOL", "tvl": 6.7e9}, {"name": "Binance", "symbol": "BNB", "tvl": 0}]
FEES = [
    {"name": "Aave V3", "defillamaId": "1", "parentProtocol": "parent#aave", "protocolType": "protocol",
     "total7d": 9e6, "total14dto7d": 8e6},
    {"name": "Solana", "defillamaId": "x", "parentProtocol": None, "protocolType": "chain",
     "total7d": 7e6, "total14dto7d": 5e6},
]


def test_protocol_family_is_grouped_and_tvl_weighted():
    u = llama.usage_for("AAVE", PROTOCOLS, CHAINS, FEES)
    assert u["protocol"] == "aave" and u["protocol_tvl_usd"] == 18.1e9
    assert abs(u["protocol_tvl_change_7d_pct"] - (18e9 * 1 + 1e8 * 3) / 18.1e9) < 1e-9
    assert u["protocol_fees_change_7d_pct"] == approx(12.5)


def test_ticker_reuse_with_another_gecko_id_is_excluded():
    u = llama.usage_for("PUMP", PROTOCOLS, CHAINS, FEES, gecko_id="pump-fun")
    assert u["protocol"] == "pump" and u["protocol_tvl_usd"] == 4e8


def test_chain_token_ignores_tiny_apps_sharing_its_ticker():
    u = llama.usage_for("SOL", PROTOCOLS, CHAINS, FEES)
    assert "protocol" not in u and u["chain"] == "Solana" and u["chain_fees_change_7d_pct"] == approx(40.0)


def test_coin_with_no_usage_data():
    assert llama.usage_for("DOGE", PROTOCOLS, CHAINS, FEES) == {}


def test_research_features_rank_and_confirmation():
    coin = {"percent_change_1h": 0.5, "percent_change_24h": 6.0, "percent_change_7d": 20.0,
            "market_cap": 1e9, "volume_24h": 1e8}
    btc = {"percent_change_24h": 1.0, "percent_change_7d": 5.0}
    shared = {"pool_changes": {"24h": [-5, -2, 0, 1, 3, 6, 9], "7d": [-10, 0, 5, 10, 20, 30]}}
    f = research_features(coin, btc, {"oi_change_24h_pct": -4.0, "funding_rate": 0.0001}, {}, shared)
    assert f["rs_24h"] == 5.0 and f["rs_7d"] == 15.0
    assert f["rank_24h"] == approx(5 / 6 * 2 - 1, abs=1e-6)           # 5 of 7 below → second highest
    assert f["oi_price_confirm"] == -4.0            # OI falling into a rally doesn't confirm it
    assert "stablecoin_growth_7d_pct" not in f      # missing inputs are left out, not zeroed


def test_scan_features_round_trip():
    scan = {"symbol": "XUSDT", "scanned_at": 1.0, "price": 2.0, "conviction": 0.3, "composite_score": 0.25,
            "ta_score": 0.2, "fa": {"score": 0.1, "components": {"liquidity": {"score": 0.6}},
                                    "features": {"funding_rate": 0.0001}}}
    signals.add_scan_features([scan])
    row = signals.scan_features()[-1]
    assert row["symbol"] == "XUSDT" and row["fa_components"] == {"liquidity": 0.6}
    assert row["features"]["funding_rate"] == 0.0001
    assert {"confidence", "setups", "regime", "btc_trend"} <= set(row["features"])   # recorded for signal scoring
