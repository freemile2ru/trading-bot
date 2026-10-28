"""DefiLlama (free, no key) — usage fundamentals: TVL, fees, stablecoin liquidity.

CoinMarketCap only knows price, volume and supply. DefiLlama measures whether anyone is
actually using what a token represents:

  * a DeFi token (UNI, AAVE, HYPE, PUMP…) — the TVL and fees of its whole protocol family
    (Uniswap V1–V4, Aave V1–V4 …, grouped by DefiLlama's parent protocol)
  * a chain token (SOL, SUI, AVAX, NEAR, TAO…) — the chain's TVL and the fees it earns
  * everything — total stablecoin supply, a proxy for dollars sitting on-chain ready to
    buy; growing supply has historically accompanied rising crypto markets

Coins with neither (BTC, DOGE, XRP…) simply get no usage data.

The protocol and fee listings are single calls covering every project, so they're cached
and shared by the whole sweep; only a chain's TVL history is fetched per symbol.
"""

from data.http import get_json

API = "https://api.llama.fi"
STABLES = "https://stablecoins.llama.fi"


def _pct(new: float | None, old: float | None) -> float | None:
    return (new / old - 1) * 100 if new and old else None


async def fetch_protocols() -> list[dict]:
    rows = await get_json(f"{API}/protocols", timeout=30.0)
    keep = ("id", "name", "symbol", "parentProtocol", "gecko_id", "category", "tvl", "change_7d")
    return [{k: p.get(k) for k in keep} for p in rows if p.get("category") != "CEX"]


async def fetch_chains() -> list[dict]:
    rows = await get_json(f"{API}/v2/chains", timeout=20.0)
    return [{"name": c["name"], "symbol": c.get("tokenSymbol"), "tvl": c.get("tvl") or 0} for c in rows]


async def fetch_fees() -> list[dict]:
    d = await get_json(f"{API}/overview/fees",
                       params={"excludeTotalDataChart": "true", "excludeTotalDataChartBreakdown": "true"},
                       timeout=30.0)
    keep = ("name", "defillamaId", "parentProtocol", "protocolType", "total7d", "total14dto7d", "total30d")
    return [{k: p.get(k) for k in keep} for p in d.get("protocols", [])]


async def fetch_chain_tvl_history(chain: str) -> list[dict]:
    return await get_json(f"{API}/v2/historicalChainTvl/{chain}", timeout=20.0)


async def fetch_stablecoin_history() -> list[dict]:
    """Daily total USD stablecoin supply, oldest first: [{"date": unix_s, "usd": float}]."""
    rows = await get_json(f"{STABLES}/stablecoincharts/all", timeout=30.0)
    return [{"date": int(r["date"]), "usd": (r.get("totalCirculatingUSD") or {}).get("peggedUSD") or 0}
            for r in rows]


def stablecoin_growth(history: list[dict]) -> dict:
    usd = [r["usd"] for r in history if r["usd"]]
    if len(usd) < 31:
        return {}
    return {"stablecoin_supply_usd": usd[-1], "stablecoin_growth_7d_pct": _pct(usd[-1], usd[-8]),
            "stablecoin_growth_30d_pct": _pct(usd[-1], usd[-31])}


def protocol_group(symbol: str, protocols: list[dict], gecko_id: str | None = None) -> list[dict]:
    """Every protocol belonging to the project whose token is `symbol`.

    A token's protocols share a parent (parent#aave covers Aave V1–V4, Horizon, Arc…).
    Protocols that merely reuse the ticker (PumpBTC also says PUMP) are excluded when
    they carry a different CoinGecko id, and ties go to the family with the most TVL.
    """
    mine = [p for p in protocols if (p.get("symbol") or "").upper() == symbol.upper()
            and not (gecko_id and p.get("gecko_id") and p["gecko_id"] != gecko_id)]
    if not mine:
        return []
    families: dict[str, float] = {}
    for p in mine:
        fam = p.get("parentProtocol") or f"id#{p['id']}"
        families[fam] = families.get(fam, 0) + (p.get("tvl") or 0)
    best = max(families, key=families.get)
    if best.startswith("id#"):
        return [p for p in mine if f"id#{p['id']}" == best]
    return [p for p in protocols if p.get("parentProtocol") == best]


def usage_for(symbol: str, protocols: list[dict], chains: list[dict], fees: list[dict],
              gecko_id: str | None = None) -> dict:
    """TVL and fee trends for a token's protocol family and/or chain. {} if it has neither."""
    out: dict = {}
    chain = max((c for c in chains if (c.get("symbol") or "").upper() == symbol.upper() and c["tvl"] > 0),
                key=lambda c: c["tvl"], default=None)
    group = protocol_group(symbol, protocols, gecko_id)
    # A chain token's ticker is often reused by small apps on that chain ("Solana Farm"
    # says SOL). Keep a protocol match only when it's a real part of the token's value
    # (HYPE: the Hyperliquid exchange dwarfs its L1).
    if chain and sum(p.get("tvl") or 0 for p in group) < 0.1 * chain["tvl"]:
        group = []
    if group:
        tvl = sum(p.get("tvl") or 0 for p in group)
        ids = {str(p["id"]) for p in group}
        parents = {p.get("parentProtocol") for p in group if p.get("parentProtocol")}
        fee_rows = [f for f in fees if f.get("protocolType") != "chain"
                    and (str(f.get("defillamaId")) in ids or f.get("parentProtocol") in parents)]
        out["protocol"] = next(iter(parents), group[0]["name"]).replace("parent#", "")
        out["protocol_tvl_usd"] = tvl
        if tvl:
            # TVL-weighted 7d change across the family (DefiLlama gives it per protocol).
            out["protocol_tvl_change_7d_pct"] = sum((p.get("tvl") or 0) * (p.get("change_7d") or 0)
                                                    for p in group) / tvl
        if fee_rows:
            f7 = sum(f.get("total7d") or 0 for f in fee_rows)
            f_prev = sum(f.get("total14dto7d") or 0 for f in fee_rows)
            out["protocol_fees_7d_usd"] = f7
            out["protocol_fees_change_7d_pct"] = _pct(f7, f_prev)

    if chain:
        out["chain"] = chain["name"]
        out["chain_tvl_usd"] = chain["tvl"]
        cf = next((f for f in fees if f.get("protocolType") == "chain" and f.get("name") == chain["name"]), None)
        if cf and cf.get("total7d") is not None:
            out["chain_fees_7d_usd"] = cf["total7d"]
            out["chain_fees_change_7d_pct"] = _pct(cf["total7d"], cf.get("total14dto7d"))
    return out


def chain_tvl_changes(history: list[dict]) -> dict:
    tvl = [r["tvl"] for r in history if r.get("tvl")]
    if len(tvl) < 31:
        return {}
    return {"chain_tvl_change_7d_pct": _pct(tvl[-1], tvl[-8]), "chain_tvl_change_30d_pct": _pct(tvl[-1], tvl[-31])}
