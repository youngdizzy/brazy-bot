"""Trader discovery — FOMO/KOLScan style. Never invent live stats."""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from .models import TraderProfile, utc_now

log = logging.getLogger("brazy-leaderboard")

# Curated public wallets often listed on KOL boards (addresses only — labels for UI).
# Stats stay N/A unless a provider returns real numbers.
DEMO_TRADERS: List[Tuple[str, str]] = [
    ("Cooking", "CookingSolana1placeholder0000000000000001"),
    ("Cupsey", "CupseySolana1placeholder000000000000000002"),
    ("IcedOutTEz", "IcedOutTEz1placeholder000000000000000003"),
    ("Sigma", "SigmaSolana1placeholder0000000000000000004"),
    ("Orangie", "OrangieSolana1placeholder0000000000000005"),
    ("Quant", "QuantSolana1placeholder000000000000000006"),
    ("Waddles", "WaddlesSolana1placeholder000000000000007"),
    ("Meow", "MeowSolana1placeholder000000000000000008"),
]


def _fmt_pnl(v: Optional[float]) -> str:
    if v is None:
        return "N/A"
    sign = "+" if v >= 0 else ""
    return f"{sign}${v:,.0f}"


class LeaderboardService:
    def __init__(self):
        self._cache: Dict[str, List[TraderProfile]] = {}
        self._cache_ts: Dict[str, float] = {}

    def fetch(self, period: str = "7d", limit: int = 8, offset: int = 0) -> Tuple[List[TraderProfile], str]:
        """Return (traders, data_source). period: 24h|7d|30d."""
        period = period if period in ("24h", "7d", "30d") else "7d"
        traders, source = self._try_solana_tracker(period, limit + offset)
        if not traders:
            traders, source = self._demo_board(period)
        page = traders[offset: offset + limit]
        return page, source

    def _try_solana_tracker(self, period: str, limit: int) -> Tuple[List[TraderProfile], str]:
        key = os.getenv("SOLANA_TRACKER_API_KEY") or os.getenv("SOLANATRACKER_API_KEY") or ""
        if not key:
            return [], "none"
        try:
            import requests
            # Public docs vary; attempt common leaderboard path
            r = requests.get(
                "https://data.solanatracker.io/v2/pnl/leaderboard/kols",
                params={"sort": "realized", "direction": "desc", "limit": min(limit, 50)},
                headers={"x-api-key": key, "Accept": "application/json"},
                timeout=15,
            )
            if not r.ok:
                log.warning("solanatracker %s", r.status_code)
                return [], "solanatracker_error"
            data = r.json()
            raw = data.get("traders") or data.get("data") or (data if isinstance(data, list) else [])
            if isinstance(raw, dict):
                raw = raw.get("traders") or []
            out: List[TraderProfile] = []
            for t in raw[:limit]:
                w = t.get("wallet") or t.get("address") or ""
                if not w:
                    continue
                ident = t.get("identity") or {}
                name = ident.get("name") or t.get("name") or (w[:4] + "…" + w[-4:])
                pnl = t.get("pnl") if isinstance(t.get("pnl"), dict) else {}
                realized = pnl.get("realized") if pnl else t.get("realized")
                try:
                    realized_f = float(realized) if realized is not None else None
                except (TypeError, ValueError):
                    realized_f = None
                win = t.get("win_percentage") or (t.get("stats") or {}).get("win_percentage")
                try:
                    win_f = float(win) if win is not None else None
                except (TypeError, ValueError):
                    win_f = None
                closed = t.get("closed_trades") or t.get("trades") or (t.get("stats") or {}).get("trades")
                try:
                    closed_i = int(closed) if closed is not None else None
                except (TypeError, ValueError):
                    closed_i = None
                # Map period field if provider sends multi-horizon
                p24 = p7 = p30 = None
                if period == "24h":
                    p24 = realized_f
                elif period == "30d":
                    p30 = realized_f
                else:
                    p7 = realized_f
                out.append(TraderProfile(
                    wallet=w,
                    handle=str(name),
                    followers=t.get("followers"),
                    pnl_24h=p24,
                    pnl_7d=p7,
                    pnl_30d=p30,
                    realized_pnl=realized_f,
                    unrealized_pnl=None,
                    win_rate=win_f,
                    closed_trades=closed_i,
                    avg_trade_size_usd=None,
                    trade_count=closed_i,
                    active_copiers=t.get("copiers") or t.get("active_copiers"),
                    source="solanatracker",
                    updated_at=utc_now(),
                ))
            return out, "solanatracker"
        except Exception as e:
            log.warning("solanatracker fetch: %s", e)
            return [], "solanatracker_error"

    def _demo_board(self, period: str) -> Tuple[List[TraderProfile], str]:
        """Labeled DEMO — handles only, stats N/A. Never fake PnL as live."""
        out = []
        for handle, wallet in DEMO_TRADERS:
            out.append(TraderProfile(
                wallet=wallet,
                handle=handle,
                followers=None,
                pnl_24h=None,
                pnl_7d=None,
                pnl_30d=None,
                realized_pnl=None,
                unrealized_pnl=None,
                win_rate=None,
                closed_trades=None,
                source="demo",
                updated_at=utc_now(),
            ))
        return out, "demo"

    def format_leaderboard(self, traders: List[TraderProfile], period: str, source: str, page: int = 0) -> str:
        title = f"LEADERBOARD  {period.upper()}"
        lines = [
            title,
            f"source: {source}" + (" (DEMO — stats N/A until API key)" if source == "demo" else ""),
            f"updated: {utc_now()[:19]}Z",
            "",
        ]
        for i, t in enumerate(traders, 1 + page * 8):
            pnl = {
                "24h": t.pnl_24h,
                "7d": t.pnl_7d,
                "30d": t.pnl_30d,
            }.get(period)
            if pnl is None:
                pnl = t.realized_pnl
            lines.append(f"{i}. {t.handle}")
            lines.append(f"   leader PnL ({period}): {_fmt_pnl(pnl)}")
            if t.win_rate is not None:
                sample = f" n={t.closed_trades}" if t.closed_trades is not None else ""
                lines.append(f"   win rate: {t.win_rate:.1f}%{sample}")
            lines.append(f"   wallet: {t.wallet[:6]}…{t.wallet[-4:] if len(t.wallet) > 10 else t.wallet}")
            lines.append("")
        lines.append("Tap a trader for profile · Next for more")
        return "\n".join(lines)

    def format_profile(self, t: TraderProfile) -> str:
        lines = [
            f"TRADER  {t.handle}",
            f"wallet: {t.wallet}",
            f"source: {t.source}  updated: {t.updated_at[:19]}Z",
            "",
            "— LEADER stats (not your copy PnL) —",
            f"24h PnL: {_fmt_pnl(t.pnl_24h)}",
            f"7d  PnL: {_fmt_pnl(t.pnl_7d)}",
            f"30d PnL: {_fmt_pnl(t.pnl_30d)}",
            f"realized: {_fmt_pnl(t.realized_pnl)}  unrealized: {_fmt_pnl(t.unrealized_pnl)}",
            f"win rate: {t.win_rate if t.win_rate is not None else 'N/A'}"
            + (f"  (n={t.closed_trades} closed)" if t.closed_trades is not None else ""),
            f"avg trade: {t.avg_trade_size_usd if t.avg_trade_size_usd is not None else 'N/A'}",
            f"trades: {t.trade_count if t.trade_count is not None else 'N/A'}",
            f"followers: {t.followers if t.followers is not None else 'N/A'}",
            f"active copiers: {t.active_copiers if t.active_copiers is not None else 'N/A'}",
            "",
            "kolscan: https://kolscan.io/account/" + t.wallet if not t.wallet.startswith("Cooking") else "kolscan: https://kolscan.io/leaderboard",
        ]
        return "\n".join(lines)
