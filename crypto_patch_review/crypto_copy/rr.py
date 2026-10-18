"""Reward-to-risk calculation. Minimum 1.5:1 required for live entries."""
from __future__ import annotations

from typing import Optional, Tuple


def reward_to_risk(
    entry: float,
    stop: float,
    take_profit: float,
    fee_frac: float = 0.0,
) -> Optional[float]:
    """Long-only R:R after simple fee drag on both legs.
    fee_frac e.g. 0.01 = 1% total round-trip estimate.
    Returns None if invalid (no risk distance).
    """
    if entry is None or stop is None or take_profit is None:
        return None
    try:
        entry = float(entry)
        stop = float(stop)
        take_profit = float(take_profit)
        fee_frac = max(0.0, float(fee_frac))
    except (TypeError, ValueError):
        return None
    risk = entry - stop
    if risk <= 0:
        return None
    reward = take_profit - entry
    if reward <= 0:
        return None
    # fees reduce effective reward and increase effective risk slightly
    risk_adj = risk + entry * fee_frac * 0.5
    reward_adj = reward - entry * fee_frac * 0.5
    if risk_adj <= 0 or reward_adj <= 0:
        return None
    return reward_adj / risk_adj


def meets_min_rr(
    entry: float,
    stop: float,
    take_profit: float,
    min_rr: float = 1.5,
    fee_frac: float = 0.01,
) -> Tuple[bool, Optional[float], str]:
    rr = reward_to_risk(entry, stop, take_profit, fee_frac=fee_frac)
    if rr is None:
        return False, None, "invalid_rr_inputs"
    if rr + 1e-9 < float(min_rr):
        return False, rr, f"rr_{rr:.3f}_below_{min_rr}"
    return True, rr, "ok"
