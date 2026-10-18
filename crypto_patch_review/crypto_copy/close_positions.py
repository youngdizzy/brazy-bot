"""Three close scopes: copied | sniped | all. Origin from position records only."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("brazy-close")

ORIGIN_COPIED = "copied"
ORIGIN_SNIPED = "sniped"
ORIGIN_MANUAL = "manual"
ORIGIN_UNKNOWN = "unknown"

SCOPE_COPIED = "copied"
SCOPE_SNIPED = "sniped"
SCOPE_ALL = "all"


def normalize_origin(pos: Dict[str, Any]) -> str:
    o = (pos.get("origin") or pos.get("source") or pos.get("trade_origin") or "").lower().strip()
    if o in ("copy", "copied", "copy_trade", "fomo", "kolscan"):
        return ORIGIN_COPIED
    if o in ("snipe", "sniped", "scan", "meme_snipe"):
        return ORIGIN_SNIPED
    if o in ("manual", "bot", "manual_buy"):
        return ORIGIN_MANUAL
    # copy engine positions often have leader_wallet
    if pos.get("leader_wallet"):
        return ORIGIN_COPIED
    if pos.get("snipe") is True or pos.get("via") == "snipe":
        return ORIGIN_SNIPED
    return ORIGIN_UNKNOWN


def filter_open(positions: List[Dict[str, Any]], scope: str) -> List[Dict[str, Any]]:
    opens = [p for p in positions if p.get("status", "open") == "open"]
    if scope == SCOPE_ALL:
        # bot-managed only: has known origin OR tracked id; exclude pure unknown wallet dust
        return [p for p in opens if normalize_origin(p) != ORIGIN_UNKNOWN or p.get("id")]
    if scope == SCOPE_COPIED:
        return [p for p in opens if normalize_origin(p) == ORIGIN_COPIED]
    if scope == SCOPE_SNIPED:
        return [p for p in opens if normalize_origin(p) == ORIGIN_SNIPED]
    return []


def estimate_proceeds_usd(positions: List[Dict[str, Any]]) -> Optional[float]:
    total = 0.0
    any_v = False
    for p in positions:
        px = p.get("mark_usd") or p.get("price_usd") or p.get("last_price")
        qty = p.get("qty") or p.get("amount") or 0
        try:
            if px is not None:
                total += float(px) * float(qty)
                any_v = True
        except Exception:
            pass
    return total if any_v else None


def confirm_text(scope: str, positions: List[Dict[str, Any]]) -> str:
    labels = {
        SCOPE_COPIED: "CLOSE ALL COPIED TRADES",
        SCOPE_SNIPED: "CLOSE ALL SNIPED TRADES",
        SCOPE_ALL: "CLOSE ALL (bot-managed positions)",
    }
    title = labels.get(scope, "CLOSE")
    est = estimate_proceeds_usd(positions)
    est_s = f"${est:,.2f}" if est is not None else "N/A"
    names = ", ".join((p.get("symbol") or p.get("mint", "?")[:6]) for p in positions[:15])
    origins = {}
    for p in positions:
        o = normalize_origin(p)
        origins[o] = origins.get(o, 0) + 1
    origin_line = ", ".join(f"{k}={v}" for k, v in origins.items())
    return (
        f"{title}\n"
        f"Positions: {len(positions)}\n"
        f"Origins: {origin_line}\n"
        f"Est. value (if priced): {est_s}\n"
        f"Tokens: {names or 'none'}\n\n"
        "Does NOT remove copy-trader configs.\n"
        "Does NOT sell untracked wallet tokens.\n"
        "Confirm to proceed (tracker and/or live)."
    )


def close_tracker(
    positions: List[Dict[str, Any]],
    scope: str,
    record_close_fn=None,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Mark matching open positions closed in tracker. Returns (updated_list, results)."""
    targets = {id(p) for p in filter_open(positions, scope)}
    results = []
    for p in positions:
        if id(p) not in targets:
            continue
        if p.get("status", "open") != "open":
            continue
        if record_close_fn:
            try:
                record_close_fn(p)
            except Exception:
                pass
        p["status"] = "closed"
        p["closed_at"] = datetime.utcnow().isoformat()
        p["close_scope"] = scope
        results.append(
            f"{p.get('symbol') or '?'}: Confirmed Closed (tracker) origin={normalize_origin(p)}"
        )
    return positions, results


def close_live_each(
    positions: List[Dict[str, Any]],
    scope: str,
    secret: str,
    pubkey: str,
    sell_fn,
    record_close_fn=None,
    pending_ids: Optional[set] = None,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Sell each matching position via sell_fn(mint)->dict. No double-submit if id in pending."""
    pending_ids = pending_ids if pending_ids is not None else set()
    targets = filter_open(positions, scope)
    results = []
    by_id = {str(p.get("id")): p for p in positions if p.get("id") is not None}
    for p in targets:
        pid = str(p.get("id") or "")
        sym = p.get("symbol") or "?"
        mint = p.get("mint") or p.get("token") or ""
        if pid and pid in pending_ids:
            results.append(f"{sym}: Pending (already in-flight)")
            continue
        if not mint:
            results.append(f"{sym}: Unable to Sell (no mint)")
            continue
        if pid:
            pending_ids.add(pid)
        try:
            out = sell_fn(secret, pubkey, mint)
        except Exception as e:
            results.append(f"{sym}: Failed {e}")
            if pid:
                pending_ids.discard(pid)
            continue
        if out.get("ok"):
            if record_close_fn:
                try:
                    record_close_fn(p)
                except Exception:
                    pass
            p["status"] = "closed"
            p["closed_at"] = datetime.utcnow().isoformat()
            p["close_scope"] = scope
            p["close_sig"] = out.get("sig")
            results.append(f"{sym}: Confirmed Closed sig={(out.get('sig') or '')[:16]}")
        else:
            err = out.get("error") or "unknown"
            if out.get("sig") and not out.get("ok"):
                results.append(f"{sym}: Pending/uncertain sig={out.get('sig')[:16]} err={err}")
            else:
                results.append(f"{sym}: Failed {err}")
        if pid:
            pending_ids.discard(pid)
    return positions, results
