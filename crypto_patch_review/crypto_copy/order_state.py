"""Durable order state machine for live swaps."""
from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any, Dict, Optional


class OrderState(str, Enum):
    CREATED = "CREATED"
    RISK_CHECKED = "RISK_CHECKED"
    QUOTED = "QUOTED"
    TRANSACTION_BUILT = "TRANSACTION_BUILT"
    VALIDATED = "VALIDATED"
    SIMULATED = "SIMULATED"
    SIGNED = "SIGNED"
    SUBMITTED = "SUBMITTED"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"
    UNKNOWN_REQUIRES_RECONCILIATION = "UNKNOWN_REQUIRES_RECONCILIATION"


class Order:
    def __init__(self, side: str, mint: str, amount_raw: int, meta: Optional[Dict] = None):
        self.id = str(uuid.uuid4())[:16]
        self.side = side
        self.mint = mint
        self.amount_raw = amount_raw
        self.state = OrderState.CREATED
        self.meta = meta or {}
        self.sig: Optional[str] = None
        self.error: Optional[str] = None
        self.ts: Dict[str, float] = {"created": time.time()}
        self.quote: Optional[Dict[str, Any]] = None

    def advance(self, state: OrderState, **kw) -> None:
        self.state = state
        self.ts[state.value.lower()] = time.time()
        if "sig" in kw:
            self.sig = kw["sig"]
        if "error" in kw:
            self.error = kw["error"]
        if "quote" in kw:
            self.quote = kw["quote"]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "side": self.side,
            "mint": self.mint,
            "amount_raw": self.amount_raw,
            "state": self.state.value,
            "sig": self.sig,
            "error": self.error,
            "ts": self.ts,
            "quote": self.quote,
            "meta": self.meta,
        }
