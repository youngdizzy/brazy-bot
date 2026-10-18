"""Follower PnL from follower fills only. Never substitute leader stats."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


class PnLCalculator:
    """
    Win definition (documented):
    A closed follower trade is a WIN if realized_pnl_usd > 0 after fees.
    Win rate = wins / closed_trades * 100 when closed_trades > 0 else N/A.
    """

    def stats_for_leader(
        self,
        fills: List[Dict[str, Any]],
        positions: List[Dict[str, Any]],
        mark_prices: Optional[Dict[str, float]] = None,
    ) -> Dict[str, Any]:
        mark_prices = mark_prices or {}
        realized = 0.0
        fees = 0.0
        closed = 0
        wins = 0
        open_count = 0
        exposure = 0.0
        unrealized = 0.0

        # Realized from closed legs in fills + position realized field
        for p in positions:
            realized += float(p.get("realized_pnl_usd") or 0)
            fees += float(p.get("fees_usd") or 0)
            if p.get("status") == "closed":
                closed += 1
                if float(p.get("realized_pnl_usd") or 0) > 0:
                    wins += 1
            elif p.get("status") == "open":
                open_count += 1
                qty = float(p.get("qty") or 0)
                entry = float(p.get("avg_entry_usd") or 0)
                mint = p.get("mint") or ""
                px = mark_prices.get(mint)
                if px is not None and qty > 0:
                    unrealized += (px - entry) * qty
                    exposure += px * qty
                else:
                    exposure += entry * qty

        for f in fills:
            fees += float(f.get("fee_usd") or 0)

        total = realized + unrealized
        win_rate = (wins / closed * 100.0) if closed > 0 else None
        return {
            "my_realized_pnl": round(realized, 4),
            "my_unrealized_pnl": round(unrealized, 4) if mark_prices else None,
            "my_total_pnl": round(total, 4),
            "my_win_rate": round(win_rate, 2) if win_rate is not None else None,
            "closed_trades": closed,
            "open_positions": open_count,
            "exposure_usd": round(exposure, 4),
            "fees_usd": round(fees, 4),
            "sample_size": closed,
        }

    def apply_fill_to_positions(
        self,
        positions: List[Dict[str, Any]],
        fill: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Update position ledger from a follower fill."""
        leader = fill["leader_wallet"]
        mint = fill["mint"]
        side = fill["side"]
        qty = float(fill["qty"])
        price = float(fill["price_usd"])
        fee = float(fill.get("fee_usd") or 0)
        symbol = fill.get("symbol") or mint[:6]

        positions = [dict(p) for p in positions]
        idx = None
        for i, p in enumerate(positions):
            if p.get("leader_wallet") == leader and p.get("mint") == mint and p.get("status") == "open":
                idx = i
                break

        if side == "buy":
            if idx is None:
                import uuid
                positions.append({
                    "id": str(uuid.uuid4())[:12],
                    "leader_wallet": leader,
                    "mint": mint,
                    "symbol": symbol,
                    "qty": qty,
                    "avg_entry_usd": price,
                    "realized_pnl_usd": 0.0,
                    "fees_usd": fee,
                    "status": "open",
                    "origin": "copied",
                    "opened_at": fill.get("ts"),
                    "closed_at": None,
                })
            else:
                p = positions[idx]
                old_q = float(p["qty"])
                old_a = float(p["avg_entry_usd"])
                new_q = old_q + qty
                p["avg_entry_usd"] = (old_a * old_q + price * qty) / new_q if new_q else price
                p["qty"] = new_q
                p["fees_usd"] = float(p.get("fees_usd") or 0) + fee
        else:  # sell
            if idx is None:
                return positions  # no open pos — ignore or track as error upstream
            p = positions[idx]
            old_q = float(p["qty"])
            entry = float(p["avg_entry_usd"])
            sell_q = min(qty, old_q)
            pnl = (price - entry) * sell_q - fee
            p["realized_pnl_usd"] = float(p.get("realized_pnl_usd") or 0) + pnl
            p["fees_usd"] = float(p.get("fees_usd") or 0) + fee
            p["qty"] = old_q - sell_q
            if p["qty"] <= 1e-12:
                p["qty"] = 0.0
                p["status"] = "closed"
                p["closed_at"] = fill.get("ts")
        return positions
