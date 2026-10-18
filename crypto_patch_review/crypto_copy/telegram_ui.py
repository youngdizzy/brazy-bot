"""Telegram keyboards + handlers for FOMO/KOL leaderboard & copy wizard."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .alerts import AlertService
from .copy_engine import CopyEngine, live_enabled
from .leaderboard import LeaderboardService
from .models import CopyRules, CopySubscription, TraderProfile
from .storage import CopyStorage

try:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
except ImportError:
    InlineKeyboardButton = None  # type: ignore
    InlineKeyboardMarkup = None  # type: ignore


def default_storage(data_dir: Path) -> CopyStorage:
    return CopyStorage(Path(data_dir) / "copy")


class CopyTelegramUI:
    def __init__(self, data_dir: Path):
        self.storage = default_storage(data_dir)
        self.board = LeaderboardService()
        self.engine = CopyEngine(self.storage)
        self.alerts = AlertService(self.storage)
        # wizard state in memory (per process); also ok in context.user_data
        self._wizard: Dict[int, Dict[str, Any]] = {}

    def leaderboard_keyboard(self, period: str = "7d", page: int = 0) -> Any:
        traders, source = self.board.fetch(period=period, limit=8, offset=page * 8)
        rows = []
        # period filters
        rows.append([
            InlineKeyboardButton("24h", callback_data=f"clb_p_24h_{page}"),
            InlineKeyboardButton("7d", callback_data=f"clb_p_7d_{page}"),
            InlineKeyboardButton("30d", callback_data=f"clb_p_30d_{page}"),
        ])
        # two columns of traders
        btns = []
        for i, t in enumerate(traders):
            label = f"{t.handle}"[:16]
            btns.append(InlineKeyboardButton(label, callback_data=f"clb_t_{period}_{page}_{i}"))
        for i in range(0, len(btns), 2):
            rows.append(btns[i:i + 2])
        rows.append([
            InlineKeyboardButton("Next", callback_data=f"clb_next_{period}_{page}"),
            InlineKeyboardButton("Copy list", callback_data="copy_list_btn"),
        ])
        rows.append([InlineKeyboardButton("< Sol desk", callback_data="hub_crypto")])
        text = self.board.format_leaderboard(traders, period, source, page)
        # stash traders for profile resolve
        self._page_cache = getattr(self, "_page_cache", {})
        self._page_cache[f"{period}_{page}"] = traders
        return text, InlineKeyboardMarkup(rows)

    def profile_keyboard(self, wallet: str, handle: str) -> Any:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Copy Trader", callback_data=f"ccopy_start_{wallet[:32]}"),
                InlineKeyboardButton("Get Alerts", callback_data=f"calert_pick_{wallet[:32]}"),
            ],
            [
                InlineKeyboardButton("Back", callback_data="fomo_btn"),
                InlineKeyboardButton("Menu", callback_data="hub_crypto"),
            ],
        ])

    def alert_mode_keyboard(self, wallet: str) -> Any:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("First buy", callback_data=f"calert_mode_first_buy_{wallet[:32]}")],
            [InlineKeyboardButton("All buys", callback_data=f"calert_mode_all_buys_{wallet[:32]}")],
            [InlineKeyboardButton("Buys and sells", callback_data=f"calert_mode_buys_and_sells_{wallet[:32]}")],
            [InlineKeyboardButton("Back", callback_data="fomo_btn")],
        ])

    def wizard_start(self, user_id: int, wallet: str, handle: str, existing_risk: Optional[Dict] = None) -> Tuple[str, Any]:
        rules = CopyRules()
        if existing_risk:
            # map brazy risk_preset lightly
            preset = (existing_risk.get("risk_preset") or "medium").lower()
            if preset == "chill":
                rules.fixed_amount_sol = 0.02
                rules.max_per_trade_sol = 0.1
                rules.max_exposure_sol = 0.4
            elif preset == "degen":
                rules.fixed_amount_sol = 0.15
                rules.max_per_trade_sol = 0.5
                rules.max_exposure_sol = 2.0
            rules.no_rug = bool(existing_risk.get("no_rug_mode", True))
            rules.anti_mev = bool(existing_risk.get("anti_mev", True))
        self._wizard[user_id] = {
            "wallet": wallet,
            "handle": handle,
            "rules": rules.to_dict(),
            "step": "choose_rules",
        }
        text = (
            f"COPY WIZARD — {handle}\n"
            f"{wallet}\n\n"
            "Does NOT start copying until you confirm.\n"
            "Choose rules:"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Use existing risk rules (default)", callback_data="ccopy_rules_existing")],
            [InlineKeyboardButton("Custom rules", callback_data="ccopy_rules_custom")],
            [InlineKeyboardButton("Cancel", callback_data="copy_list_btn")],
        ])
        return text, kb

    def wizard_custom_prompt(self) -> Tuple[str, Any]:
        text = (
            "CUSTOM RULES — send one message:\n"
            "fixed_sol max_trade max_exposure max_concurrent slippage_bps daily_loss_sol\n"
            "Example: 0.05 0.25 1.0 3 150 0.2\n"
            "Or tap defaults below."
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Use defaults", callback_data="ccopy_rules_defaults")],
            [InlineKeyboardButton("Cancel", callback_data="copy_list_btn")],
        ])
        return text, kb

    def wizard_review(self, user_id: int) -> Tuple[str, Any]:
        w = self._wizard.get(user_id) or {}
        rules = CopyRules.from_dict(w.get("rules") or {})
        sub = CopySubscription(
            wallet=w.get("wallet", ""),
            handle=w.get("handle") or "",
            status="paused",
            rules=rules,
            paper=not live_enabled(),
        )
        text = self.engine.review_text(sub)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("CONFIRM ACTIVATE", callback_data="ccopy_confirm")],
            [InlineKeyboardButton("Cancel", callback_data="copy_list_btn")],
        ])
        return text, kb

    def wizard_confirm(self, user_id: int) -> str:
        w = self._wizard.get(user_id)
        if not w:
            return "Wizard expired. Start again from profile."
        rules = CopyRules.from_dict(w.get("rules") or {})
        errs = rules.validate()
        if errs:
            return "Invalid: " + "; ".join(errs)
        sub = CopySubscription(
            wallet=w["wallet"],
            handle=w.get("handle") or "",
            status="paused",
            rules=rules,
            paper=not live_enabled(),
        )
        ok, msg = self.engine.activate(user_id, sub)
        self._wizard.pop(user_id, None)
        return msg if ok else msg

    def resolve_trader(self, period: str, page: int, idx: int) -> Optional[TraderProfile]:
        cache = getattr(self, "_page_cache", {})
        traders = cache.get(f"{period}_{page}") or []
        if 0 <= idx < len(traders):
            return traders[idx]
        return None
