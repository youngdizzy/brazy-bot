"""Per-user JSON persistence under data/users/<id>_copy.json — isolated."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import (
    AlertConfig,
    CopySubscription,
    FollowerFill,
    FollowerPosition,
    utc_now,
)

log = logging.getLogger("brazy-copy-storage")


class CopyStorage:
    def __init__(self, base_dir: Path):
        self.base = Path(base_dir)
        self.base.mkdir(parents=True, exist_ok=True)

    def _path(self, user_id: int) -> Path:
        return self.base / f"{int(user_id)}_copy.json"

    def load(self, user_id: int) -> Dict[str, Any]:
        p = self._path(user_id)
        if not p.exists():
            return {
                "subscriptions": {},
                "alerts": {},
                "fills": [],
                "positions": [],
                "seen_events": {},  # sig -> ts
                "daily_loss": {},  # date -> sol loss
                "kill_switch": False,
                "updated_at": utc_now(),
            }
        try:
            with open(p, "r") as f:
                return json.load(f)
        except Exception as e:
            log.warning("load copy store failed %s: %s", user_id, e)
            return {
                "subscriptions": {},
                "alerts": {},
                "fills": [],
                "positions": [],
                "seen_events": {},
                "daily_loss": {},
                "kill_switch": False,
                "updated_at": utc_now(),
            }

    def save(self, user_id: int, data: Dict[str, Any]) -> None:
        data["updated_at"] = utc_now()
        p = self._path(user_id)
        tmp = p.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        tmp.replace(p)

    def list_subscriptions(self, user_id: int) -> List[CopySubscription]:
        raw = self.load(user_id).get("subscriptions") or {}
        out = []
        for w, d in raw.items():
            d = dict(d)
            d["wallet"] = w
            out.append(CopySubscription.from_dict(d))
        return out

    def upsert_subscription(self, user_id: int, sub: CopySubscription) -> None:
        data = self.load(user_id)
        data.setdefault("subscriptions", {})[sub.wallet] = sub.to_dict()
        self.save(user_id, data)

    def remove_subscription(self, user_id: int, wallet: str) -> bool:
        data = self.load(user_id)
        subs = data.get("subscriptions") or {}
        if wallet in subs:
            del subs[wallet]
            data["subscriptions"] = subs
            self.save(user_id, data)
            return True
        return False

    def set_alert(self, user_id: int, cfg: AlertConfig) -> None:
        data = self.load(user_id)
        data.setdefault("alerts", {})[cfg.wallet] = cfg.to_dict()
        self.save(user_id, data)

    def list_alerts(self, user_id: int) -> List[AlertConfig]:
        raw = self.load(user_id).get("alerts") or {}
        out = []
        for w, d in raw.items():
            out.append(AlertConfig(
                wallet=w,
                handle=d.get("handle") or "",
                mode=d.get("mode") or "first_buy",
                enabled=bool(d.get("enabled", True)),
                last_event_sig=d.get("last_event_sig"),
                created_at=d.get("created_at") or utc_now(),
            ))
        return out

    def add_fill(self, user_id: int, fill: FollowerFill) -> None:
        data = self.load(user_id)
        fills = data.get("fills") or []
        fills.append(fill.to_dict())
        data["fills"] = fills[-2000:]  # cap
        self.save(user_id, data)

    def list_fills(self, user_id: int, leader: Optional[str] = None) -> List[Dict[str, Any]]:
        fills = self.load(user_id).get("fills") or []
        if leader:
            fills = [f for f in fills if f.get("leader_wallet") == leader]
        return fills

    def list_positions(self, user_id: int, leader: Optional[str] = None) -> List[Dict[str, Any]]:
        pos = self.load(user_id).get("positions") or []
        if leader:
            pos = [p for p in pos if p.get("leader_wallet") == leader]
        return pos

    def save_positions(self, user_id: int, positions: List[Dict[str, Any]]) -> None:
        data = self.load(user_id)
        data["positions"] = positions
        self.save(user_id, data)

    def seen_event(self, user_id: int, sig: str) -> bool:
        return sig in (self.load(user_id).get("seen_events") or {})

    def mark_event(self, user_id: int, sig: str) -> None:
        data = self.load(user_id)
        seen = data.get("seen_events") or {}
        seen[sig] = utc_now()
        # prune
        if len(seen) > 5000:
            keys = sorted(seen.keys(), key=lambda k: seen[k])[:2000]
            for k in keys:
                del seen[k]
        data["seen_events"] = seen
        self.save(user_id, data)

    def kill_switch(self, user_id: int) -> bool:
        """True = trading halted. Unreadable storage => True (fail closed)."""
        try:
            return bool(self.load(user_id).get("kill_switch"))
        except Exception:
            return True

    def kill_switch_status(self, user_id: int) -> tuple:
        """(active: bool, readable: bool)"""
        try:
            data = self.load(user_id)
            return bool(data.get("kill_switch")), True
        except Exception:
            return True, False

    def set_kill_switch(self, user_id: int, on: bool) -> None:
        data = self.load(user_id)
        data["kill_switch"] = bool(on)
        self.save(user_id, data)
