"""Copy engine — paper by default. Live Jupiter path when COPY_LIVE_ENABLED=true.

Secret keys are passed per-call only and never logged.
Live failures never silently become paper fills.
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from .models import CopyRules, CopySubscription, FollowerFill, utc_now
from .pnl import PnLCalculator
from .storage import CopyStorage
from . import executor as ex
from .risk_limits import RiskSnapshot, check_before_sign

log = logging.getLogger("brazy-copy-engine")


def live_enabled() -> bool:
    return os.getenv("COPY_LIVE_ENABLED", "").lower() in ("1", "true", "yes")


class CopyEngine:
    def __init__(self, storage: CopyStorage):
        self.storage = storage
        self.pnl = PnLCalculator()

    def review_text(self, sub: CopySubscription) -> str:
        r = sub.rules
        mode = "PAPER/SIM" if sub.paper or not live_enabled() else "LIVE (dangerous)"
        return "\n".join([
            "COPY SETUP — REVIEW",
            f"trader: {sub.handle or sub.wallet}",
            f"wallet: {sub.wallet}",
            f"execution: {mode}",
            f"status after confirm: active (still paper unless live flag set)",
            "",
            "— sizing —",
            f"mode: {r.sizing_mode}",
            f"fixed SOL: {r.fixed_amount_sol}",
            f"max per trade SOL: {r.max_per_trade_sol}",
            f"max exposure SOL: {r.max_exposure_sol}",
            f"max concurrent: {r.max_concurrent}",
            "",
            "— risk —",
            f"slippage max: {r.max_slippage_bps} bps",
            f"priority fee max: {r.max_priority_fee_sol} SOL",
            f"daily loss limit: {r.daily_loss_limit_sol} SOL",
            f"min liquidity USD: {r.min_liquidity_usd}",
            f"min token age min: {r.min_token_age_min}",
            f"copy delay: {r.copy_delay_sec}s  stale after: {r.stale_trade_sec}s",
            f"sides: {r.side_mode}",
            f"no_rug: {r.no_rug}  anti_mev: {r.anti_mev}",
            "",
            "RISKS: slippage, failed tx, rugs, MEV, RPC lag, partial fills.",
            "Insufficient funds → skip + miss counter.",
            "Sell fail → position remains; manual intervention.",
            "",
            "Confirm to activate, or Cancel.",
        ])

    def activate(self, user_id: int, sub: CopySubscription) -> Tuple[bool, str]:
        errs = sub.rules.validate()
        if errs:
            return False, "Invalid rules: " + "; ".join(errs)
        if self.storage.kill_switch(user_id):
            return False, "Kill switch is ON — turn off before activating"
        # Force paper unless live explicitly enabled
        if not live_enabled():
            sub.paper = True
        sub.status = "active"
        sub.started_at = sub.started_at or utc_now()
        sub.last_activity = utc_now()
        self.storage.upsert_subscription(user_id, sub)
        return True, "Copy ACTIVE" + (" (PAPER/SIM — no real orders)" if sub.paper else " (LIVE)")

    def pause(self, user_id: int, wallet: str) -> None:
        for s in self.storage.list_subscriptions(user_id):
            if s.wallet == wallet:
                s.status = "paused"
                self.storage.upsert_subscription(user_id, s)
                return

    def stop(self, user_id: int, wallet: str) -> None:
        for s in self.storage.list_subscriptions(user_id):
            if s.wallet == wallet:
                s.status = "stopped"
                self.storage.upsert_subscription(user_id, s)
                return

    def emergency_kill(self, user_id: int) -> None:
        self.storage.set_kill_switch(user_id, True)
        for s in self.storage.list_subscriptions(user_id):
            if s.status == "active":
                s.status = "paused"
                self.storage.upsert_subscription(user_id, s)

    def validate_event(
        self,
        user_id: int,
        sub: CopySubscription,
        event: Dict[str, Any],
    ) -> Tuple[bool, str]:
        """event: {sig, side, mint, symbol, price_usd, liquidity_usd, age_min, ts}"""
        if self.storage.kill_switch(user_id):
            return False, "kill_switch"
        if sub.status != "active":
            return False, "not_active"
        sig = event.get("sig") or ""
        if not sig:
            return False, "no_sig"
        if self.storage.seen_event(user_id, f"copy:{sig}"):
            return False, "duplicate"
        side = event.get("side")
        if side not in ("buy", "sell"):
            return False, "bad_side"
        r = sub.rules
        if r.side_mode == "buy_only" and side != "buy":
            return False, "sell_disabled"
        age = event.get("age_sec")
        if age is not None and age > r.stale_trade_sec:
            return False, "stale"
        liq = event.get("liquidity_usd")
        if liq is not None and liq < r.min_liquidity_usd:
            return False, "low_liquidity"
        token_age = event.get("token_age_min")
        if token_age is not None and token_age < r.min_token_age_min:
            return False, "token_too_new"
        # concurrent
        opens = [p for p in self.storage.list_positions(user_id, sub.wallet) if p.get("status") == "open"]
        if side == "buy" and len(opens) >= r.max_concurrent:
            return False, "max_concurrent"
        # exposure
        exposure = sum(float(p.get("avg_entry_usd") or 0) * float(p.get("qty") or 0) for p in opens)
        # rough SOL exposure check skipped without SOL/USD — use USD notional vs max_exposure * placeholder
        return True, "ok"

    def simulate_copy(
        self,
        user_id: int,
        sub: CopySubscription,
        event: Dict[str, Any],
    ) -> Tuple[bool, str, Optional[FollowerFill]]:
        """Paper fill. Never broadcasts."""
        ok, reason = self.validate_event(user_id, sub, event)
        if not ok:
            if reason == "duplicate":
                return False, reason, None
            sub.missed_trades += 1
            self.storage.upsert_subscription(user_id, sub)
            self.storage.mark_event(user_id, f"copy:{event.get('sig')}")
            return False, reason, None

        r = sub.rules
        # sizing in SOL terms stored as qty using price
        amount_sol = min(r.fixed_amount_sol, r.max_per_trade_sol)
        price = float(event.get("price_usd") or 0) or 1.0
        # qty in tokens ≈ (amount_sol * sol_usd) / price — without oracle use amount_sol as notional USD proxy
        notional_usd = amount_sol * float(os.getenv("SOL_USD_HINT", "150"))
        qty = notional_usd / price if price else 0
        fee = notional_usd * 0.003
        slip = min(r.max_slippage_bps, 50)

        fill = FollowerFill(
            id=str(uuid.uuid4()),
            leader_wallet=sub.wallet,
            mint=event.get("mint") or "",
            symbol=event.get("symbol") or "?",
            side=event["side"],
            qty=qty,
            price_usd=price,
            fee_usd=fee,
            slip_bps=float(slip),
            ts=utc_now(),
            paper=True,
            leader_sig=event.get("sig"),
            follower_sig=f"paper:{uuid.uuid4().hex[:12]}",
        )
        self.storage.add_fill(user_id, fill)
        positions = self.storage.list_positions(user_id)
        positions = self.pnl.apply_fill_to_positions(positions, fill.to_dict())
        self.storage.save_positions(user_id, positions)
        self.storage.mark_event(user_id, f"copy:{event.get('sig')}")
        sub.last_activity = utc_now()
        self.storage.upsert_subscription(user_id, sub)
        log.info("paper copy user=%s leader=%s side=%s", user_id, sub.wallet[:8], fill.side)
        return True, "paper_fill", fill


    def process_event(
        self,
        user_id: int,
        sub: CopySubscription,
        event: Dict[str, Any],
        secret_base58: Optional[str] = None,
        user_pubkey: Optional[str] = None,
    ) -> Tuple[bool, str, Optional[FollowerFill]]:
        """Route to paper or live. Never silent paper when live was requested."""
        # Explicit paper subscription → paper only
        if sub.paper:
            return self.simulate_copy(user_id, sub, event)
        # Live subscription path
        if not live_enabled():
            return False, "live_disabled", None
        if not secret_base58 or not user_pubkey:
            return False, "live_requested_but_missing_wallet_secret_or_pubkey", None
        return self.execute_live(user_id, sub, event, secret_base58, user_pubkey)

    def execute_live(
        self,
        user_id: int,
        sub: CopySubscription,
        event: Dict[str, Any],
        secret_base58: str,
        user_pubkey: str,
    ) -> Tuple[bool, str, Optional[FollowerFill]]:
        """Real Jupiter swap. Never reports paper as live."""
        if not live_enabled():
            return False, "live_disabled", None
        ok, reason = self.validate_event(user_id, sub, event)
        if not ok:
            if reason != "duplicate":
                sub.missed_trades += 1
                self.storage.upsert_subscription(user_id, sub)
            return False, reason, None

        r = sub.rules
        side = event["side"]
        mint = event.get("mint") or ""
        if not mint:
            return False, "no_mint", None

        kill, readable = True, False
        try:
            kill, readable = self.storage.kill_switch_status(user_id)
        except Exception:
            kill, readable = True, False
        opens = [p for p in self.storage.list_positions(user_id) if p.get("status") == "open"]
        snap = RiskSnapshot(
            max_trade_sol=r.max_per_trade_sol,
            max_daily_loss_sol=r.daily_loss_limit_sol,
            max_open_positions=r.max_concurrent,
            max_exposure_sol=r.max_exposure_sol,
            max_slippage_bps=r.max_slippage_bps,
            open_positions=len(opens),
            exposure_sol=0.0,
            kill_switch=kill,
            kill_switch_readable=readable,
        )
        amount_sol = min(r.fixed_amount_sol, r.max_per_trade_sol)
        ok_r, reason_r = check_before_sign(side, amount_sol, None, r.max_slippage_bps, snap)
        if not ok_r:
            sub.missed_trades += 1
            self.storage.upsert_subscription(user_id, sub)
            return False, reason_r, None

        # Delay
        if r.copy_delay_sec > 0:
            time.sleep(min(r.copy_delay_sec, 15))

        amount_sol = min(r.fixed_amount_sol, r.max_per_trade_sol)
        bal = ex.get_balance_sol(user_pubkey)
        if bal is not None and bal < amount_sol + 0.005:
            sub.missed_trades += 1
            self.storage.upsert_subscription(user_id, sub)
            return False, "insufficient_sol", None

        if side == "buy":
            result = ex.buy_token_sol(
                secret_base58,
                user_pubkey,
                mint,
                amount_sol,
                slippage_bps=r.max_slippage_bps,
            )
        else:
            # sell only tokens we hold — never more
            result = ex.sell_token_pct(
                secret_base58,
                user_pubkey,
                mint,
                pct=100.0,
                slippage_bps=r.max_slippage_bps,
            )

        if not result.get("ok"):
            sub.failed_copies += 1
            self.storage.upsert_subscription(user_id, sub)
            # mark event to avoid retry storm on same sig
            self.storage.mark_event(user_id, f"copy:{event.get('sig')}")
            return False, result.get("error") or "live_fail", None

        price = float(event.get("price_usd") or 0) or 1.0
        notional_usd = amount_sol * float(os.getenv("SOL_USD_HINT", "150"))
        qty = notional_usd / price if side == "buy" else float(event.get("ui_amount") or 0)
        fill = FollowerFill(
            id=str(uuid.uuid4()),
            leader_wallet=sub.wallet,
            mint=mint,
            symbol=event.get("symbol") or "?",
            side=side,
            qty=qty,
            price_usd=price,
            fee_usd=notional_usd * 0.003,
            slip_bps=float(r.max_slippage_bps),
            ts=utc_now(),
            paper=False,
            leader_sig=event.get("sig"),
            follower_sig=result.get("sig"),
        )
        self.storage.add_fill(user_id, fill)
        positions = self.storage.list_positions(user_id)
        positions = self.pnl.apply_fill_to_positions(positions, fill.to_dict())
        self.storage.save_positions(user_id, positions)
        self.storage.mark_event(user_id, f"copy:{event.get('sig')}")
        sub.last_activity = utc_now()
        sub.paper = False
        self.storage.upsert_subscription(user_id, sub)
        log.info("LIVE copy user=%s sig=%s side=%s", user_id, result.get("sig"), side)
        return True, "live_fill", fill

    def run_poll_cycle(
        self,
        user_id: int,
        secret_base58: Optional[str] = None,
        user_pubkey: Optional[str] = None,
    ) -> List[str]:
        """Poll all active subs; process new events. Returns status lines."""
        from .monitor import WalletMonitor
        mon = WalletMonitor()
        lines = []
        for sub in self.storage.list_subscriptions(user_id):
            if sub.status != "active":
                continue
            for ev in mon.poll_wallet(sub.wallet):
                ok, reason, fill = self.process_event(
                    user_id, sub, ev, secret_base58, user_pubkey
                )
                lines.append(
                    f"{sub.handle or sub.wallet[:8]} {ev.get('side')} "
                    f"{'OK' if ok else 'SKIP'} {reason} "
                    f"{(fill.follower_sig if fill else '')}"
                )
        return lines

    def copy_list_rows(self, user_id: int) -> str:
        subs = self.storage.list_subscriptions(user_id)
        if not subs:
            return "COPY LIST empty\nAdd from leaderboard → Copy Trader"
        lines = ["COPY LIST — YOUR results (not leader PnL)", ""]
        for s in subs:
            fills = self.storage.list_fills(user_id, s.wallet)
            positions = self.storage.list_positions(user_id, s.wallet)
            st = self.pnl.stats_for_leader(fills, positions)
            lines.append(f"{s.handle or s.wallet[:8]}  [{s.status}]")
            lines.append(f"  leader wallet: {s.wallet[:6]}…{s.wallet[-4:]}")
            lines.append(f"  my realized: ${st['my_realized_pnl']}")
            if st["my_unrealized_pnl"] is not None:
                lines.append(f"  my unrealized: ${st['my_unrealized_pnl']}")
            lines.append(f"  my total: ${st['my_total_pnl']}")
            wr = f"{st['my_win_rate']}%" if st["my_win_rate"] is not None else "N/A"
            lines.append(f"  my win rate: {wr} (n={st['sample_size']} closed)")
            lines.append(f"  open: {st['open_positions']}  exposure~$ {st['exposure_usd']}")
            lines.append(f"  fees: ${st['fees_usd']}  failed: {s.failed_copies}  missed: {s.missed_trades}")
            lines.append(f"  started: {s.started_at or 'N/A'}  last: {s.last_activity or 'N/A'}")
            lines.append(f"  mode: {'PAPER' if s.paper else 'LIVE'}")
            lines.append("")
        if self.storage.kill_switch(user_id):
            lines.append("KILL SWITCH ON — all copy paused")
        return "\n".join(lines)
