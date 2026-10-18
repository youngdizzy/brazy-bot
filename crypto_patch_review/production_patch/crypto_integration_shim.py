"""
Production integration shim for /opt/brazy/brazy_bot_fixed_v3.py

Import this from the production entrypoint (do not replace the whole bot file).

Usage in brazy_bot_fixed_v3.py (minimal):

    from crypto_integration_shim import (
        register_crypto_handlers,
        maybe_start_copy_monitor,
        handle_crypto_callback,
        CRYPTO_CALLBACK_PREFIXES,
    )

    # inside main(), after Application is built:
    register_crypto_handlers(app)

    # after scheduler start / before run_polling:
    maybe_start_copy_monitor(data_dir=Path("data"), owner_ids=OWNER_IDS)

    # at the top of your existing CallbackQueryHandler body:
    # if data.startswith(CRYPTO_CALLBACK_PREFIXES): handled = await handle_crypto_callback(...); if handled: return

Live trading stays OFF unless COPY_LIVE_ENABLED=true in environment.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

log = logging.getLogger("brazy-crypto-shim")

CRYPTO_CALLBACK_PREFIXES = (
    "pos_close_scope_",
    "pos_cconf_",
    "pos_view_",
    "pos_iclose_",
    "pos_idone_",
    "pos_close_",
    "pos_menu",
    "crypto_scan",
    "mscan_",
    "msnipe_",
    "fomo_btn",
    "copy_list_btn",
    "copy_kill",
    "funded_account",  # stocks — leave to main bot if present
)

# Defaults — never force live
os.environ.setdefault("COPY_LIVE_ENABLED", "false")
os.environ.setdefault("AUTO_COPY_MONITOR", "false")


def live_enabled() -> bool:
    return os.getenv("COPY_LIVE_ENABLED", "").lower() in ("1", "true", "yes")


def register_crypto_handlers(app: Any) -> None:
    """Register /copypoll /copystatus /autohealth if CommandHandler available."""
    try:
        from telegram.ext import CommandHandler
    except Exception as e:
        log.warning("telegram not available for register: %s", e)
        return

    async def copypoll(update, context):
        from crypto_copy.copy_engine import CopyEngine, live_enabled as le
        from crypto_copy.storage import CopyStorage
        uid = update.effective_user.id
        data_dir = Path(os.getenv("BRAZY_DATA_DIR", "data"))
        engine = CopyEngine(CopyStorage(data_dir / "copy"))
        # credentials must be supplied by production profile loader — not here
        secret = context.user_data.get("sol_burner_key")
        pub = context.user_data.get("sol_wallet")
        lines = engine.run_poll_cycle(uid, secret, pub)
        mode = "LIVE" if le() else "PAPER/OFF"
        await update.message.reply_text(
            ("COPY POLL (%s)\n" % mode) + ("\n".join(lines) if lines else "No new events.")
        )

    async def copystatus(update, context):
        from crypto_copy.copy_engine import live_enabled as le
        await update.message.reply_text(
            "COPY STATUS\nlive_flag: %s\nauto: %s"
            % (le(), os.getenv("AUTO_COPY_MONITOR", "false"))
        )

    async def autohealth(update, context):
        try:
            from crypto_copy.automation import get_automation, auto_enabled
            data_dir = Path(os.getenv("BRAZY_DATA_DIR", "data"))
            h = get_automation(data_dir).health.as_dict()
            h["auto_flag"] = auto_enabled()
            await update.message.reply_text("\n".join("%s: %s" % (k, v) for k, v in h.items()))
        except Exception as e:
            await update.message.reply_text("autohealth: %s" % e)

    app.add_handler(CommandHandler("copypoll", copypoll))
    app.add_handler(CommandHandler("copystatus", copystatus))
    app.add_handler(CommandHandler("autohealth", autohealth))
    log.info("crypto command handlers registered (live=%s)", live_enabled())


def maybe_start_copy_monitor(
    data_dir: Path,
    owner_ids: Optional[Sequence[int]] = None,
    credential_provider: Optional[Callable[[int], Tuple[Optional[str], Optional[str]]]] = None,
) -> None:
    """Start background monitor only if AUTO_COPY_MONITOR=true. Single worker."""
    if os.getenv("AUTO_COPY_MONITOR", "").lower() not in ("1", "true", "yes"):
        log.info("AUTO_COPY_MONITOR off — monitor not started")
        return
    try:
        import asyncio
        from crypto_copy.automation import get_automation
        auto = get_automation(Path(data_dir))
        if owner_ids:
            auto.configure_users(list(owner_ids))
        if credential_provider:
            auto.credential_provider = credential_provider
        if auto.health.running:
            log.warning("monitor already running — skip duplicate")
            return
        loop = asyncio.get_event_loop()
        loop.create_task(auto.start())
        log.info("copy monitor task scheduled")
    except Exception as e:
        log.warning("monitor start failed: %s", e)


async def handle_crypto_callback(
    query,
    data: str,
    user_id: int,
    profile: Dict[str, Any],
    update_user_profile: Callable,
    get_user_profile: Callable,
    record_crypto_close: Optional[Callable] = None,
    context: Any = None,
) -> bool:
    """
    Handle crypto close/scan callbacks. Returns True if handled.
    Production button_handler should call this first for CRYPTO_CALLBACK_PREFIXES.
    """
    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    except Exception:
        return False

    async def safe_edit(msg, kb=None):
        try:
            await query.edit_message_text(msg[:4000], reply_markup=kb)
        except Exception:
            await query.message.reply_text(msg[:4000], reply_markup=kb)

    # --- scope closes ---
    if data.startswith("pos_close_scope_"):
        from crypto_copy.close_positions import filter_open, confirm_text
        scope = data.replace("pos_close_scope_", "")
        opens = filter_open(profile.get("sol_positions") or [], scope)
        if not opens:
            await safe_edit("No open positions in this scope.")
            return True
        msg = confirm_text(scope, opens)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("CONFIRM (tracker only)", callback_data=f"pos_cconf_{scope}_track")],
            [InlineKeyboardButton("CONFIRM + LIVE SELL", callback_data=f"pos_cconf_{scope}_live")],
            [InlineKeyboardButton("Cancel", callback_data="pos_menu")],
        ])
        await safe_edit(msg, kb)
        return True

    if data.startswith("pos_cconf_"):
        from crypto_copy.close_positions import close_tracker, close_live_each
        from crypto_copy.copy_engine import live_enabled as le
        rest = data[len("pos_cconf_"):]
        scope, mode = rest.rsplit("_", 1)
        positions = profile.get("sol_positions") or []

        def _rec(p):
            if record_crypto_close:
                record_crypto_close(profile, p)

        if mode == "track":
            positions, results = close_tracker(positions, scope, record_close_fn=_rec)
            update_user_profile(user_id, {"sol_positions": positions})
            await safe_edit("SCOPE=%s TRACKER\n%s" % (scope, "\n".join(results[:40])))
            return True
        if mode == "live":
            if not le():
                await safe_edit("Live sell blocked: COPY_LIVE_ENABLED is false.")
                return True
            secret = profile.get("sol_burner_key")
            pub = profile.get("sol_wallet")
            if not secret or not pub:
                await safe_edit("Wallet not configured.")
                return True
            from crypto_copy import executor as ex
            pending = set()
            if context is not None:
                pending = context.user_data.setdefault("close_pending", set())

            def _sell(sec, pk, mint):
                return ex.sell_token_pct(sec, pk, mint, pct=100.0, slippage_bps=150)

            positions, results = close_live_each(
                positions, scope, secret, pub, _sell, record_close_fn=_rec, pending_ids=pending
            )
            update_user_profile(user_id, {"sol_positions": positions})
            await safe_edit("SCOPE=%s LIVE\n%s" % (scope, "\n".join(results[:40])))
            return True

    # individual close confirm
    if data.startswith("pos_idone_"):
        from crypto_copy.copy_engine import live_enabled as le
        from datetime import datetime
        rest = data[len("pos_idone_"):]
        pid, mode = rest.rsplit("_", 1)
        positions = profile.get("sol_positions") or []
        pos = next((p for p in positions if str(p.get("id")) == pid and p.get("status", "open") == "open"), None)
        if not pos:
            await safe_edit("Position not found.")
            return True
        pending = set()
        if context is not None:
            pending = context.user_data.setdefault("close_pending", set())
        if pid in pending:
            await safe_edit("%s: Pending" % pid)
            return True
        if mode == "track":
            if record_crypto_close:
                record_crypto_close(profile, pos)
            pos["status"] = "closed"
            pos["closed_at"] = datetime.utcnow().isoformat()
            update_user_profile(user_id, {"sol_positions": positions})
            await safe_edit("%s: Closed (tracker)" % pos.get("symbol"))
            return True
        if mode == "live":
            if not le():
                await safe_edit("Live sell blocked: COPY_LIVE_ENABLED is false.")
                return True
            secret, pub = profile.get("sol_burner_key"), profile.get("sol_wallet")
            mint = pos.get("mint") or pos.get("token") or ""
            if not secret or not pub or not mint:
                await safe_edit("Unable to Sell (wallet/mint)")
                return True
            pending.add(pid)
            from crypto_copy import executor as ex
            try:
                out = ex.sell_token_pct(secret, pub, mint, pct=100.0, slippage_bps=150)
            finally:
                pending.discard(pid)
            if out.get("ok"):
                if record_crypto_close:
                    record_crypto_close(profile, pos)
                pos["status"] = "closed"
                pos["closed_at"] = datetime.utcnow().isoformat()
                update_user_profile(user_id, {"sol_positions": positions})
                await safe_edit("%s: Closed sig=%s" % (pos.get("symbol"), (out.get("sig") or "")[:16]))
            else:
                await safe_edit("%s: Failed %s" % (pos.get("symbol"), out.get("error")))
            return True

    if data.startswith("pos_iclose_"):
        from crypto_copy.close_positions import normalize_origin
        pid = data.replace("pos_iclose_", "")
        pos = next((p for p in (profile.get("sol_positions") or []) if str(p.get("id")) == pid and p.get("status", "open") == "open"), None)
        if not pos:
            await safe_edit("Position not found.")
            return True
        qty = pos.get("qty") or pos.get("amount") or 0
        msg = (
            "CONFIRM CLOSE POSITION\n"
            "id: %s\nsymbol: %s\norigin: %s\nqty: %s\n"
            "Cancel = no sell."
        ) % (pid, pos.get("symbol"), normalize_origin(pos), qty)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("CONFIRM (tracker)", callback_data="pos_idone_%s_track" % pid)],
            [InlineKeyboardButton("CONFIRM + LIVE SELL", callback_data="pos_idone_%s_live" % pid)],
            [InlineKeyboardButton("Cancel", callback_data="pos_menu")],
        ])
        await safe_edit(msg, kb)
        return True

    return False
