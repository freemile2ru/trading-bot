"""On-chain data: Etherscan, mempool.space (BTC), blockchain.info."""

import asyncio
import os
from data.http import get_json, get_text

ETHERSCAN_BASE = "https://api.etherscan.io/api"
MEMPOOL_BASE = "https://mempool.space/api"
BLOCKCHAIN_INFO = "https://blockchain.info"


def _etherscan_key() -> str:
    """Return Etherscan API key, or empty string — never send a placeholder."""
    return os.getenv("ETHERSCAN_API_KEY", "")


async def fetch_eth_gas() -> dict:
    key = _etherscan_key()
    params: dict = {"module": "gastracker", "action": "gasoracle"}
    if key:
        params["apikey"] = key
    try:
        data = await get_json(ETHERSCAN_BASE, params=params, timeout=10)
        if data.get("status") != "1":
            return {"error": data.get("message", "Etherscan error")}
        result = data.get("result", {})
        return {
            "safe_gas_gwei": float(result.get("SafeGasPrice", 0)),
            "standard_gas_gwei": float(result.get("ProposeGasPrice", 0)),
            "fast_gas_gwei": float(result.get("FastGasPrice", 0)),
            "base_fee_gwei": float(result.get("suggestBaseFee", 0)),
        }
    except Exception as e:
        return {"error": str(e)}


async def fetch_eth_supply_stats() -> dict:
    key = _etherscan_key()
    params: dict = {"module": "stats", "action": "ethsupply2"}
    if key:
        params["apikey"] = key
    try:
        data = await get_json(ETHERSCAN_BASE, params=params, timeout=10)
        if data.get("status") != "1":
            return {"error": data.get("message", "Etherscan error")}
        result = data.get("result", {})
        return {
            "eth_supply": float(result.get("EthSupply", 0)) / 1e18,
            "burned_fees_eth": float(result.get("BurntFees", 0)) / 1e18,
            "eth_2_staking": float(result.get("Eth2Staking", 0)) / 1e18,
        }
    except Exception as e:
        return {"error": str(e)}


async def fetch_btc_mempool() -> dict:
    try:
        data = await get_json(f"{MEMPOOL_BASE}/mempool", timeout=10)
        return {
            "count": data.get("count", 0),
            "vsize": data.get("vsize", 0),
            "total_fee_btc": data.get("total_fee", 0) / 1e8,
        }
    except Exception as e:
        return {"error": str(e)}


async def fetch_btc_hashrate() -> dict:
    try:
        text = await get_text(f"{BLOCKCHAIN_INFO}/q/hashrate", timeout=10)
        hashrate_gh = float(text)
        return {"hashrate_gh": hashrate_gh, "hashrate_eh": hashrate_gh / 1e9}
    except Exception as e:
        return {"error": str(e)}


async def fetch_btc_stats() -> dict:
    try:
        data = await get_json(f"{BLOCKCHAIN_INFO}/stats?format=json", timeout=10)
        return {
            "n_tx_per_day": data.get("n_tx"),
            "trade_volume_btc": data.get("trade_volume"),
            "difficulty": data.get("difficulty"),
            "miners_revenue_usd": data.get("miners_revenue_usd"),
            "nextretarget": data.get("nextretarget"),
        }
    except Exception as e:
        return {"error": str(e)}


async def fetch_onchain_snapshot(symbol: str = "BTCUSDT") -> dict:
    coin = symbol.upper().replace("USDT", "")

    if coin == "BTC":
        mempool, hashrate, stats = await asyncio.gather(
            fetch_btc_mempool(),
            fetch_btc_hashrate(),
            fetch_btc_stats(),
            return_exceptions=True,
        )
        return {
            "asset": "BTC",
            "mempool": mempool if not isinstance(mempool, Exception) else {"error": str(mempool)},
            "hashrate": hashrate if not isinstance(hashrate, Exception) else {"error": str(hashrate)},
            "network_stats": stats if not isinstance(stats, Exception) else {"error": str(stats)},
        }
    elif coin == "ETH":
        gas, supply = await asyncio.gather(
            fetch_eth_gas(),
            fetch_eth_supply_stats(),
            return_exceptions=True,
        )
        return {
            "asset": "ETH",
            "gas": gas if not isinstance(gas, Exception) else {"error": str(gas)},
            "supply": supply if not isinstance(supply, Exception) else {"error": str(supply)},
        }
    else:
        return {"asset": coin, "note": "On-chain data not available for this asset"}
