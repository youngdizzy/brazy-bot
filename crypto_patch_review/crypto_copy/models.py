"""Data models for copy-trading. Follower PnL is NEVER the leader's PnL."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class TraderProfile:
    wallet: str
    handle: str = ""
    followers: Optional[int] = None
    pnl_24h: Optional[float] = None
    pnl_7d: Optional[float] = None
    pnl_30d: Optional[float] = None
    realized_pnl: Optional[float] = None
    unrealized_pnl: Optional[float] = None
    win_rate: Optional[float] = None  # 0-100
    closed_trades: Optional[int] = None
    avg_trade_size_usd: Optional[float] = None
    trade_count: Optional[int] = None
    active_copiers: Optional[int] = None
    source: str = "demo"
    updated_at: str = field(default_factory=utc_now)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TraderProfile":
        kwargs = {}
        for k in cls.__dataclass_fields__:
            if k in (d or {}):
                kwargs[k] = d[k]
        if "wallet" not in kwargs:
            kwargs["wallet"] = (d or {}).get("wallet") or ""
        return cls(**kwargs)


@dataclass
class CopyRules:
    """Risk rules for copying one leader. Server is source of truth."""
    sizing_mode: str = "fixed"  # fixed | percent_of_leader
    fixed_amount_sol: float = 0.05
    percent_of_leader: float = 10.0
    max_per_trade_sol: float = 0.25
    max_exposure_sol: float = 1.0
    max_concurrent: int = 3
    max_slippage_bps: int = 150  # 1.5%
    max_priority_fee_sol: float = 0.001
    daily_loss_limit_sol: float = 0.2
    min_liquidity_usd: float = 15000.0
    min_token_age_min: int = 5
    copy_delay_sec: int = 2
    stale_trade_sec: int = 45
    side_mode: str = "buy_only"  # buy_only | buy_and_sell
    use_existing_risk: bool = True
    no_rug: bool = True
    anti_mev: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CopyRules":
        base = cls()
        for k, v in (d or {}).items():
            if hasattr(base, k) and v is not None:
                setattr(base, k, v)
        return base

    def validate(self) -> List[str]:
        errs = []
        if self.fixed_amount_sol <= 0 and self.sizing_mode == "fixed":
            errs.append("fixed_amount_sol must be > 0")
        if self.max_per_trade_sol < self.fixed_amount_sol and self.sizing_mode == "fixed":
            errs.append("max_per_trade_sol must be >= fixed_amount_sol")
        if self.max_exposure_sol <= 0:
            errs.append("max_exposure_sol must be > 0")
        if self.max_concurrent < 1:
            errs.append("max_concurrent must be >= 1")
        if self.max_slippage_bps < 10 or self.max_slippage_bps > 2000:
            errs.append("max_slippage_bps out of range (10-2000)")
        if self.daily_loss_limit_sol <= 0:
            errs.append("daily_loss_limit_sol must be > 0")
        if self.side_mode not in ("buy_only", "buy_and_sell"):
            errs.append("side_mode must be buy_only or buy_and_sell")
        if self.copy_delay_sec < 0 or self.copy_delay_sec > 120:
            errs.append("copy_delay_sec out of range")
        return errs


@dataclass
class AlertConfig:
    wallet: str
    handle: str = ""
    mode: str = "first_buy"  # first_buy | all_buys | buys_and_sells
    enabled: bool = True
    last_event_sig: Optional[str] = None
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CopySubscription:
    wallet: str
    handle: str = ""
    status: str = "paused"  # paused | active | stopped
    rules: CopyRules = field(default_factory=CopyRules)
    started_at: Optional[str] = None
    last_activity: Optional[str] = None
    paper: bool = True  # always True unless live explicitly enabled
    failed_copies: int = 0
    missed_trades: int = 0

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["rules"] = self.rules.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CopySubscription":
        rules = CopyRules.from_dict(d.get("rules") or {})
        return cls(
            wallet=d.get("wallet", ""),
            handle=d.get("handle") or "",
            status=d.get("status") or "paused",
            rules=rules,
            started_at=d.get("started_at"),
            last_activity=d.get("last_activity"),
            paper=bool(d.get("paper", True)),
            failed_copies=int(d.get("failed_copies") or 0),
            missed_trades=int(d.get("missed_trades") or 0),
        )


@dataclass
class FollowerFill:
    """One fill belonging to the FOLLOWER (you), not the leader."""
    id: str
    leader_wallet: str
    mint: str
    symbol: str
    side: str  # buy | sell
    qty: float
    price_usd: float
    fee_usd: float
    slip_bps: float
    ts: str
    paper: bool = True
    leader_sig: Optional[str] = None
    follower_sig: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class FollowerPosition:
    leader_wallet: str
    mint: str
    symbol: str
    qty: float
    avg_entry_usd: float
    realized_pnl_usd: float = 0.0
    fees_usd: float = 0.0
    status: str = "open"  # open | closed
    opened_at: str = field(default_factory=utc_now)
    closed_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
