"""Wallet trade alerts — notify only, never authorize trades."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .models import AlertConfig, utc_now
from .storage import CopyStorage

log = logging.getLogger("brazy-alerts")


class AlertService:
    def __init__(self, storage: CopyStorage):
        self.storage = storage

    def set_mode(self, user_id: int, wallet: str, handle: str, mode: str) -> AlertConfig:
        if mode not in ("first_buy", "all_buys", "buys_and_sells"):
            mode = "first_buy"
        cfg = AlertConfig(wallet=wallet, handle=handle, mode=mode, enabled=True)
        self.storage.set_alert(user_id, cfg)
        return cfg

    def disable(self, user_id: int, wallet: str) -> None:
        cfg = AlertConfig(wallet=wallet, enabled=False)
        self.storage.set_alert(user_id, cfg)

    def should_notify(self, user_id: int, wallet: str, side: str, sig: str) -> bool:
        if self.storage.seen_event(user_id, f"alert:{sig}"):
            return False
        for cfg in self.storage.list_alerts(user_id):
            if cfg.wallet != wallet or not cfg.enabled:
                continue
            if cfg.mode == "first_buy":
                if side != "buy":
                    return False
                # only if never alerted this wallet buy before
                if cfg.last_event_sig:
                    return False
                return True
            if cfg.mode == "all_buys":
                return side == "buy"
            if cfg.mode == "buys_and_sells":
                return side in ("buy", "sell")
        return False

    def mark_notified(self, user_id: int, wallet: str, sig: str) -> None:
        self.storage.mark_event(user_id, f"alert:{sig}")
        data = self.storage.load(user_id)
        alerts = data.get("alerts") or {}
        if wallet in alerts:
            alerts[wallet]["last_event_sig"] = sig
            data["alerts"] = alerts
            self.storage.save(user_id, data)

    def format_alert(self, handle: str, wallet: str, side: str, symbol: str, amount: str) -> str:
        return (
            f"ALERT  {handle or wallet[:8]}\n"
            f"{side.upper()} {symbol}  {amount}\n"
            f"wallet: {wallet}\n"
            f"{utc_now()[:19]}Z\n"
            f"(alert only — not a trade)"
        )
