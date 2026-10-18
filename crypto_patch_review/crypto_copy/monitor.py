"""Monitor Solana wallets for swap-like activity via public RPC.

Parses confirmed signatures + transaction meta for token balance changes.
Does not invent trades. Best-effort classification of buy/sell vs SOL.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional, Set

from .executor import RPC_URL, rpc, SOL_MINT

log = logging.getLogger("brazy-monitor")


def fetch_signatures(wallet: str, limit: int = 15, before: Optional[str] = None) -> List[Dict[str, Any]]:
    params: list = [wallet, {"limit": limit}]
    if before:
        params[1]["before"] = before
    try:
        res = rpc("getSignaturesForAddress", params)
        return res or []
    except Exception as e:
        log.warning("sigs %s: %s", wallet[:8], e)
        return []


def fetch_tx(sig: str) -> Optional[Dict[str, Any]]:
    try:
        return rpc(
            "getTransaction",
            [
                sig,
                {
                    "encoding": "jsonParsed",
                    "maxSupportedTransactionVersion": 0,
                    "commitment": "confirmed",
                },
            ],
        )
    except Exception as e:
        log.warning("tx %s: %s", sig[:12], e)
        return None


def classify_swap_event(wallet: str, sig: str, tx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return buy/sell event dict or None if not a clear token swap involving wallet."""
    if not tx or tx.get("meta", {}).get("err"):
        return None
    meta = tx.get("meta") or {}
    pre = meta.get("preTokenBalances") or []
    post = meta.get("postTokenBalances") or []
    # Map account index -> balances for owner==wallet
    def idx_map(rows):
        m = {}
        for r in rows:
            if r.get("owner") != wallet:
                continue
            mint = r.get("mint")
            ui = (r.get("uiTokenAmount") or {}).get("uiAmount")
            raw = (r.get("uiTokenAmount") or {}).get("amount")
            if mint:
                m[mint] = {"ui": ui, "raw": int(raw or 0)}
        return m

    pre_m = idx_map(pre)
    post_m = idx_map(post)
    mints = set(pre_m) | set(post_m)
    changes = []
    for mint in mints:
        if mint == SOL_MINT:
            continue
        a0 = pre_m.get(mint, {}).get("raw", 0)
        a1 = post_m.get(mint, {}).get("raw", 0)
        if a1 != a0:
            changes.append((mint, a1 - a0, post_m.get(mint, {}).get("ui")))

    if not changes:
        return None

    # Prefer largest absolute change
    changes.sort(key=lambda x: abs(x[1]), reverse=True)
    mint, delta, ui = changes[0]
    side = "buy" if delta > 0 else "sell"
    block_time = tx.get("blockTime")
    age_sec = (time.time() - block_time) if block_time else 0
    return {
        "sig": sig,
        "side": side,
        "mint": mint,
        "symbol": mint[:6],
        "delta_raw": delta,
        "ui_amount": ui,
        "price_usd": None,  # filled by caller if needed
        "liquidity_usd": None,
        "token_age_min": 999,
        "age_sec": age_sec,
        "wallet": wallet,
        "ts": block_time,
    }


class WalletMonitor:
    """Poll source wallets; yield new classified events (dedupe via seen set)."""

    def __init__(self):
        self._seen: Dict[str, Set[str]] = {}

    def poll_wallet(self, wallet: str, limit: int = 12) -> List[Dict[str, Any]]:
        seen = self._seen.setdefault(wallet, set())
        events = []
        for row in fetch_signatures(wallet, limit=limit):
            sig = row.get("signature")
            if not sig or sig in seen:
                continue
            if row.get("err"):
                seen.add(sig)
                continue
            tx = fetch_tx(sig)
            ev = classify_swap_event(wallet, sig, tx or {})
            seen.add(sig)
            if ev:
                events.append(ev)
            # cap memory
            if len(seen) > 500:
                self._seen[wallet] = set(list(seen)[-250:])
        return events
