"""Backend risk gates enforced before any sign."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class RiskSnapshot:
    max_trade_sol: float = 0.25
    max_daily_loss_sol: float = 0.5
    max_open_positions: int = 5
    max_exposure_sol: float = 1.0
    max_slippage_bps: int = 150
    max_price_impact_pct: float = 12.0
    daily_realized_loss_sol: float = 0.0
    open_positions: int = 0
    exposure_sol: float = 0.0
    kill_switch: bool = False
    kill_switch_readable: bool = True
    min_rr: float = 1.5


def check_before_sign(
    side: str,
    size_sol: float,
    price_impact_pct: Optional[float],
    slippage_bps: int,
    snap: RiskSnapshot,
    planned_rr: Optional[float] = None,
) -> Tuple[bool, str]:
    if not snap.kill_switch_readable:
        return False, "kill_switch_unreadable_fail_closed"
    if snap.kill_switch:
        return False, "kill_switch"
    if size_sol <= 0:
        return False, "size_zero"
    if size_sol > snap.max_trade_sol:
        return False, f"max_trade_sol:{size_sol}>{snap.max_trade_sol}"
    if snap.daily_realized_loss_sol >= snap.max_daily_loss_sol:
        return False, "daily_loss_limit"
    if side == "buy" and snap.open_positions >= snap.max_open_positions:
        return False, "max_open_positions"
    if side == "buy" and (snap.exposure_sol + size_sol) > snap.max_exposure_sol:
        return False, "max_exposure"
    if slippage_bps > snap.max_slippage_bps:
        return False, "slippage_too_high"
    if price_impact_pct is not None and price_impact_pct > snap.max_price_impact_pct:
        return False, f"price_impact:{price_impact_pct}"
    if side == "buy" and planned_rr is not None and planned_rr + 1e-9 < snap.min_rr:
        return False, f"min_rr:{planned_rr}<{snap.min_rr}"
    return True, "ok"
