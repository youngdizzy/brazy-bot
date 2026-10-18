"""Background copy monitor + scan service.

Does NOT enable live trading. Starts only when AUTO_COPY_MONITOR=true
or when start() is called from bot lifecycle.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from .copy_engine import CopyEngine, live_enabled
from .storage import CopyStorage
from .monitor import WalletMonitor
from .scan_ui import discover_memes

log = logging.getLogger("brazy-automation")


@dataclass
class Health:
    running: bool = False
    last_poll_ts: float = 0.0
    last_ok_ts: float = 0.0
    last_error: str = ""
    wallets: int = 0
    events_seen: int = 0
    copied: int = 0
    skipped: int = 0
    failed: int = 0
    scan_last_ts: float = 0.0
    scan_candidates: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "running": self.running,
            "last_poll_ts": self.last_poll_ts,
            "last_ok_ts": self.last_ok_ts,
            "last_error": self.last_error,
            "wallets": self.wallets,
            "events_seen": self.events_seen,
            "copied": self.copied,
            "skipped": self.skipped,
            "failed": self.failed,
            "scan_last_ts": self.scan_last_ts,
            "scan_candidates": self.scan_candidates,
            "live_enabled": live_enabled(),
        }


class AutomationService:
    """Single-worker async loop: poll active copy wallets; optional scan."""

    def __init__(self, data_dir: Path, interval_sec: float = 45.0):
        self.data_dir = Path(data_dir)
        self.interval = max(15.0, float(interval_sec))
        self.storage = CopyStorage(self.data_dir / "copy")
        self.engine = CopyEngine(self.storage)
        self.monitor = WalletMonitor()
        self.health = Health()
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()
        # user_id -> (secret, pubkey) provider callback set by bot
        self.credential_provider: Optional[Callable[[int], tuple]] = None
        self.user_ids: List[int] = []

    def configure_users(self, user_ids: List[int]) -> None:
        self.user_ids = list(user_ids)

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self.health.running = True
        self._task = asyncio.create_task(self._loop(), name="brazy-auto")
        log.info("automation started interval=%s", self.interval)

    async def stop(self) -> None:
        self._stop.set()
        self.health.running = False
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except Exception:
                self._task.cancel()
        log.info("automation stopped")

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
                self.health.last_ok_ts = time.time()
                self.health.last_error = ""
            except Exception as e:
                self.health.last_error = str(e)[:200]
                log.exception("automation tick")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass

    async def tick(self) -> List[str]:
        """One poll cycle for all configured users."""
        async with self._lock:
            self.health.last_poll_ts = time.time()
            lines: List[str] = []
            for uid in self.user_ids:
                secret, pub = None, None
                if self.credential_provider:
                    try:
                        secret, pub = self.credential_provider(uid)
                    except Exception:
                        secret, pub = None, None
                sub_lines = self.engine.run_poll_cycle(uid, secret, pub)
                for ln in sub_lines:
                    lines.append(ln)
                    self.health.events_seen += 1
                    if "OK" in ln:
                        self.health.copied += 1
                    elif "SKIP" in ln:
                        self.health.skipped += 1
                # count wallets
                n = len([s for s in self.storage.list_subscriptions(uid) if s.status == "active"])
                self.health.wallets = max(self.health.wallets, n)
            # light scan (discovery only — no auto-buy)
            if os.getenv("AUTO_SCAN_DISCOVER", "").lower() in ("1", "true", "yes"):
                coins, _ = discover_memes(limit=5)
                self.health.scan_last_ts = time.time()
                self.health.scan_candidates = len(coins)
            return lines


_SERVICE: Optional[AutomationService] = None


def get_automation(data_dir: Path) -> AutomationService:
    global _SERVICE
    if _SERVICE is None:
        interval = float(os.getenv("AUTO_COPY_INTERVAL_SEC", "45"))
        _SERVICE = AutomationService(data_dir, interval_sec=interval)
    return _SERVICE


def auto_enabled() -> bool:
    return os.getenv("AUTO_COPY_MONITOR", "").lower() in ("1", "true", "yes")
