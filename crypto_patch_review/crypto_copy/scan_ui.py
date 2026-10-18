"""Scan Memes — FOMO-style coin buttons + one-click snipe via executor."""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from .snipe import enrich_pair, passes_snipe_filters
from . import executor as ex
from .copy_engine import live_enabled

log = logging.getLogger("brazy-scan-ui")

try:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
except ImportError:
    InlineKeyboardButton = None  # type: ignore
    InlineKeyboardMarkup = None  # type: ignore


def _dex(url: str, params=None) -> Any:
    import requests
    r = requests.get(url, params=params or {}, timeout=18)
    if not r.ok:
        return None
    return r.json()


def discover_memes(
    chain: str = "solana",
    limit: int = 8,
    sort: str = "newest",
    min_liq_usd: float = 8000.0,
) -> Tuple[List[Dict[str, Any]], str]:
    """Return list of coin dicts for buttons. Never invents stats."""
    coins: List[Dict[str, Any]] = []
    source = "dexscreener"

    # token profiles (new)
    profiles = _dex("https://api.dexscreener.com/token-profiles/latest/v1")
    candidates = []
    if isinstance(profiles, list):
        for row in profiles:
            if (row.get("chainId") or "").lower() != chain:
                continue
            addr = row.get("tokenAddress") or row.get("address")
            if not addr:
                continue
            candidates.append(addr)

    # boosts as extra
    boosts = _dex("https://api.dexscreener.com/token-boosts/top/v1")
    if isinstance(boosts, list):
        for row in boosts:
            if (row.get("chainId") or "").lower() != chain:
                continue
            addr = row.get("tokenAddress")
            if addr and addr not in candidates:
                candidates.append(addr)

    seen = set()
    for addr in candidates[:40]:
        if addr in seen:
            continue
        seen.add(addr)
        pair = enrich_pair(addr, chain)
        if not pair:
            continue
        ok, reason = passes_snipe_filters(pair, min_liq_usd=min_liq_usd, max_age_min=24 * 60)
        # still show borderline with status; hard reject only invalid mint
        base = pair.get("baseToken") or {}
        mint = base.get("address") or addr
        if not mint or len(mint) < 32:
            continue
        liq = float((pair.get("liquidity") or {}).get("usd") or 0)
        vol = float((pair.get("volume") or {}).get("h24") or 0)
        mcap = pair.get("marketCap") or pair.get("fdv")
        try:
            mcap_f = float(mcap) if mcap is not None else None
        except Exception:
            mcap_f = None
        price = pair.get("priceUsd")
        try:
            price_f = float(price) if price is not None else None
        except Exception:
            price_f = None
        created = pair.get("pairCreatedAt")
        age_min = None
        if created:
            try:
                age_min = (time.time() * 1000 - float(created)) / 60000.0
            except Exception:
                age_min = None
        logo = None
        info = pair.get("info") or {}
        if isinstance(info, dict):
            logo = info.get("imageUrl")
        coins.append({
            "mint": mint,
            "symbol": (base.get("symbol") or "?")[:12],
            "name": (base.get("name") or base.get("symbol") or "?")[:24],
            "price_usd": price_f,
            "liquidity_usd": liq,
            "mcap_usd": mcap_f,
            "volume_h24": vol,
            "age_min": age_min,
            "pair_address": pair.get("pairAddress"),
            "dex": pair.get("dexId") or "?",
            "url": pair.get("url") or f"https://dexscreener.com/{chain}/{pair.get('pairAddress') or mint}",
            "logo": logo,
            "filter_ok": ok,
            "filter_reason": reason,
            "ts": time.time(),
        })
        if len(coins) >= limit * 3:
            break

    # sort
    if sort == "liquidity":
        coins.sort(key=lambda c: c.get("liquidity_usd") or 0, reverse=True)
    elif sort == "volume":
        coins.sort(key=lambda c: c.get("volume_h24") or 0, reverse=True)
    elif sort == "mcap":
        coins.sort(key=lambda c: c.get("mcap_usd") if c.get("mcap_usd") is not None else 1e99)
    else:  # newest
        coins.sort(key=lambda c: c.get("age_min") if c.get("age_min") is not None else 1e99)

    return coins[:limit], source


