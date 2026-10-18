"""Brazy Bot — Solana FOMO/KOLScan-style copy trading package.

Live execution is OFF by default. Set COPY_LIVE_ENABLED=true only after
burner wallet + Jupiter path verified. Fail-closed otherwise.
"""
from .models import (
    TraderProfile,
    CopyRules,
    CopySubscription,
    AlertConfig,
    FollowerFill,
    FollowerPosition,
)
from .leaderboard import LeaderboardService
from .alerts import AlertService
from .copy_engine import CopyEngine, live_enabled
from .pnl import PnLCalculator
from .storage import CopyStorage
from . import executor
from . import monitor
from . import snipe

__all__ = [
    "TraderProfile",
    "CopyRules",
    "CopySubscription",
    "AlertConfig",
    "FollowerFill",
    "FollowerPosition",
    "LeaderboardService",
    "AlertService",
    "CopyEngine",
    "PnLCalculator",
    "CopyStorage",
    "live_enabled",
    "executor",
    "monitor",
    "snipe",
]
