"""Auto exit evaluation: TP / SL / trailing. Does not invent fills."""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("brazy-posmgr")


def evaluate_exit(
    pos: Dict[str, Any],
    mark: float,
) -> Tuple[Optional[str], str]:
    """Return (action, reason) action in sell|None."""
    if pos.get("status") != "open":
        return None, "not_open"
    try:
        mark = float(mark)
    except Exception:
        return None, "bad_mark"
    stop = pos.get("stop") or pos.get("stop_loss")
    tp = pos.get("take_profit") or pos.get("tp")
    trail = pos.get("trail_pct")
    entry = float(pos.get("avg_entry_usd") or pos.get("entry") or 0)
    if stop is not None:
        try:
            if mark <= float(stop):
                return "sell", f"stop_hit mark={mark} stop={stop}"
        except Exception:
            pass
    if tp is not None:
        try:
            if mark >= float(tp):
                return "sell", f"tp_hit mark={mark} tp={tp}"
        except Exception:
            pass
    if trail is not None and entry > 0:
        try:
            peak = float(pos.get("peak_mark") or mark)
            peak = max(peak, mark)
            pos["peak_mark"] = peak
            trail_stop = peak * (1.0 - float(trail) / 100.0)
            if mark <= trail_stop:
                return "sell", f"trail_hit mark={mark} trail_stop={trail_stop}"
        except Exception:
            pass
    return None, "hold"


def attach_risk_levels(
    pos: Dict[str, Any],
    stop: Optional[float],
    take_profit: Optional[float],
    min_rr: float = 1.5,
    fee_frac: float = 0.01,
) -> Tuple[Dict[str, Any], str]:
    from .rr import meets_min_rr
    entry = float(pos.get("avg_entry_usd") or pos.get("entry") or 0)
    if stop is None or take_profit is None or entry <= 0:
        return pos, "levels_incomplete"
    ok, rr, reason = meets_min_rr(entry, float(stop), float(take_profit), min_rr=min_rr, fee_frac=fee_frac)
    pos["stop"] = float(stop)
    pos["take_profit"] = float(take_profit)
    pos["planned_rr"] = rr
    if not ok:
        return pos, reason
    return pos, "ok"