def _fmt_usd(v: Optional[float]) -> str:
    if v is None:
        return "N/A"
    if v >= 1_000_000:
        return f"${v/1e6:.2f}M"
    if v >= 1_000:
        return f"${v/1e3:.1f}K"
    if v >= 1:
        return f"${v:.2f}"
    return f"${v:.6f}"


def _fmt_age(m: Optional[float]) -> str:
    if m is None:
        return "N/A"
    if m < 60:
        return f"{m:.0f}m"
    if m < 1440:
        return f"{m/60:.1f}h"
    return f"{m/1440:.1f}d"


class ScanMemesUI:
    def __init__(self):
        self._cache: Dict[str, List[Dict[str, Any]]] = {}
        self._inflight: set = set()  # user_id:mint locks

    def build_board(
        self,
        sort: str = "newest",
        limit: int = 8,
        min_liq: float = 8000.0,
    ) -> Tuple[str, Any]:
        coins, source = discover_memes(sort=sort, limit=limit, min_liq_usd=min_liq)
        self._cache[sort] = coins
        lines = [
            "SCAN MEMES",
            f"source: {source} | sort: {sort}",
            "Tap a coin for details / snipe.",
            "",
        ]
        if not coins:
            lines.append("No pairs passed filters. Try Refresh or lower min liquidity.")
        rows = []
        rows.append([
            InlineKeyboardButton("Newest", callback_data="mscan_sort_newest"),
            InlineKeyboardButton("Liquidity", callback_data="mscan_sort_liquidity"),
        ])
        rows.append([
            InlineKeyboardButton("Volume 24h", callback_data="mscan_sort_volume"),
            InlineKeyboardButton("Low mcap", callback_data="mscan_sort_mcap"),
        ])
        btns = []
        for i, c in enumerate(coins):
            sym = c["symbol"]
            liq = _fmt_usd(c.get("liquidity_usd"))
            label = f"{sym} {liq}"[:28]
            btns.append(InlineKeyboardButton(label, callback_data=f"mscan_i_{sort}_{i}"))
            age = _fmt_age(c.get("age_min"))
            st = "ok" if c.get("filter_ok") else c.get("filter_reason", "filter")
            lines.append(
                f"{i+1}. {c['name']} ({sym})  px {_fmt_usd(c.get('price_usd'))}  "
                f"liq {_fmt_usd(c.get('liquidity_usd'))}  age {age}  [{st}]"
            )
        for j in range(0, len(btns), 2):
            rows.append(btns[j:j + 2])
        rows.append([
            InlineKeyboardButton("Refresh", callback_data=f"mscan_sort_{sort}"),
            InlineKeyboardButton("< Sol desk", callback_data="hub_crypto"),
        ])
        return "\n".join(lines)[:3800], InlineKeyboardMarkup(rows)

    def resolve(self, sort: str, idx: int) -> Optional[Dict[str, Any]]:
        coins = self._cache.get(sort) or []
        if 0 <= idx < len(coins):
            return coins[idx]
        return None

    def detail_panel(self, coin: Dict[str, Any], sol_size: float) -> Tuple[str, Any]:
        mint = coin["mint"]
        lines = [
            f"SNIPE — {coin['name']} ({coin['symbol']})",
            f"mint: {mint}",
            f"price: {_fmt_usd(coin.get('price_usd'))}",
            f"liquidity: {_fmt_usd(coin.get('liquidity_usd'))}",
            f"mcap: {_fmt_usd(coin.get('mcap_usd'))}",
            f"vol 24h: {_fmt_usd(coin.get('volume_h24'))}",
            f"age: {_fmt_age(coin.get('age_min'))}",
            f"dex: {coin.get('dex')}  route: Jupiter",
            f"planned size: {sol_size} SOL",
            f"live: {'ON' if live_enabled() else 'OFF (browse only)'}",
            "",
            "Snipe runs risk checks + Jupiter + sim before any sign.",
        ]
        if not coin.get("filter_ok"):
            lines.append(f"filter note: {coin.get('filter_reason')}")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("SNIPE COIN", callback_data=f"msnipe_{mint[:40]}")],
            [
                InlineKeyboardButton("Refresh quote", callback_data=f"mscan_mint_{mint[:40]}"),
                InlineKeyboardButton("Back", callback_data="crypto_scan"),
            ],
            [InlineKeyboardButton("< Sol desk", callback_data="hub_crypto")],
        ])
        return "\n".join(lines), kb

    def refresh_mint(self, mint: str) -> Optional[Dict[str, Any]]:
        pair = enrich_pair(mint, "solana")
        if not pair:
            return None
        base = pair.get("baseToken") or {}
        liq = float((pair.get("liquidity") or {}).get("usd") or 0)
        ok, reason = passes_snipe_filters(pair, min_liq_usd=5000)
        return {
            "mint": base.get("address") or mint,
            "symbol": (base.get("symbol") or "?")[:12],
            "name": (base.get("name") or "?")[:24],
            "price_usd": float(pair.get("priceUsd") or 0) or None,
            "liquidity_usd": liq,
            "mcap_usd": float(pair.get("marketCap") or pair.get("fdv") or 0) or None,
            "volume_h24": float((pair.get("volume") or {}).get("h24") or 0),
            "age_min": None,
            "dex": pair.get("dexId") or "?",
            "filter_ok": ok,
            "filter_reason": reason,
        }

    def execute_snipe(
        self,
        user_id: int,
        mint: str,
        secret: Optional[str],
        pubkey: Optional[str],
        sol_size: float,
        slippage_bps: int,
        kill: bool,
    ) -> str:
        lock = f"{user_id}:{mint}"
        if lock in self._inflight:
            return "Already submitting this mint — wait."
        self._inflight.add(lock)
        try:
            if kill:
                return "Kill switch ON — snipe blocked."
            if not live_enabled():
                return (
                    "COPY_LIVE_ENABLED is false — scan/detail work, but no sign/broadcast.\n"
                    "Set COPY_LIVE_ENABLED=true + restart to allow real snipes."
                )
            if not secret or not pubkey:
                return "Wallet not configured. /wallet key <burner_secret> first."
            coin = self.refresh_mint(mint)
            if not coin:
                return "Could not refresh pair — mint/route invalid or DexScreener empty."
            if (coin.get("liquidity_usd") or 0) < 3000:
                return f"Rejected: liquidity too low ({coin.get('liquidity_usd')})."
            # Jupiter buy
            result = ex.buy_token_sol(
                secret,
                pubkey,
                mint,
                sol_size,
                slippage_bps=slippage_bps,
            )
            if not result.get("ok"):
                return (
                    "SNIPE FAILED\n"
                    f"error: {result.get('error')}\n"
                    f"sig: {result.get('sig') or 'none'}\n"
                    "(no fake fill)"
                )
            out_amt = (result.get("quote") or {}).get("outAmount")
            try:
                qty = float(out_amt) if out_amt else 0.0
            except Exception:
                qty = 0.0
            self.last_fill = {
                "mint": mint,
                "symbol": (coin.get("symbol") if coin else None) or mint[:6],
                "qty": qty,
                "avg_entry_usd": coin.get("price_usd") if coin else None,
                "sig": result.get("sig"),
                "origin": "sniped",
                "status": "open",
            }
            return (
                "SNIPE CONFIRMED\n"
                f"sig: {result.get('sig')}\n"
                f"status: {result.get('confirmation')}\n"
                f"quote out: {out_amt}\n"
                "origin: sniped"
            )
        finally:
            self._inflight.discard(lock)


_SCAN_UI: Optional[ScanMemesUI] = None


def get_scan_ui() -> ScanMemesUI:
    global _SCAN_UI
    if _SCAN_UI is None:
        _SCAN_UI = ScanMemesUI()
    return _SCAN_UI
