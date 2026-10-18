"""Token / pool sniping helpers — DexScreener discovery + hard filters.

Does not guarantee safety against rugs. Filters only.
Live buy goes through executor when COPY_LIVE_ENABLED and snipe enabled.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("brazy-snipe")


def _dex(url: str, params=None) -> Any:
    import requests
    r = requests.get(url, params=params or {}, timeout=15)
    if not r.ok:
        return None
    return r.json()


def fetch_new_pairs(chain: str = "solana", limit: int = 20) -> List[Dict[str, Any]]:
    """Best-effort new pairs from DexScreener token profiles / boosts."""
    out = []
    data = _dex("https://api.dexscreener.com/token-profiles/latest/v1")
    if isinstance(data, list):
        for row in data:
            if (row.get("chainId") or "").lower() != chain:
                continue
            out.append(row)
            if len(out) >= limit:
                break
    return out


def enrich_pair(token_address: str, chain: str = "solana") -> Optional[Dict[str, Any]]:
    data = _dex(f"https://api.dexscreener.com/token-pairs/v1/{chain}/{token_address}")
    if not data or not isinstance(data, list) or not data:
        data = _dex("https://api.dexscreener.com/latest/dex/tokens/" + token_address)
        if data and data.get("pairs"):
            pairs = [p for p in data["pairs"] if (p.get("chainId") or "").lower() == chain]
            return pairs[0] if pairs else None
        return None
    return data[0]


def passes_snipe_filters(
    pair: Dict[str, Any],
    min_liq_usd: float = 15000,
    max_age_min: float = 120,
    max_impact_hint: float = 12.0,
) -> Tuple[bool, str]:
    if not pair:
        return False, "no_pair"
    liq = float((pair.get("liquidity") or {}).get("usd") or 0)
    if liq < min_liq_usd:
        return False, f"liq<{min_liq_usd}"
    created = pair.get("pairCreatedAt")
    if created:
        age_min = (time.time() * 1000 - float(created)) / 60000.0
        if age_min > max_age_min:
            return False, f"too_old:{age_min:.0f}m"
    # DexScreener does not always expose mint authority — mark N/A
    return True, "ok"


def snipe_candidate_event(pair: Dict[str, Any]) -> Dict[str, Any]:
    base = pair.get("baseToken") or {}
    return {
        "sig": f"snipe:{(pair.get('pairAddress') or base.get('address') or '')}",
        "side": "buy",
        "mint": base.get("address"),
        "symbol": base.get("symbol") or "?",
        "price_usd": float(pair.get("priceUsd") or 0) or None,
        "liquidity_usd": float((pair.get("liquidity") or {}).get("usd") or 0),
        "token_age_min": 0,
        "age_sec": 0,
        "source": "dexscreener",
    }
