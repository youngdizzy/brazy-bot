#!/usr/bin/env python3
"""
Brazy Bot — Full Version
Built exclusively for Axion.
Tradeify prop firm + TJR / ICT strategy + Vision + Voice + Journal + Alerts.
"""

import asyncio
import logging
import os
import json
import re
import base64
import hashlib
import io
from datetime import datetime, time, timedelta
from typing import Optional, Dict, Any, List
from pathlib import Path

from dotenv import load_dotenv
import pytz
load_dotenv(Path(__file__).resolve().parent / ".env", override=False)  # load this bot folder's .env

NY_TZ = pytz.timezone("America/New_York")

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
    TypeHandler,
    ApplicationHandlerStop,
)
from telegram.constants import ChatAction
from telegram.error import BadRequest
from telegram.ext import ExtBot

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

# Optional AI imports (graceful fallback if missing)
try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("brazy-bot")

# ---------------------------------------------------------------------------
# Config & Persistence
# ---------------------------------------------------------------------------
DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
USERS_DIR = DATA_DIR / "users"
USERS_DIR.mkdir(exist_ok=True)
USER_DATA_FILE = DATA_DIR / "user_profiles.json"
JOURNAL_DIR = DATA_DIR / "journal"
JOURNAL_DIR.mkdir(exist_ok=True)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
DATA_SECRET = os.getenv("DATA_SECRET", os.getenv("BRAZY_DATA_SECRET", "")).strip()

OWNER_IDS = set()
_raw = os.getenv("TELEGRAM_OWNER_IDS", "")
if _raw:
    for part in _raw.split(","):
        part = part.strip()
        if part.isdigit():
            OWNER_IDS.add(int(part))

# Private by default: only explicitly licensed users and configured owners can use the bot.
LICENSE_USER_IDS = set()
_lic = os.getenv("LICENSE_USER_IDS", "").strip()
LICENSE_LIST_CONFIGURED = bool(_lic)
if _lic:
    for part in _lic.split(","):
        part = part.strip()
        if part.isdigit():
            LICENSE_USER_IDS.add(int(part))
LICENSE_USER_IDS |= OWNER_IDS


def user_is_licensed(user_id: int) -> bool:
    """Fail closed: never expose a private trading/wallet bot to everyone by default."""
    uid = int(user_id)
    if LICENSE_LIST_CONFIGURED:
        return uid in LICENSE_USER_IDS or uid in OWNER_IDS
    return bool(OWNER_IDS) and uid in OWNER_IDS


async def authorization_gate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Global authorization guard for every message and callback, not just /start."""
    user = update.effective_user
    if user is None or user_is_licensed(user.id):
        return
    message = update.effective_message
    query = update.callback_query
    notice = "Access restricted. Ask the bot owner to authorize your Telegram ID: %s" % (user.id if user else "unknown")
    try:
        if query is not None:
            await query.answer("Access restricted", show_alert=True)
        elif message is not None:
            await message.reply_text(notice)
    finally:
        raise ApplicationHandlerStop


def user_data_path(user_id: int) -> Path:
    return USERS_DIR / f"{int(user_id)}.json"


def _fernet():
    """Return a stable Fernet cipher; never fall back to plaintext storage."""
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        return None

    # Once generated, the local key file takes precedence so adding/removing an
    # environment secret later cannot silently change the key for existing data.
    key_path = DATA_DIR / ".fernet_key"
    try:
        if key_path.exists():
            key = key_path.read_bytes().strip()
        elif DATA_SECRET:
            digest = hashlib.sha256(DATA_SECRET.encode("utf-8")).digest()
            return Fernet(base64.urlsafe_b64encode(digest))
        else:
            key = Fernet.generate_key()
            fd = os.open(str(key_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as key_file:
                key_file.write(key)
        try:
            os.chmod(key_path, 0o600)
        except OSError:
            pass
        return Fernet(key)
    except FileExistsError:
        try:
            return Fernet(key_path.read_bytes().strip())
        except Exception:
            return None
    except Exception as exc:
        log.error("Could not load/create wallet encryption key: %s", exc)
        return None


def encrypt_secret(plain: str) -> str:
    if not plain:
        return ""
    f = _fernet()
    if not f:
        raise RuntimeError("Wallet-key encryption is unavailable. Install cryptography; refusing to store a private key in plaintext.")
    return "enc:" + f.encrypt(plain.encode("utf-8")).decode("utf-8")


def decrypt_secret(blob: str) -> str:
    if not blob:
        return ""
    # Backward compatibility for data written by older versions. New writes never use plain:.
    if str(blob).startswith("plain:"):
        return blob[6:]
    if str(blob).startswith("enc:"):
        try:
            f = _fernet()
            if f:
                return f.decrypt(blob[4:].encode("utf-8")).decode("utf-8")
        except Exception:
            pass
        # One-time compatibility path for old ciphertext encrypted with the old hard-coded key.
        try:
            from cryptography.fernet import Fernet
            legacy_digest = hashlib.sha256(b"brazy-dev-secret-change-me").digest()
            legacy = Fernet(base64.urlsafe_b64encode(legacy_digest))
            return legacy.decrypt(blob[4:].encode("utf-8")).decode("utf-8")
        except Exception:
            log.error("Unable to decrypt a saved burner key. Preserve .fernet_key and DATA_SECRET; do not overwrite wallet data.")
            return ""
    return blob  # legacy plaintext data; migration can be done by saving it again.


async def safe_edit(query, text: str, reply_markup=None):
    """Edit Telegram messages safely without duplicate replies."""
    try:
        await query.edit_message_text(text[:4000], reply_markup=reply_markup)
    except Exception as e:
        if "message is not modified" in str(e).lower():
            return
        log.warning("edit_message_text failed: %s", e)
        try:
            await query.message.reply_text(text[:4000], reply_markup=reply_markup)
        except Exception as e2:
            log.warning("reply fallback failed: %s", e2)


async def safe_reply(update, text: str, reply_markup=None):
    try:
        await update.message.reply_text(text[:4000], reply_markup=reply_markup)
    except Exception as e:
        log.warning("reply_text failed: %s", e)

# AI clients
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_AVAILABLE and OPENAI_API_KEY else None
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=120.0) if ANTHROPIC_AVAILABLE and ANTHROPIC_API_KEY else None

# Telegram Markdown is fragile when AI output or user-controlled identifiers are inserted.
# Retry only entity-parsing failures as plain text so one malformed response cannot crash a command.
_ORIGINAL_SEND_MESSAGE = ExtBot.send_message
_ORIGINAL_EDIT_MESSAGE_TEXT = ExtBot.edit_message_text


def _fallback_plain_text(value):
    if not isinstance(value, str):
        return value
    # Keep content intact; only remove common formatting delimiters in fallback text.
    value = re.sub(r"\*\*(.*?)\*\*", r"\1", value, flags=re.S)
    value = re.sub(r"(?<!\w)\*(.*?)\*(?!\w)", r"\1", value, flags=re.S)
    value = re.sub(r"(?<!\w)_(.*?)_(?!\w)", r"\1", value, flags=re.S)
    value = value.replace("`", "")
    return value


async def _send_message_with_parse_fallback(self, *args, **kwargs):
    try:
        return await _ORIGINAL_SEND_MESSAGE(self, *args, **kwargs)
    except BadRequest as exc:
        if "parse entities" not in str(exc).lower() and "can't find end of the entity" not in str(exc).lower():
            raise
        retry = dict(kwargs)
        retry["parse_mode"] = None
        retry.pop("entities", None)
        if "text" in retry:
            retry["text"] = _fallback_plain_text(retry["text"])
        elif len(args) >= 2:
            args = (args[0], _fallback_plain_text(args[1]), *args[2:])
        log.warning("Telegram Markdown parse failed; retrying message as plain text")
        return await _ORIGINAL_SEND_MESSAGE(self, *args, **retry)


async def _edit_message_with_parse_fallback(self, *args, **kwargs):
    try:
        return await _ORIGINAL_EDIT_MESSAGE_TEXT(self, *args, **kwargs)
    except BadRequest as exc:
        if "parse entities" not in str(exc).lower() and "can't find end of the entity" not in str(exc).lower():
            raise
        retry = dict(kwargs)
        retry["parse_mode"] = None
        retry.pop("entities", None)
        if "text" in retry:
            retry["text"] = _fallback_plain_text(retry["text"])
        elif args:
            args = (_fallback_plain_text(args[0]), *args[1:])
        log.warning("Telegram Markdown parse failed; retrying edit as plain text")
        return await _ORIGINAL_EDIT_MESSAGE_TEXT(self, *args, **retry)


# Patch the PTB bot methods used by Message.reply_text, CallbackQuery edits, and scheduled sends.
ExtBot.send_message = _send_message_with_parse_fallback
ExtBot.edit_message_text = _edit_message_with_parse_fallback


# ---------------------------------------------------------------------------
# Tradeify Rules Database (accurate as of late 2026)
# ---------------------------------------------------------------------------
TRADEIFY_RULES = {
    "Growth": {
        25000: {"profit_target": 1500, "max_drawdown": 1000, "daily_loss_limit": 600, "consistency_eval": None, "consistency_funded": 0.35, "min_days": 1, "max_minis": 1, "max_micros": 10},
        50000: {"profit_target": 3000, "max_drawdown": 2000, "daily_loss_limit": 1250, "consistency_eval": None, "consistency_funded": 0.35, "min_days": 1, "max_minis": 4, "max_micros": 40},
        100000: {"profit_target": 6000, "max_drawdown": 3500, "daily_loss_limit": 2500, "consistency_eval": None, "consistency_funded": 0.35, "min_days": 1, "max_minis": 8, "max_micros": 80},
        150000: {"profit_target": 9000, "max_drawdown": 5000, "daily_loss_limit": 3750, "consistency_eval": None, "consistency_funded": 0.35, "min_days": 1, "max_minis": 12, "max_micros": 120},
    },
    "Select": {
        25000: {"profit_target": 1500, "max_drawdown": 1000, "daily_loss_limit": None, "consistency_eval": 0.40, "consistency_funded": None, "min_days": 3, "max_minis": 1, "max_micros": 10},
        50000: {"profit_target": 3000, "max_drawdown": 2000, "daily_loss_limit": None, "consistency_eval": 0.40, "consistency_funded": None, "min_days": 3, "max_minis": 4, "max_micros": 40},
        100000: {"profit_target": 6000, "max_drawdown": 3000, "daily_loss_limit": None, "consistency_eval": 0.40, "consistency_funded": None, "min_days": 3, "max_minis": 8, "max_micros": 80},
        150000: {"profit_target": 9000, "max_drawdown": 4500, "daily_loss_limit": None, "consistency_eval": 0.40, "consistency_funded": None, "min_days": 3, "max_minis": 12, "max_micros": 120},
    },
    "Lightning": {
        25000: {"profit_target": None, "max_drawdown": 1000, "daily_loss_limit": None, "consistency_eval": None, "consistency_funded": 0.20, "min_days": 0, "max_minis": 1, "max_micros": 10},
        50000: {"profit_target": None, "max_drawdown": 2000, "daily_loss_limit": 1250, "consistency_eval": None, "consistency_funded": 0.20, "min_days": 0, "max_minis": 4, "max_micros": 40},
        100000: {"profit_target": None, "max_drawdown": 4000, "daily_loss_limit": 2500, "consistency_eval": None, "consistency_funded": 0.20, "min_days": 0, "max_minis": 8, "max_micros": 80},
        150000: {"profit_target": None, "max_drawdown": 5250, "daily_loss_limit": 3750, "consistency_eval": None, "consistency_funded": 0.20, "min_days": 0, "max_minis": 12, "max_micros": 120},
    },
}

# ---------------------------------------------------------------------------
# TJR / ICT System Prompt (Hypothesis only) — Improved
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are Brazy Bot — Axion's elite private trading desk — think and speak like a disciplined hedge fund manager, not a retail hype account.
You specialize in TJR Boot Camp + ICT / Smart Money Concepts, and also master orderflow, volume profile, classic price action, candlestick patterns, and hybrid strategies.
This applies to ALL markets you analyze: futures, forex, stocks, AND all crypto (BTC, ETH, majors, alts) — same institutional standard, adapted to each asset's microstructure.

CORE IDENTITY:
- You exist only for Axion.
- Think like a hedge fund manager: capital preservation first, asymmetric R, process over opinions, no FOMO, no ego entries.
- Strict about risk and Tradeify rules when he is on a prop account.
- Never predict the future — only clear hypotheses with invalidation.
- Teach patiently, no fluff.
- Crypto is not a casino here: 24/7 liquidity, volatility, and funding exist, but structure, liquidity, and risk rules still govern every decision.

==================================================
ADAPTIVE ASSET PROFILES (detect from chart or caption)
==================================================
Instantly adapt logic based on what the chart is:

**Futures (ES, NQ, YM, RTY, CL, GC, etc.):**
- Session-based (RTH vs overnight)
- VWAP, overnight high/low, opening range matter
- Flatten rules if Tradeify
- Liquidity often at prior day/session extremes

**Crypto (ALL crypto — BTC, ETH, SOL, majors, alts):**
- 24/7 liquidity, no traditional session close
- Higher volatility, wicks are normal
- No daily VWAP "reset" in the same way — use rolling or session VWAP if shown
- Funding rates / weekend behavior can matter
- Still use structure, FVG, OB, liquidity concepts

**Forex (EURUSD, GBPUSD, etc.):**
- Session kills (London, NY, overlap)
- Spreads and rollover matter
- Cleaner technicals often, respect Asian range

**Stocks / Equity Index CFDs:**
- Regular trading hours, gap risk at open
- News/earnings sensitivity
- Volume profile and VWAP very relevant

If asset class is unclear from the image or caption → ASK: "What market is this (ES/NQ, BTC, EURUSD, stock ticker)?"

==================================================
TIMEFRAME SYNTHESIS
==================================================
- When LIVE MULTI-TIMEFRAME CANDLES are provided in the user message, use them to see structure not visible on the screenshot (1m, 15m, 1h, 4h, daily as available).
- If only one chart is provided and timeframe is not labeled → ASK or state your best guess and ask for confirmation.
- If multiple timeframes are provided (text + images, live data, or several screenshots):
  * Identify higher-timeframe bias (4H / Daily / Weekly)
  * Identify lower-timeframe trigger (1m / 5m / 15m)
  * Explicitly align them: "HTF bias is bullish on 4H. LTF 15m shows liquidity sweep + BOS for long entry."
- Always state which timeframe each observation comes from.
- Prefer: HTF for bias & major POIs, LTF for precise entry, stop, and timing.

==================================================
ADVANCED MULTI-MODAL VISUAL ANALYSIS
==================================================
When you receive a chart screenshot you MUST actively look for:

1. Micro-Pattern Recognition
   - Candlestick: Engulfing, Morning/Evening Star, Pin bar, Doji, Three soldiers/crows, Inside bar
   - Chart patterns: Head & Shoulders, Inverse H&S, Flags, Pennants, Double/Triple tops-bottoms, Wedges, Triangles, Cup & Handle
   - State them only when clearly visible. Do not force patterns.

2. Institutional Liquidity & Supply/Demand
   - Equal highs / equal lows (liquidity pools)
   - Order Blocks / Breaker Blocks
   - Fair Value Gaps (FVG) / Imbalances
   - Strong support/resistance / consolidation zones
   - Volume clusters or obvious acceptance/rejection areas if visible

3. Multi-Indicator Confluence (if indicators are visible on the chart)
   - RSI (overbought/oversold, divergence)
   - MACD (cross, histogram, divergence)
   - Moving averages / VWAP / EMAs
   - Volume
   - Any other visible oscillators
   Explicitly connect them: e.g. "Price is rejecting a major Order Block while RSI shows bearish divergence and MACD histogram is rolling over."

4. Structure
   - Higher highs / higher lows or lower highs / lower lows
   - Break of Structure (BOS) / Change of Character (CHoCH)
   - Premium vs Discount relative to dealing range equilibrium (50%)

==================================================
OUTPUT STRUCTURE (always use this for chart analysis)
==================================================
**Asset / Timeframe:** (screenshot TF + any live MTF used)
**Daily Bias:** Bullish / Bearish / Neutral

**MULTI-TIMEFRAME ANALYSIS** (required every chart)
- **HTF (4H / Daily / 1H):** bias, structure (HH/HL or LH/LL), major liquidity, key OB/FVG
- **MTF (15m / 5m):** intermediate structure, where price sits vs equilibrium
- **LTF (1m / screenshot TF):** entry trigger, sweep, BOS, precise zone
- **Alignment:** HTF + LTF agree? If not → WAIT or reduce quality

**Confluence Score:** X/6 (liquidity swept, BOS/MSS, FVG or OB, discount/premium, session/killzone, HTF align)
**Setup Quality:** A+ / A / B / Skip
  - A+ only if confluence >= 5 AND HTF/LTF aligned AND rules clear AND clear invalidation
  - A if confluence 3-4 with clean structure
  - else B or Skip — do not inflate quality
**Why this grade:** one tight sentence

**TRADE SETUP** (clear plan — always fill; use WAIT fields if no trade)
**Recommendation:** BUY / SELL / WAIT
**Entry Zone:**
**Stop Loss:**
**Take Profit:** TP1 / TP2 at real liquidity, opposing OB, or session levels — not a fake number
**R:R:** default aim **2:1 or better** when structure allows. If the chart only offers clean ~1.5R, take that — never invent 2R by stretching TP into noise.
**Payout-first targeting (Tradeify max payout wins over vanity R:R):**
  - Ideal: 2R+ into real liquidity while HTF still supports
  - Eval: progress toward profit target; protect DD; steady wins beat one lottery trade
  - Funded: partials at TP1 (~1R–1.5R or first liquidity), runner toward 2R+ / next liquidity if structure holds
  - Consistency: do not need one oversized day — balanced days keep you payout-eligible
  - Near target or tight on DD: bank / tighten — surviving to max payout > forcing 2R
  - Best plan = whatever the chart + Tradeify rules support to get paid and stay alive
**Entry precision pack:** (required when BUY or SELL)
  - Trigger: exact condition to enter
  - Invalidation candle: what bar/close kills the idea
  - Partial plan: scale at first liquidity / ~1R–1.5R; runner to 2R+ only if HTF still valid
  - Skip-if: what in the next 1-3 minutes means abort
**Position Size Note:** from stop distance + risk % + Tradeify max contracts

**How to approach it (step-by-step):**
**Timing advice:**
**Visible Patterns & Confluence:**
  - Structure / Liquidity / FVG / OB
  - Candlestick or chart patterns
  - Indicators (if any) and what they add
**Strategy this belongs to:**
**Extra knowledge you should know:**
**Hypothesis:** (must start with Hypothesis)
**Invalidation:**
**Payout path check:** Does this help eval target / funded payout without risking DD or consistency? Yes/No + why
**Rule pre-check:** Honor TRADEIFY RULE PRE-CHECK. Any BLOCKER → WAIT.
**Risk Guardian Check:**
**Your current settings:** Daily Bias + Strict Mode

TJR Checklist (run every time):
1. Clear bias on the relevant timeframe?
2. Liquidity swept?
3. BOS / MSS?
4. FVG or Order Block in bias direction?
5. Discount (buys) / Premium (sells)?
6. Confluence count (structure + pattern + indicator)?

Only recommend BUY/SELL on A or A+ when Strict Mode is ON. Weak setups = WAIT.
If timeframe, asset, or picture is unclear → ask a short clarifying question instead of guessing wildly.

==================================================
PRIMARY MISSION — MAX PAYOUT
==================================================
Axion built this bot to help him pass evaluations and reach maximum Tradeify payout without blowing the account.
That is the whole point.

DECISION HIERARCHY (never reverse this order):
1. Tradeify / account rules (DD, daily loss, contracts, flatten, consistency)
2. Capital preservation
3. Setup quality (A+ / A only when Strict ON)
4. Clear hypothesis + invalidation
5. Everything else

Therefore you MUST:
- Reject bad opportunities. If setup is not A or A+ (when Strict is ON), say WAIT. No maybe garbage.
- Never push FOMO, revenge, or "just one more trade."
- Enforce Tradeify rules hard: daily loss, trailing DD, max contracts, flatten by 4:59 PM ET, consistency when it applies.
- Prefer fewer high-quality trades that can scale toward payout over many small noisy ones.
- Protect capital first. Surviving to payout beats ego entries.
- When you say BUY/SELL: clear bias, liquidity, BOS, FVG/OB, discount/premium, confluence. Prefer ~2R+ when structure allows; honest R:R always. For Sol/crypto memecoins: minimum TP 1.5:1 — if invalidation cannot support >=1.5R to a real target, Recommendation = WAIT. Targets serve Tradeify max payout (rules + real liquidity), not a fixed ratio vanity metric.
- If RULE PRE-CHECK has blockers → WAIT. No exceptions.
- Use his recent debriefs/playbook notes when provided — respect "delete" setups, favor "repeat" patterns.

Every WAIT that saves a stupid loss protects the path to max payout.
Every A+ plan that hits moves him closer to max payout.
Targets: default aim 2:1+ at real liquidity when the chart allows; never stretch TP into noise. Partials + runner fine. Tradeify max payout and DD/consistency always outrank a forced ratio.
Targets: use real chart liquidity and Tradeify constraints (DD, daily loss, consistency). Aim for strong R when structure allows (~2R+), but honesty over forced 2:1. Partials and protecting the account beat greedy TPs that reverse into a rule breach.

Tone: Direct, calm, sharp, no fluff, completely loyal to Axion.
"""

# ---------------------------------------------------------------------------
# User Profile Management
# ---------------------------------------------------------------------------

def load_profiles() -> Dict[str, Any]:
    """Legacy aggregator — prefer per-user files via get_user_profile."""
    data = {}
    if USER_DATA_FILE.exists():
        try:
            with open(USER_DATA_FILE, "r") as f:
                data = json.load(f)
        except Exception:
            data = {}
    # Also index per-user files
    for p in USERS_DIR.glob("*.json"):
        try:
            with open(p, "r") as f:
                data[p.stem] = json.load(f)
        except Exception:
            pass
    return data


def save_profiles(data: Dict[str, Any]):
    """Write each user to isolated file (no cross-user blob required)."""
    for uid, profile in (data or {}).items():
        try:
            path_u = user_data_path(int(uid))
            # encrypt burner key at rest
            out = dict(profile)
            raw_key = out.get("sol_burner_key")
            if raw_key and not str(raw_key).startswith("enc:") and not str(raw_key).startswith("plain:"):
                out["sol_burner_key"] = encrypt_secret(raw_key)
            with open(path_u, "w") as f:
                json.dump(out, f, indent=2)
        except Exception as e:
            log.warning("save user %s failed: %s", uid, e)
    # keep legacy file minimal (ids only) — optional empty
    try:
        with open(USER_DATA_FILE, "w") as f:
            json.dump({k: {"_isolated": True} for k in (data or {})}, f, indent=2)
    except Exception:
        pass


def _default_profile() -> Dict[str, Any]:
    return {
        "timezone": None,
        "accounts": {},
        "active_account": None,
        "risk_percent": 0.5,
        "strict_mode": True,
        "daily_bias": None,
        "daily_profit_target": None,
        "daily_max_loss": None,
        "asset_profile": "auto",
        "open_positions": [],
        "journal": [],
        "performance_logs": [],
        "alerts": [],
        "ai_provider": "openai",
        "awaiting_performance": None,
        "account_phase": "eval",
        "account_balance_profit": 0,
        "best_day_profit": 0,
        "debriefs": [],
        "awaiting_debrief": None,
        "trading_mode": "paper",
        "paper_journal": [],
        "custom_rules": {
            "max_daily_loss": None,
            "max_risk_percent": None,
            "flatten_et": None,
            "notes": "",
        },
        # Separate TJR-style rules for LIVE own-money stocks/futures. Never used for Sol memes or funded accounts.
        "live_stock_rules": {
            "risk_percent": 0.5,
            "max_daily_loss_pct": 2.0,
            "max_trades_day": 3,
            "min_rr": 2.0,
            "session": "NY open 9:30-11:30 ET",
            "a_plus_only": True,
            "require_htf_bias": True,
            "require_liquidity_sweep": True,
            "require_displacement_bos": True,
            "prefer_fvg_entry": True,
            "stop_beyond_swing": True,
            "no_revenge_trading": True,
        },
        "risk_preset": "medium",
        "custom_sol_rules": {
            "max_risk_pct": 0.75,
            "max_daily_loss_pct": 3.0,
            "max_positions": 4,
            "min_liq_usd": 35000,
            "min_grade": "A",
            "min_rr": 1.5,
            "copy_size_pct_of_their_buy": 25,
            "min_pair_age_min": 15,
        },
        "copy_traders": [],
        "sol_wallet": None,
        "sol_burner_key": None,
        "awaiting_deposit": None,
        "deposit_address": None,
        "no_rug_mode": True,
        "anti_mev": True,
        "x_discovery_enabled": False,
        "pump_cashback_code": None,
        "crypto_chain": "solana",
        "sol_positions": [],
        "crypto_closed": [],
        "crypto_alerts": {
            "wins": True,
            "losses": True,
            "pnl_moves": True,
            "pnl_threshold_pct": 20,
        },
        "last_desk": "home",
    }


def get_user_profile(user_id: int) -> Dict[str, Any]:
    """Isolated profile: data/users/<telegram_id>.json only."""
    path_u = user_data_path(user_id)
    if path_u.exists():
        try:
            with open(path_u, "r") as f:
                profile = json.load(f)
        except Exception:
            profile = _default_profile()
    else:
        # migrate from legacy blob once
        profile = None
        if USER_DATA_FILE.exists():
            try:
                with open(USER_DATA_FILE, "r") as f:
                    allp = json.load(f)
                legacy = allp.get(str(user_id))
                if legacy and not legacy.get("_isolated"):
                    profile = legacy
            except Exception:
                pass
        if profile is None:
            profile = _default_profile()
        with open(path_u, "w") as f:
            json.dump(profile, f, indent=2)
        try:
            os.chmod(path_u, 0o600)
        except OSError:
            pass

    # decrypt burner key into runtime memory only
    if profile.get("sol_burner_key"):
        profile["sol_burner_key"] = decrypt_secret(profile["sol_burner_key"])
    return profile


def update_user_profile(user_id: int, updates: Dict[str, Any]):
    profile = get_user_profile(user_id)
    profile.update(updates)
    out = dict(profile)
    # always store encrypted
    if out.get("sol_burner_key"):
        raw = out["sol_burner_key"]
        if not str(raw).startswith("enc:") and not str(raw).startswith("plain:"):
            out["sol_burner_key"] = encrypt_secret(raw)
    path_u = user_data_path(user_id)
    with open(path_u, "w") as f:
        json.dump(out, f, indent=2)
    try:
        os.chmod(path_u, 0o600)
    except OSError:
        pass


def add_journal_entry(user_id: int, entry: Dict[str, Any]):
    profile = get_user_profile(user_id)
    profile["journal"].append(entry)
    update_user_profile(user_id, {"journal": profile["journal"]})
    # Also save to file
    journal_file = JOURNAL_DIR / f"{user_id}_{datetime.utcnow().strftime('%Y%m')}.json"
    entries = []
    if journal_file.exists():
        with open(journal_file, "r") as f:
            entries = json.load(f)
    entries.append(entry)
    with open(journal_file, "w") as f:
        json.dump(entries, f, indent=2)


# ---------------------------------------------------------------------------
# Tradeify rule breach pre-check + post-trade debrief (max payout strength)
# ---------------------------------------------------------------------------
def rule_breach_precheck(profile: Dict) -> Dict[str, Any]:
    """Hard gate before BUY/SELL.

    Modes:
    - paper: practice — full analysis, no hard blocks
    - live: own money — custom_rules only (no Tradeify)
    - funded: Tradeify prop rules + max payout path
    """
    blockers = []
    notes = []
    active = profile.get("active_account")
    phase = profile.get("account_phase", "eval")
    mode = (profile.get("trading_mode") or "paper").lower()
    if mode not in ("paper", "live", "funded"):
        mode = "paper"
    now = datetime.now(NY_TZ)
    t = now.hour + now.minute / 60.0
    custom = profile.get("custom_rules") or {}

    # ----- PAPER -----
    if mode == "paper":
        notes.append("PAPER MODE — practice. Full analysis. No hard account blocks.")
        if now.weekday() < 5 and t >= 16.75:
            notes.append("PAPER note: past Tradeify flatten window (4:59 PM ET) — funded would block")
        if profile.get("strict_mode", True):
            notes.append("Strict ON — only A / A+ recommended")
        if not profile.get("daily_bias"):
            notes.append("Daily bias not set — /bias bullish|bearish|neutral")
        return {
            "ok": True,
            "blockers": [],
            "notes": notes,
            "phase": phase,
            "active": active,
            "paper": True,
            "mode": "paper",
        }

    # ----- LIVE (own money) -----
    if mode == "live":
        notes.append("LIVE MODE — own capital. No Tradeify. Stock/futures rules are separate from Sol meme rules.")
        stock_rules = profile.get("live_stock_rules") or {}
        if stock_rules:
            notes.append(
                "TJR stock plan: risk " + str(stock_rules.get("risk_percent", 0.5)) + "%/trade; "
                + str(stock_rules.get("max_daily_loss_pct", 2.0)) + "% max daily loss; "
                + str(stock_rules.get("max_trades_day", 3)) + " trades/day; min "
                + str(stock_rules.get("min_rr", 2.0)) + ":1 R:R; "
                + str(stock_rules.get("session", "NY open 9:30-11:30 ET")) + "."
            )
            notes.append("TJR checklist: higher-timeframe bias, liquidity sweep, displacement/BOS, FVG entry when present, stop beyond invalidation, no revenge trades; A+ only.")
        flatten = custom.get("flatten_et")
        if flatten and now.weekday() < 5:
            try:
                fh, fm = str(flatten).split(":")
                ft = int(fh) + int(fm) / 60.0
                if t >= ft:
                    blockers.append(f"Custom rule: past flatten ({flatten} ET) — no new risk")
                elif t >= ft - 0.25:
                    notes.append(f"Near your custom flatten ({flatten} ET)")
            except Exception:
                notes.append(f"Custom flatten unreadable: {flatten}")
        if custom.get("max_daily_loss"):
            notes.append(f"Custom max daily loss: ${custom['max_daily_loss']}")
        if custom.get("max_risk_percent"):
            notes.append(f"Custom max risk/trade: {custom['max_risk_percent']}%")
        if custom.get("notes"):
            notes.append(f"Your rules: {custom['notes']}")
        if profile.get("strict_mode", True):
            notes.append("Strict ON — only A / A+ allowed")
        if not profile.get("daily_bias"):
            notes.append("Daily bias not set — set with /bias")
        if now.weekday() >= 5:
            notes.append("Weekend — futures thin; crypto only if planned")
        return {
            "ok": len(blockers) == 0,
            "blockers": blockers,
            "notes": notes,
            "phase": phase,
            "active": active,
            "paper": False,
            "mode": "live",
        }

    # ----- FUNDED (Tradeify) -----
    notes.append("FUNDED MODE — Tradeify rules enforced. Max payout path.")
    if now.weekday() < 5 and t >= 16.75:
        blockers.append("Past / near flatten window (be flat by 4:59 PM ET) — no new risk")
    elif now.weekday() < 5 and t >= 16.0:
        notes.append("Close to flatten — only manage, avoid fresh entries")

    if not active or active not in profile.get("accounts", {}):
        notes.append("No active Tradeify account — /start to set size + plan")
        if profile.get("strict_mode", True):
            notes.append("Strict ON — only A / A+ allowed")
        return {
            "ok": len(blockers) == 0,
            "blockers": blockers,
            "notes": notes,
            "phase": phase,
            "active": active,
            "paper": False,
            "mode": "funded",
        }

    acc = profile["accounts"][active]
    rules = acc.get("rules", {})
    max_dd = rules.get("max_drawdown")
    daily_loss = rules.get("daily_loss_limit")
    max_minis = rules.get("max_minis")
    profit_target = rules.get("profit_target")
    consistency = rules.get("consistency_eval") if phase == "eval" else rules.get("consistency_funded")
    profit = profile.get("account_balance_profit") or 0
    best_day = profile.get("best_day_profit") or 0

    if max_dd:
        notes.append(f"Trailing DD wall: ${max_dd}")
    if daily_loss:
        notes.append(f"Daily loss limit: ${daily_loss}")
    if max_minis is not None:
        notes.append(f"Max contracts: {max_minis} minis / {rules.get('max_micros', '?')} micros")
    if profit_target:
        left = profit_target - profit
        notes.append(f"Profit target ${profit_target} | progress ${profit} | left ${left}")
        if left <= 0 and phase == "eval":
            notes.append("Eval target reached — protect, do not gamble it away")
    if consistency and profit > 0 and best_day > 0:
        frac = best_day / max(profit, 1)
        if frac > (consistency or 0.35):
            blockers.append(
                f"Consistency risk: best day ${best_day} vs progress ${profit} "
                f"(~{int((consistency or 0.35)*100)}% rule)"
            )
        else:
            notes.append(f"Consistency ~{int(consistency*100)}% single-day cap")

    if profile.get("strict_mode", True):
        notes.append("Strict ON — only A / A+ allowed")
    if not profile.get("daily_bias"):
        notes.append("Daily bias not set — set with /bias")
    if now.weekday() >= 5:
        notes.append("Weekend — futures thin; crypto only if planned")

    return {
        "ok": len(blockers) == 0,
        "blockers": blockers,
        "notes": notes,
        "phase": phase,
        "active": active,
        "paper": False,
        "mode": "funded",
    }


def format_precheck_for_prompt(pre: Dict[str, Any]) -> str:
    lines = [f"Mode: {pre.get('mode')} | Phase: {pre.get('phase')} | Active: {pre.get('active')}"]
    if pre.get("blockers"):
        lines.append("BLOCKERS (must WAIT if any): " + " | ".join(pre["blockers"]))
    if pre.get("notes"):
        lines.append("Notes: " + " | ".join(pre["notes"]))
    lines.append("If blockers exist → Recommendation MUST be WAIT.")
    return "\n".join(lines)


async def start_debrief(update: Update, user_id: int, seed: Dict[str, Any]):
    """Begin post-trade debrief after a log"""
    update_user_profile(user_id, {
        "awaiting_debrief": {
            "step": 1,
            "data": seed,
        }
    })
    await update.message.reply_text(
        "**Post-trade debrief — 1/3**\n\n"
        "Did this trade advance the **max payout path** (eval target / funded growth) or was it just activity?\n"
        "Reply: `advanced` / `neutral` / `hurt`",
        parse_mode="Markdown"
    )


async def handle_debrief_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, profile: Dict) -> bool:
    state = profile.get("awaiting_debrief")
    if not state:
        return False
    user_id = update.effective_user.id
    text = update.message.text.strip()
    step = state.get("step", 1)
    data = state.get("data", {})

    if step == 1:
        data["payout_path"] = text
        state["step"] = 2
        state["data"] = data
        update_user_profile(user_id, {"awaiting_debrief": state})
        await update.message.reply_text(
            "**Debrief 2/3**\n"
            "Any rule stress? (DD, daily loss, size, consistency, flatten)\n"
            "Reply: `none` or short note"
        )
        return True
    if step == 2:
        data["rule_stress"] = text
        state["step"] = 3
        state["data"] = data
        update_user_profile(user_id, {"awaiting_debrief": state})
        await update.message.reply_text(
            "**Debrief 3/3**\n"
            "Playbook: `repeat` (keep this setup) / `refine` (adjust) / `delete` (never again)\n"
            "Optional one-line lesson after the word."
        )
        return True
    if step == 3:
        data["playbook"] = text
        data["date"] = datetime.now(NY_TZ).strftime("%Y-%m-%d %H:%M ET")
        debriefs = profile.get("debriefs", [])
        debriefs.append(data)
        # keep last 100
        debriefs = debriefs[-100:]
        update_user_profile(user_id, {"awaiting_debrief": None, "debriefs": debriefs})

        # Coach summary
        path = (data.get("payout_path") or "").lower()
        if "hurt" in path:
            coach = "That one worked against the payout path. Next time: smaller or WAIT."
        elif "advanced" in path:
            coach = "Good — that is the only kind of trade that should get size."
        else:
            coach = "Neutral trades are fine rarely; do not let them become the habit."

        await update.message.reply_text(
            f"**Debrief saved**\n"
            f"Path: {data.get('payout_path')}\n"
            f"Rules: {data.get('rule_stress')}\n"
            f"Playbook: {data.get('playbook')}\n\n"
            f"{coach}\n"
            f"View recent: /debriefs",
            parse_mode="Markdown"
        )
        return True
    return False


async def debriefs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profile = get_user_profile(update.effective_user.id)
    items = profile.get("debriefs", [])[-5:]
    if not items:
        await update.message.reply_text("No debriefs yet. They start after `/log` or `/performance`.")
        return
    lines = ["**Last debriefs**\n"]
    for d in reversed(items):
        lines.append(
            f"• {d.get('date', '')} | {d.get('result', d.get('setup', ''))} | "
            f"path={d.get('payout_path', '')} | {d.get('playbook', '')}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")



# ---------------------------------------------------------------------------
# Multi-timeframe live candles (see beyond the screenshot)
# ---------------------------------------------------------------------------
def _extract_symbol_hint(caption: str, profile: Dict) -> Optional[str]:
    """Best-effort symbol from caption or asset profile."""
    import re
    text = (caption or "").upper()
    # Common tickers / futures / crypto
    patterns = [
        r"\b(ES|NQ|YM|RTY|CL|GC|SI|NG)\b",
        r"\b(BTC|ETH|SOL|BNB|XRP|ADA|DOGE|LINK|AVAX|DOT|MATIC|ARB|OP)\b",
        r"\b([A-Z]{1,5})\b",  # stock-like
    ]
    # Prefer explicit mentions
    for pat in patterns[:2]:
        m = re.search(pat, text)
        if m:
            return m.group(1)
    # Caption like "symbol: AAPL" or "ticker TSLA"
    m = re.search(r"(?:SYMBOL|TICKER|PAIR)[:\s]+([A-Z0-9/\-]{1,12})", text)
    if m:
        return m.group(1).replace("USDT", "").replace("USD", "")
    # Single clear token in caption
    tokens = re.findall(r"\b[A-Z]{2,5}\b", text)
    skip = {"BUY", "SELL", "WAIT", "LONG", "SHORT", "HTF", "LTF", "FVG", "BOS", "OB", "THE", "AND", "FOR", "RTH", "ETH"}  # ETH kept via crypto list
    for t in tokens:
        if t not in skip and t not in ("USD", "USDT"):
            return t
    return None


def fetch_mtf_candles(symbol: str, asset_profile: str = "auto") -> str:
    """Fetch recent candles on multiple timeframes. Returns text summary for the AI."""
    symbol = (symbol or "").strip().upper()
    if not symbol:
        return "No symbol detected — send caption with ticker (e.g. ES, NQ, BTC, AAPL) to load live MTF candles."

    lines = [f"LIVE MULTI-TIMEFRAME DATA for {symbol}:"]
    got_any = False

    # --- Crypto via Binance public API ---
    crypto_set = {"BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "LINK", "AVAX", "DOT", "MATIC", "ARB", "OP", "PEPE", "WIF", "SUI", "TIA"}
    is_crypto = asset_profile == "crypto" or symbol in crypto_set or symbol.endswith("USDT")

    if is_crypto:
        base = symbol.replace("USDT", "").replace("USD", "")
        pair = f"{base}USDT"
        intervals = [("1m", "1m"), ("15m", "15m"), ("1h", "1h"), ("4h", "4h")]
        try:
            import requests as req
            for label, interval in intervals:
                r = req.get(
                    "https://api.binance.com/api/v3/klines",
                    params={"symbol": pair, "interval": interval, "limit": 30},
                    timeout=8,
                )
                if not r.ok:
                    continue
                data = r.json()
                if not data:
                    continue
                got_any = True
                # last 8 candles summary
                candles = data[-8:]
                parts = []
                for c in candles:
                    o, h, l, cl = float(c[1]), float(c[2]), float(c[3]), float(c[4])
                    parts.append(f"O{o:.4g}/H{h:.4g}/L{l:.4g}/C{cl:.4g}")
                last = data[-1]
                lines.append(
                    f"  {label} last~30 bars; recent 8: " + " | ".join(parts[-4:])
                )
                lines.append(
                    f"  {label} now: O={float(last[1]):.6g} H={float(last[2]):.6g} L={float(last[3]):.6g} C={float(last[4]):.6g}"
                )
        except Exception as e:
            lines.append(f"  Crypto fetch error: {e}")

    # --- Stocks / ETFs / some futures proxies via yfinance ---
    if not is_crypto or not got_any:
        try:
            import yfinance as yf
            # Map common futures to Yahoo continuous-ish symbols
            ymap = {
                "ES": "ES=F", "NQ": "NQ=F", "YM": "YM=F", "RTY": "RTY=F",
                "CL": "CL=F", "GC": "GC=F", "SI": "SI=F", "NG": "NG=F",
                "BTC": "BTC-USD", "ETH": "ETH-USD",
            }
            ysym = ymap.get(symbol, symbol)
            ticker = yf.Ticker(ysym)
            frames = [
                ("1m", "1m", "1d"),   # 1m needs short period
                ("15m", "15m", "5d"),
                ("1h", "60m", "10d"),
                ("4h", "1h", "30d"),  # approximate 4h from 1h aggregate feel
                ("1d", "1d", "3mo"),
            ]
            for label, interval, period in frames:
                try:
                    df = ticker.history(period=period, interval=interval)
                    if df is None or len(df) < 5:
                        continue
                    got_any = True
                    tail = df.tail(6)
                    recent = []
                    for idx, row in tail.iterrows():
                        recent.append(
                            f"O{row['Open']:.4g}/H{row['High']:.4g}/L{row['Low']:.4g}/C{row['Close']:.4g}"
                        )
                    last = df.iloc[-1]
                    lines.append(f"  {label} recent: " + " | ".join(recent[-4:]))
                    lines.append(
                        f"  {label} last bar: O={last['Open']:.6g} H={last['High']:.6g} L={last['Low']:.6g} C={last['Close']:.6g}"
                    )
                except Exception:
                    continue
        except Exception as e:
            lines.append(f"  yfinance error: {e}")

    if not got_any:
        lines.append("  Could not load live candles. Caption the ticker clearly (e.g. 'NQ 5m' or 'BTC').")
    else:
        lines.append(
            "Use this MTF data to state HTF bias and LTF trigger even if the screenshot is only one timeframe. "
            "Do not invent candles — only use what is listed."
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# AI Vision Analysis
# ---------------------------------------------------------------------------
async def analyze_chart_with_ai(image_bytes: bytes, profile: Dict, caption: str = "") -> str:
    """Send chart to vision model and get structured TJR analysis"""
    active = profile.get("active_account") or "No account set"
    bias = profile.get("daily_bias") or "Not set"
    strict = profile.get("strict_mode", True)
    risk = profile.get("risk_percent", 0.5)
    asset_profile = profile.get("asset_profile", "auto")
    account_phase = profile.get("account_phase", "eval")
    account_profit = profile.get("account_balance_profit", 0)
    trading_mode = profile.get("trading_mode", "live")
    pre = rule_breach_precheck(profile)
    pre_text = format_precheck_for_prompt(pre)
    symbol_hint = _extract_symbol_hint(caption, profile)
    mtf_text = fetch_mtf_candles(symbol_hint, asset_profile) if symbol_hint else (
        "No symbol in caption — AI should ask for ticker to load 1m/15m/1h/4h live candles, "
        "or analyze screenshot only."
    )
    # Personal playbook from recent debriefs
    recent_debriefs = profile.get("debriefs", [])[-5:]
    debrief_lines = []
    for d in recent_debriefs:
        debrief_lines.append(
            f"{d.get('setup', '?')}: path={d.get('payout_path', '?')}, playbook={d.get('playbook', '?')}"
        )
    debrief_text = "; ".join(debrief_lines) if debrief_lines else "None yet"

    user_prompt = f"""
Analyze this chart screenshot with full multi-modal vision and TJR/ICT + price action skill.

Context:
- Active Tradeify account: {active}
- Current Daily Bias set by trader: {bias}
- Strict mode: {"ON" if strict else "OFF"}
- Risk per trade: {risk}%

Trader caption (if any): {caption}
- Forced asset profile: {asset_profile} (if not auto, lock logic to this class)
- Account phase: {account_phase} | Current profit progress: ${account_profit} | Mode: {trading_mode}
- Mission: max Tradeify payout path — reject anything that endangers DD or is low quality

TRADEIFY RULE PRE-CHECK (hard gate):
{pre_text}

HIS RECENT DEBRIEFS / PLAYBOOK:
{debrief_text}
(Respect delete/refine/repeat — do not recommend setups he marked delete.)

LIVE MULTI-TIMEFRAME CANDLES (beyond the screenshot):
{mtf_text}
Synthesize HTF bias from this data + what you see on the image. If only one TF is on the image, still state higher/lower TF context from live data when available.

IMPORTANT:
1. Detect or ask for ASSET CLASS (Futures ES/NQ, Crypto, Forex, Stock). Adapt logic accordingly.
2. Always run MULTI-TIMEFRAME ANALYSIS: HTF bias/structure, MTF context, LTF entry — use LIVE candle data when provided plus the screenshot.
3. Deliver a clear TRADE SETUP block: Recommendation, Entry, SL, TP at real liquidity, honest R:R (prefer 2R+ when chart allows), entry precision pack. Payout path and Tradeify rules outrank forced 2:1.
4. Actively look for: candlestick patterns, chart patterns, Order Blocks, FVGs, liquidity, indicators on the image.
5. Confluence Score X/6 — A+ only when high score + HTF/LTF aligned + rules clear.
6. If BUY or SELL: Entry precision pack required. TPs at real liquidity; prefer ~2R+ when structure allows but never fake R:R — serve Tradeify max payout path.
7. Follow the exact output structure. If symbol missing for live MTF, still analyze the image and ask for ticker for deeper MTF.
8. If asset or timeframe is unclear, state best guess and ask to confirm.
"""

    # Prefer Anthropic Claude if available, else OpenAI
    if anthropic_client and (profile.get("ai_provider") != "openai" or not openai_client):
        try:
            message = await asyncio.to_thread(
                anthropic_client.messages.create,
                model=os.getenv("ANTHROPIC_VISION_MODEL", os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929")),
                max_tokens=1600,
                system=SYSTEM_PROMPT,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(image_bytes).decode()}},
                        {"type": "text", "text": user_prompt}
                    ]
                }]
            )
            text_out = message.content[0].text
            m = pre.get("mode", "paper")
            if m == "paper":
                text_out = "**PAPER MODE** — practice (rules advisory)\n\n" + text_out
            elif m == "live":
                text_out = "**LIVE MODE** — own capital (custom rules)\n\n" + text_out
            else:
                text_out = "**FUNDED MODE** — Tradeify rules on\n\n" + text_out
            if not pre.get("ok"):
                text_out = "**RULE GATE: WAIT**\n" + "\n".join(f"• {b}" for b in pre["blockers"]) + "\n\n" + text_out
            return text_out
        except Exception as e:
            # Keep the real provider failure visible in the server logs and avoid
            # falsely claiming that no provider is configured.
            log.exception("Anthropic chart-vision request failed")
            if not openai_client:
                return (
                    f"Claude chart analysis failed ({type(e).__name__}): {str(e)[:500]}\n\n"
                    "No trade recommendation was generated. Check the bot server logs for details."
                )

    if openai_client:
        try:
            b64 = base64.b64encode(image_bytes).decode()
            response = await asyncio.to_thread(
                openai_client.chat.completions.create,
                model=os.getenv("OPENAI_VISION_MODEL", os.getenv("OPENAI_MODEL", "gpt-4o")),
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": user_prompt},
                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
                        ]
                    }
                ],
                max_tokens=1600
            )
            text_out = response.choices[0].message.content
            m = pre.get("mode", "paper")
            if m == "paper":
                text_out = "**PAPER MODE** — practice (rules advisory)\n\n" + text_out
            elif m == "live":
                text_out = "**LIVE MODE** — own capital (custom rules)\n\n" + text_out
            else:
                text_out = "**FUNDED MODE** — Tradeify rules on\n\n" + text_out
            if not pre.get("ok"):
                text_out = "**RULE GATE: WAIT**\n" + "\n".join(f"• {b}" for b in pre["blockers"]) + "\n\n" + text_out
            return text_out
        except Exception as e:
            log.error(f"OpenAI error: {e}")
            return f"AI analysis failed: {e}\n\nPlease try again or check API keys."

    log.error(
        "No usable vision provider at runtime: anthropic_client=%s, openai_client=%s, selected_provider=%r",
        bool(anthropic_client), bool(openai_client), profile.get("ai_provider")
    )
    return (
        "AI vision provider is unavailable in the running bot.\n"
        f"Claude client ready: {'yes' if anthropic_client else 'no'}; "
        f"OpenAI client ready: {'yes' if openai_client else 'no'}.\n"
        "Check the server logs for initialization errors. No AI chart analysis was completed, "
        "so no trade recommendation was generated."
    )

# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------
def account_size_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("25k", callback_data="size_25000"), InlineKeyboardButton("50k", callback_data="size_50000")],
        [InlineKeyboardButton("100k", callback_data="size_100000"), InlineKeyboardButton("150k", callback_data="size_150000")],
        [InlineKeyboardButton("Skip (crypto only)", callback_data="menu")],
        [InlineKeyboardButton("🏠 Home", callback_data="menu")],
    ])

def plan_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Growth", callback_data="plan_Growth"), InlineKeyboardButton("Select", callback_data="plan_Select")],
        [InlineKeyboardButton("Lightning", callback_data="plan_Lightning")],
    ])


# ---------------------------------------------------------------------------
# CRYPTO MODE — memecoins, snipe, copy-trade (strongest public-data stack)
# Uses DexScreener public API (no key). Hypothesis only — not financial advice.
# ---------------------------------------------------------------------------
import re as _re

def _dex_get(url: str, params: dict = None, timeout: int = 12):
    import requests as req
    r = req.get(url, params=params or {}, timeout=timeout, headers={"Accept": "application/json"})
    r.raise_for_status()
    return r.json()


def crypto_scan_memecoins(chain: str = "solana", limit: int = 8, risk_preset: str = "medium", no_rug: bool = True, anti_mev: bool = True) -> str:
    """Find high-confluence memecoin candidates filtered by risk preset."""
    chain = (chain or "solana").lower()
    chain_map = {
        "sol": "solana", "solana": "solana",
        "eth": "ethereum", "ethereum": "ethereum",
        "base": "base", "bsc": "bsc", "arb": "arbitrum", "arbitrum": "arbitrum",
    }
    chain = chain_map.get(chain, chain)
    rp = get_risk_preset(risk_preset)
    min_liq = float(rp.get("min_liq_usd") or 0)
    min_grade = (rp.get("min_grade") or "A").upper()
    grade_rank = {"A+": 3, "A": 2, "B": 1, "Skip": 0}
    min_rank = grade_rank.get(min_grade, 2)
    min_pair_age = max(0.0, float(rp.get("min_pair_age_min") or 0))
    try:
        # Boosted tokens = social/volume attention proxy
        data = _dex_get("https://api.dexscreener.com/token-boosts/top/v1")
        if not isinstance(data, list):
            data = data if isinstance(data, list) else []
        rows = [x for x in data if str(x.get("chainId", "")).lower() == chain][:40]
        if not rows:
            # fallback: search trending via token profiles
            data2 = _dex_get("https://api.dexscreener.com/token-profiles/latest/v1")
            rows = [x for x in (data2 or []) if str(x.get("chainId", "")).lower() == chain][:40]

        scored = []
        for t in rows:
            token_addr = t.get("tokenAddress") or t.get("address") or ""
            if not token_addr:
                continue
            # enrich with pairs
            try:
                pairs = _dex_get(f"https://api.dexscreener.com/token-pairs/v1/{chain}/{token_addr}")
            except Exception:
                pairs = []
            if not pairs:
                continue
            # best liquid pair
            best = max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd") or 0))
            # Enforce configured pair age; unknown age fails closed.
            if min_pair_age > 0:
                created = best.get("pairCreatedAt")
                try:
                    if created is None:
                        continue
                    pair_age_minutes = (
                        datetime.utcnow().timestamp() * 1000 - float(created)
                    ) / 60000.0
                    if pair_age_minutes < min_pair_age:
                        continue
                except (TypeError, ValueError, OverflowError):
                    continue
            liq = float(best.get("liquidity", {}).get("usd") or 0)
            vol = float(best.get("volume", {}).get("h24") or 0)
            chg = float(best.get("priceChange", {}).get("h24") or 0)
            txns = best.get("txns", {}).get("h24", {}) or {}
            buys = int(txns.get("buys") or 0)
            sells = int(txns.get("sells") or 0)
            pair_addr = best.get("pairAddress") or ""
            symbol = (best.get("baseToken") or {}).get("symbol") or "?"
            name = (best.get("baseToken") or {}).get("name") or symbol
            url = best.get("url") or f"https://dexscreener.com/{chain}/{pair_addr}"

            # A+ filter heuristics (public data only)
            score = 0
            reasons = []
            if liq >= 50000:
                score += 2; reasons.append("liq>$50k")
            elif liq >= 15000:
                score += 1; reasons.append("liq>$15k")
            if vol >= 100000:
                score += 2; reasons.append("vol24>$100k")
            elif vol >= 25000:
                score += 1; reasons.append("vol24>$25k")
            if buys + sells >= 200:
                score += 1; reasons.append("active flow")
            if sells > 0 and buys / max(sells, 1) >= 1.2:
                score += 1; reasons.append("buy-pressure")
            if 5 <= chg <= 120:
                score += 1; reasons.append("controlled pump")
            if chg > 300:
                score -= 2; reasons.append("extended risk")
            if liq < 5000:
                score -= 2; reasons.append("thin liq")
            if no_rug:
                if liq < min_liq:
                    score -= 3; reasons.append("no_rug:liq")
                if chg > 150:
                    score -= 2; reasons.append("no_rug:extended")
                if sells > buys * 1.8 and buys + sells > 50:
                    score -= 2; reasons.append("no_rug:sellflow")
            if anti_mev and chg > 80 and vol > 0 and liq > 0 and vol / max(liq, 1) > 15:
                score -= 1; reasons.append("anti_mev:turnover spike")

            grade = "A+" if score >= 6 else ("A" if score >= 4 else ("B" if score >= 2 else "Skip"))
            scored.append({
                "score": score, "grade": grade, "symbol": symbol, "name": name,
                "liq": liq, "vol": vol, "chg": chg, "buys": buys, "sells": sells,
                "pair": pair_addr, "token": token_addr, "url": url, "reasons": reasons,
            })

        scored.sort(key=lambda x: (-x["score"], -x["vol"]))
        # Risk preset filter: min liquidity + min grade
        filtered = [
            x for x in scored
            if x["liq"] >= min_liq and grade_rank.get(x["grade"], 0) >= min_rank
        ]
        # Strict enforcement: never show candidates that fail
        # the configured minimum liquidity or minimum grade.
        top = filtered[:limit]

        lines = [
            f"**CRYPTO SCAN — {chain.upper()}** (default chain: Solana)",
            f"**Preset: {rp['label']}** | min liq ${min_liq:,.0f} | min grade {min_grade} | risk {rp['max_risk_pct']}% | min TP {rp.get('min_rr', 1.5)}:1+",
            f"no_rug={'ON' if no_rug else 'off'} | anti_mev={'ON' if anti_mev else 'off'}",
            "Hypothesis only. Size per preset. Verify mint/LP before size.",
            "",
        ]
        if not top:
            lines.append("No candidates passed filters right now. Try again later or another chain.")
            return "\n".join(lines)

        for i, x in enumerate(top, 1):
            lines.append(
                f"**{i}. {x['symbol']}** [{x['grade']}] score {x['score']}\n"
                f"  liq ${x['liq']:,.0f} | vol24 ${x['vol']:,.0f} | 24h {x['chg']:+.1f}%\n"
                f"  flow buys/sells {x['buys']}/{x['sells']}\n"
                f"  why: {', '.join(x['reasons']) or 'mixed'}\n"
                f"  token: `{x['token']}`\n"
                f"  {x['url']}"
            )
            lines.append("")
        lines.append("Snipe a contract: send address or `/snipe <address>`")
        lines.append("Copy-trade: `/copyfind sol` or `/copyfind eth`")
        return "\n".join(lines)
    except Exception as e:
        return f"Crypto scan failed: {e}\nCheck network / try `/cryptoscan sol`"


def crypto_snipe_token(address: str, chain_hint: str = None, risk_preset: str = "medium", no_rug: bool = True, anti_mev: bool = True) -> str:
    """Deep-dive a contract/pair for entry hypothesis (snipe brief) under risk preset."""
    address = (address or "").strip()
    if not address:
        return "Send a token or pair address. Example: `/snipe <contract>`"
    rp = get_risk_preset(risk_preset)
    min_liq = float(rp.get("min_liq_usd") or 0)
    min_grade = (rp.get("min_grade") or "A").upper()
    grade_rank = {"A+": 3, "A": 2, "B": 1, "Skip": 0}
    min_rank = grade_rank.get(min_grade, 2)
    min_pair_age = max(0.0, float(rp.get("min_pair_age_min") or 0))
    try:
        # Search across chains
        data = _dex_get(f"https://api.dexscreener.com/latest/dex/tokens/{address}")
        pairs = data.get("pairs") or []
        if not pairs:
            # try as pair id search
            data = _dex_get(f"https://api.dexscreener.com/latest/dex/search", params={"q": address})
            pairs = data.get("pairs") or []
        if not pairs:
            return f"No pairs found for `{address}`. Check chain/address."

        if chain_hint:
            ch = chain_hint.lower()
            filtered = [p for p in pairs if ch in str(p.get("chainId", "")).lower()]
            if filtered:
                pairs = filtered

        best = max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd") or 0))
        chain = best.get("chainId") or "?"
        base = best.get("baseToken") or {}
        quote = best.get("quoteToken") or {}
        liq = float(best.get("liquidity", {}).get("usd") or 0)
        vol_h1 = float(best.get("volume", {}).get("h1") or 0)
        vol_h24 = float(best.get("volume", {}).get("h24") or 0)
        chg_m5 = float(best.get("priceChange", {}).get("m5") or 0)
        chg_h1 = float(best.get("priceChange", {}).get("h1") or 0)
        chg_h24 = float(best.get("priceChange", {}).get("h24") or 0)
        price = best.get("priceUsd") or "?"
        fdv = best.get("fdv") or best.get("marketCap")
        tx_h1 = best.get("txns", {}).get("h1", {}) or {}
        buys_h1 = int(tx_h1.get("buys") or 0)
        sells_h1 = int(tx_h1.get("sells") or 0)
        pair = best.get("pairAddress") or ""
        url = best.get("url") or f"https://dexscreener.com/{chain}/{pair}"
        created = best.get("pairCreatedAt")
        age = "age unverified"
        age_minutes = None
        if created:
            try:
                age_minutes = (
                    datetime.utcnow().timestamp() * 1000 - float(created)
                ) / 60000.0
                if age_minutes >= 0:
                    age_h = age_minutes / 60.0
                    age = f"{age_h:.1f}h old" if age_h < 48 else f"{age_h/24:.1f}d old"
                else:
                    age_minutes = None
            except (TypeError, ValueError, OverflowError):
                age_minutes = None

        # Grade
        score = 0
        reasons = []
        risks = []
        if liq >= 50000:
            score += 2; reasons.append("solid liq")
        elif liq >= 15000:
            score += 1; reasons.append("ok liq")
        else:
            risks.append("thin liquidity")
        if vol_h1 >= 10000:
            score += 1; reasons.append("hot 1h vol")
        if buys_h1 >= sells_h1 and buys_h1 >= 20:
            score += 1; reasons.append("1h buy bias")
        if sells_h1 > buys_h1 * 1.5:
            risks.append("sell pressure 1h")
        if chg_h24 > 200:
            risks.append("already extended 24h")
        if chg_m5 < -15:
            risks.append("dumping last 5m")
        if liq > 0 and vol_h24 / max(liq, 1) > 8:
            reasons.append("high turnover")
        grade = "A+" if score >= 4 and len(risks) == 0 else ("A" if score >= 3 else ("B" if score >= 2 else "Skip"))
        if liq < min_liq:
            risks.append(f"below preset min liq ${min_liq:,.0f}")
        if no_rug:
            if liq < min_liq:
                risks.append("no_rug: liquidity gate")
            if chg_h24 > 150:
                risks.append("no_rug: extended 24h")
            if sells_h1 > buys_h1 * 1.8 and (buys_h1 + sells_h1) > 20:
                risks.append("no_rug: sell flow")
        if anti_mev:
            if chg_m5 > 25 and chg_h1 > 40:
                risks.append("anti_mev: vertical tape — wait, don't chase")
            risks.append("anti_mev: use priority fee + avoid same-block ape vs bots")
        # Enforce configured minimum pair age. Missing/invalid age
        # fails closed when an age requirement is configured.
        pair_age_block = (
            min_pair_age > 0
            and (age_minutes is None or age_minutes < min_pair_age)
        )
        if pair_age_block:
            risks.append(
                f"pair age below minimum {min_pair_age:g} min or age unverified"
            )

        # Preset / safety gates force a non-entry recommendation.
        preset_block = (
            grade_rank.get(grade, 0) < min_rank
            or liq < min_liq
            or pair_age_block
            or (no_rug and any(r.startswith("no_rug") for r in risks))
        )

        min_rr = float(rp.get("min_rr") or 1.5)
        rec = "WAIT"
        if (not preset_block) and grade_rank.get(grade, 0) >= min_rank and "dumping last 5m" not in risks and "thin liquidity" not in risks:
            size_note = f"size <= {rp['max_risk_pct']}% bankroll | min TP {min_rr}:1"
            rec = f"HYPOTHESIS LONG ({size_note})" if chg_m5 > -8 else "WAIT for reclaim"
        if preset_block or "dumping last 5m" in risks or grade == "Skip":
            rec = f"WAIT / SKIP under {rp['label']} preset (min TP {min_rr}:1 required)"

        lines = [
            f"**SNIPE BRIEF — {base.get('symbol', '?')}/{quote.get('symbol', '?')}** [{grade}]",
            f"**Preset: {rp['label']}** | min grade {min_grade} | min liq ${min_liq:,.0f} | risk {rp['max_risk_pct']}%",
            f"Chain: {chain} | {age}",
            f"Price: ${price} | FDV/MC: {fdv}",
            f"Liq: ${liq:,.0f} | Vol 1h ${vol_h1:,.0f} | Vol 24h ${vol_h24:,.0f}",
            f"Change: 5m {chg_m5:+.1f}% | 1h {chg_h1:+.1f}% | 24h {chg_h24:+.1f}%",
            f"Tx 1h: buys {buys_h1} / sells {sells_h1}",
            f"Token: `{base.get('address') or address}`",
            f"Pair: `{pair}`",
            f"{url}",
            "",
            f"**Recommendation:** {rec}",
            f"**Why:** {', '.join(reasons) or 'mixed'}",
            f"**Risks:** {', '.join(risks) or 'standard memecoin risk (rug, tax, honeypot)'}",
            "",
            "**Entry precision (hypothesis):**",
            "• Trigger: hold above recent higher-low on 1m/5m + buy flow continues",
            "• Invalidation: loss of local HL or liq pull / sell spike",
            "• Size: max " + str(rp["max_risk_pct"]) + "% bankroll (preset); smaller until flow proves out",
            "• Min TP: " + str(rp.get("min_rr", 1.5)) + ":1+ vs invalidation — WAIT if target closer than 1.5R",
            "• TP plan: first scale at >=1.5R into liquidity; runner only if flow supports",
            "• Partial: scale out into vertical spikes; never market-dump whole bag into thin book",
            "",
            "Always verify: renounced/mint authority, LP lock, tax, honeypot on an independent checker before size.",
        ]
        return "\n".join(lines)
    except Exception as e:
        return f"Snipe analysis failed: {e}"


def crypto_copyfind(chain: str = "solana", risk_preset: str = "medium", no_rug: bool = True, anti_mev: bool = True) -> str:
    """Discover trader wallets through KOLScan/FOMO resources, never scan tokens here."""
    chain_in = (chain or "solana").lower()
    chain_map = {"sol": "solana", "solana": "solana", "eth": "ethereum", "ethereum": "ethereum", "base": "base"}
    chain_name = chain_map.get(chain_in, "solana")
    rp = get_risk_preset(risk_preset)
    lines = [
        f"**COPY-TRADE WALLET FINDER — {chain_name.upper()}**",
        f"**Risk preset:** {rp['label']} | **Max daily loss:** {rp['max_daily_loss_pct']}%",
        "",
        "This tool is for finding TRADERS' PUBLIC WALLETS — it does not scan or recommend tokens.",
        "",
        "**1️⃣ KOLScan — trader wallets and performance**",
        "🌐 Leaderboard: https://kolscan.io/leaderboard",
        "📈 Trades feed: https://kolscan.io/trades",
        "🔎 Wallet profiles: https://kolscan.io/leaderboard",
        "",
        "**2️⃣ FOMO — trader leaderboard**",
        "🌐 https://fomo.family",
        "Open Leaderboard, compare 7-day and 30-day results, then open a trader profile to find their public wallet address.",
        "",
        "**What to check before adding a wallet**",
        "• Prefer consistency across 7d/30d, not just one huge 24h gain.",
        "• Inspect trade history, drawdowns, position sizes, and repeated activity across different tokens.",
        "• Avoid wallets that only buy at launch, rely on insider/deployer access, or have suspicious bundled trades.",
        "• Past performance is not a guarantee; leaderboard PnL can be incomplete or misleading.",
        "",
        "**3️⃣ Save a trader to your Copy list**",
        "Use **➕ Add copy wallet** or send `/copyadd <wallet> <label>`.",
        "Then use `/copylist` to review or `/copyrm <wallet_or_label>` to remove.",
        "",
        f"Risk reminder: preset copy-size guidance is about {rp['copy_size_pct_of_their_buy']}% of the trader's buy size, but this is not an execution guarantee. Saving a wallet only tracks it; it does not automatically place trades.",
    ]
    # If an API key is configured, include actual public leaderboard results when available.
    try:
        board = fetch_kolscan_leaderboard(10)
        if board and "Live boards (open + pick wallets" not in board:
            lines.extend(["", "**Available KOLScan leaderboard data**", board])
    except Exception as exc:
        log.debug("Optional KOLScan leaderboard enrichment unavailable: %s", exc)
    return "\n".join(lines)


def looks_like_contract(text: str) -> bool:
    t = (text or "").strip()
    if not t or " " in t.strip():
        # allow single token
        pass
    # EVM
    if _re.fullmatch(r"0x[a-fA-F0-9]{40}", t):
        return True
    # Solana-ish base58 length
    if _re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,48}", t):
        return True
    return False






SOL_CUSTOM_DEFAULTS = {
    "max_risk_pct": 0.75,
    "max_daily_loss_pct": 3.0,
    "max_positions": 4,
    "min_liq_usd": 35000,
    "min_grade": "A",
    "min_rr": 1.5,
    "copy_size_pct_of_their_buy": 25,
    "min_pair_age_min": 15,
}

SOL_CUSTOM_FIELDS = {
    "max_risk_pct": ("Risk per trade (%)", 0.01, 10.0),
    "max_daily_loss_pct": ("Maximum daily loss (%)", 0.1, 50.0),
    "max_positions": ("Maximum open memes", 1, 50),
    "min_liq_usd": ("Minimum liquidity ($)", 1000, 10000000),
    "min_grade": ("Minimum grade (A+, A, or B)", None, None),
    "min_rr": ("Minimum take-profit R:R", 0.5, 20.0),
    "copy_size_pct_of_their_buy": ("Copy size vs trader buy (%)", 1, 100),
    "min_pair_age_min": ("Minimum pair age (minutes)", 0, 10080),
}

def effective_profile_risk(profile):
    """Return custom SOL values when selected, otherwise the named preset."""
    if (profile.get("risk_preset") or "medium").lower() == "custom":
        rules = dict(SOL_CUSTOM_DEFAULTS)
        saved = profile.get("custom_sol_rules") or {}
        rules.update({k: v for k, v in saved.items() if k in SOL_CUSTOM_DEFAULTS})
        rules["label"] = "CUSTOM SOL RULES"
        rules["notes"] = "Your saved custom SOL analysis settings."
        return rules
    return profile.get("risk_preset", "medium")

def sol_custom_rules_keyboard():
    rows = [
        [InlineKeyboardButton("Risk per trade", callback_data="sol_custom_edit_max_risk_pct"),
         InlineKeyboardButton("Daily loss limit", callback_data="sol_custom_edit_max_daily_loss_pct")],
        [InlineKeyboardButton("Max open memes", callback_data="sol_custom_edit_max_positions"),
         InlineKeyboardButton("Min liquidity", callback_data="sol_custom_edit_min_liq_usd")],
        [InlineKeyboardButton("Minimum grade", callback_data="sol_custom_edit_min_grade"),
         InlineKeyboardButton("Minimum TP R:R", callback_data="sol_custom_edit_min_rr")],
        [InlineKeyboardButton("Copy-size guidance", callback_data="sol_custom_edit_copy_size_pct_of_their_buy"),
         InlineKeyboardButton("Minimum pair age", callback_data="sol_custom_edit_min_pair_age_min")],
        [InlineKeyboardButton("✅ Use Custom Rules", callback_data="sol_custom_use")],
        [InlineKeyboardButton("🔄 Reset values to Medium", callback_data="sol_custom_reset")],
        [InlineKeyboardButton("⬅️ Back to risk presets", callback_data="risk_preset_menu")],
    ]
    return InlineKeyboardMarkup(rows)

def sol_custom_rules_text(profile):
    rules = dict(SOL_CUSTOM_DEFAULTS)
    rules.update({k: v for k, v in (profile.get("custom_sol_rules") or {}).items()
                  if k in SOL_CUSTOM_DEFAULTS})
    active = (profile.get("risk_preset") or "medium").lower() == "custom"
    status = "ACTIVE" if active else "Saved, not active"
    return (
        "**Custom SOL Rules**\\n"
        f"Status: **{status}**\\n\\n"
        f"• Risk/trade: {rules['max_risk_pct']}% of bankroll\\n"
        f"• Maximum daily loss: {rules['max_daily_loss_pct']}%\\n"
        f"• Maximum open memes: {rules['max_positions']}\\n"
        f"• Minimum liquidity: ${rules['min_liq_usd']:,.0f}\\n"
        f"• Minimum grade: {rules['min_grade']}\\n"
        f"• Minimum TP R:R: {rules['min_rr']}:1\\n"
        f"• Copy-size guidance: {rules['copy_size_pct_of_their_buy']}%\\n"
        f"• Minimum pair age: {rules['min_pair_age_min']} minutes\\n\\n"
        "Choose a setting to edit. Changes are saved to your profile."
    )


def get_risk_preset(name: str) -> dict:
    """Strongest practical preset packs for Solana memecoins."""
    if isinstance(name, dict):
        return name
    name = (name or "medium").lower()
    presets = {
        "chill": {
            "label": "CHILL (strongest safety)",
            "max_risk_pct": 0.25,
            "max_daily_loss_pct": 1.0,
            "max_positions": 2,
            "min_liq_usd": 100000,
            "min_grade": "A+",
            "min_rr": 1.5,
            "copy_size_pct_of_their_buy": 10,
            "min_pair_age_min": 45,
            "notes": "Sol only. A+ only. Min TP 1.5:1. Deep liq, no chase.",
        },
        "medium": {
            "label": "MEDIUM (best default)",
            "max_risk_pct": 0.75,
            "max_daily_loss_pct": 3.0,
            "max_positions": 4,
            "min_liq_usd": 35000,
            "min_grade": "A",
            "min_rr": 1.5,
            "copy_size_pct_of_their_buy": 25,
            "min_pair_age_min": 15,
            "notes": "Sol memes only. A/A+ setups. Min TP 1.5:1. Scale out into strength.",
        },
        "degen": {
            "label": "DEGEN (risky / big upside)",
            "max_risk_pct": 2.0,
            "max_daily_loss_pct": 8.0,
            "max_positions": 8,
            "min_liq_usd": 10000,
            "min_grade": "B",
            "min_rr": 1.5,
            "copy_size_pct_of_their_buy": 50,
            "min_pair_age_min": 3,
            "notes": "High variance Sol. Min TP still 1.5:1. no_rug still applies.",
        },
        "risky": {
            "label": "DEGEN (risky / big upside)",
            "max_risk_pct": 2.0,
            "max_daily_loss_pct": 8.0,
            "max_positions": 8,
            "min_liq_usd": 10000,
            "min_grade": "B",
            "min_rr": 1.5,
            "copy_size_pct_of_their_buy": 50,
            "min_pair_age_min": 3,
            "notes": "High variance Sol. Min TP still 1.5:1. no_rug still applies.",
        },
    }
    return presets.get(name, presets["medium"])


def format_risk_preset(profile: dict) -> str:
    p = get_risk_preset(effective_profile_risk(profile))
    return (
        f"**Risk preset: {p['label']}**\n"
        f"• Risk/trade: {p['max_risk_pct']}% of bankroll\n"
        f"• Max daily loss: {p['max_daily_loss_pct']}%\n"
        f"• Max open memes: {p['max_positions']}\n"
        f"• Min liquidity: ${p['min_liq_usd']:,}\n"
        f"• Min grade: {p['min_grade']}\n"
        f"• Min TP R:R: {p.get('min_rr', 1.5)}:1+\n"
        f"• Copy size vs their buy: ~{p['copy_size_pct_of_their_buy']}%\n"
        f"• {p['notes']}"
    )


def fetch_kolscan_leaderboard(limit: int = 15) -> str:
    """Best-effort KOLScan leaderboard. Falls back to deep links if blocked."""
    import os
    lines = ["**KOLSCAN / KOL LEADERBOARD**", ""]
    # Optional Solana Tracker key for real API
    key = os.getenv("SOLANA_TRACKER_API_KEY") or os.getenv("SOLANATRACKER_API_KEY") or ""
    if key:
        try:
            import requests as req
            r = req.get(
                "https://data.solanatracker.io/v2/pnl/leaderboard/kols",
                params={"sort": "realized", "direction": "desc", "limit": limit},
                headers={"x-api-key": key, "Accept": "application/json"},
                timeout=15,
            )
            if r.ok:
                data = r.json()
                traders = data.get("traders") or data.get("data") or data if isinstance(data, list) else []
                if isinstance(traders, dict):
                    traders = traders.get("traders") or []
                for i, t in enumerate(traders[:limit], 1):
                    w = t.get("wallet") or t.get("address") or "?"
                    ident = (t.get("identity") or {})
                    name = ident.get("name") or t.get("name") or w[:6] + "…" + w[-4:]
                    pnl = t.get("pnl") or {}
                    realized = pnl.get("realized") if isinstance(pnl, dict) else t.get("realized")
                    win = None
                    if isinstance(t.get("averages"), dict):
                        pass
                    win = t.get("win_percentage") or (t.get("stats") or {}).get("win_percentage")
                    lines.append(f"{i}. **{name}** `{w}`")
                    if realized is not None:
                        lines.append(f"   realized PnL: ${float(realized):,.0f}" + (f" | win {win}%" if win else ""))
                    lines.append(f"   kolscan: https://kolscan.io/account/{w}")
                    lines.append("")
                lines.append("Add to copy list: `/copyadd <wallet> <label>`")
                return "\n".join(lines)
        except Exception as e:
            lines.append(f"Solana Tracker fetch failed: {e}")

    # Public link pack (always works)
    lines.extend([
        "Live boards (open + pick wallets to `/copyadd`):",
        "• KOLScan leaderboard: https://kolscan.io/leaderboard",
        "• KOLScan trades feed: https://kolscan.io/trades",
        "• FOMO leaderboard: https://fomo.family (app/web → Leaderboard tab)",
        "• Solana Tracker Kolscan view: https://www.solanatracker.io/leaderboard/kolscan",
        "",
        "How to use:",
        "1. Open KOLScan daily/weekly board — sort by PnL + check win rate",
        "2. Open FOMO leaderboard — 24h / 7d / 30d, follow consistent names",
        "3. `/copyadd <wallet> <name>` to save them here",
        "4. `/copylist` to see your book | `/copyrm <wallet>` to remove",
        "5. Size with `/riskpreset chill|medium|degen`",
        "",
        "Optional: set SOLANA_TRACKER_API_KEY in .env for in-chat KOL ranks.",
    ])
    return "\n".join(lines)


def fetch_fomo_board_help() -> str:
    return (
        "**FOMO LEADERBOARD**\n"
        "FOMO ranks traders by real results (not followers).\n"
        "• Web: https://fomo.family → Leaderboard\n"
        "• Use 24h for heat, 7d/30d for consistency\n"
        "• Open profile → trade history → if wallet is public, `/copyadd <wallet> fomo_<name>`\n"
        "• Follow in FOMO for feed alerts; copy-size still follows your risk preset\n"
        "\n"
        "Pair with KOLScan: `/kolscan` + FOMO board = best discovery loop."
    )




def fetch_token_price_usd(token: str) -> dict:
    """Price + symbol from DexScreener for a mint/pair address."""
    token = (token or "").strip()
    if not token:
        return {}
    try:
        data = _dex_get(f"https://api.dexscreener.com/latest/dex/tokens/{token}")
        pairs = data.get("pairs") or []
        if not pairs:
            data = _dex_get("https://api.dexscreener.com/latest/dex/search", params={"q": token})
            pairs = data.get("pairs") or []
        if not pairs:
            return {}
        # Prefer solana pair with most liquidity
        sol = [p for p in pairs if str(p.get("chainId", "")).lower() == "solana"]
        pool = sol if sol else pairs
        best = max(pool, key=lambda p: float(p.get("liquidity", {}).get("usd") or 0))
        base = best.get("baseToken") or {}
        price = float(best.get("priceUsd") or 0)
        return {
            "price": price,
            "symbol": base.get("symbol") or "?",
            "name": base.get("name") or "?",
            "token": base.get("address") or token,
            "url": best.get("url") or "",
            "chg_h24": float(best.get("priceChange", {}).get("h24") or 0),
        }
    except Exception:
        return {}


def list_positions_text(profile: dict, refresh: bool = True) -> str:
    positions = [p for p in (profile.get("sol_positions") or []) if p.get("status", "open") == "open"]
    if not positions:
        return (
            "**Sol positions** — none open.\n"
            "Add: `/pos add <token_mint> <amount> [entry_price]`\n"
            "Example: `/pos add 7xK... 100000 0.000012`"
        )
    rows = []
    for p in positions:
        price = p.get("last_price")
        sym = p.get("symbol") or "?"
        if refresh:
            info = fetch_token_price_usd(p.get("token") or "")
            if info:
                price = info.get("price")
                sym = info.get("symbol") or sym
                p["last_price"] = price
                p["symbol"] = sym
                p["url"] = info.get("url") or p.get("url")
                p["chg_h24"] = info.get("chg_h24")
        entry = float(p.get("entry") or 0)
        amt = float(p.get("amount") or 0)
        px = float(price or 0)
        value = px * amt if px else None
        pnl_pct = ((px - entry) / entry * 100) if entry and px else None
        rows.append({**p, "symbol": sym, "last_price": px, "value": value, "pnl_pct": pnl_pct})

    # sort by coin price high → low
    rows.sort(key=lambda r: float(r.get("last_price") or 0), reverse=True)

    # persist refreshed prices
    all_pos = profile.get("sol_positions") or []
    by_id = {r.get("id"): r for r in rows}
    for i, p in enumerate(all_pos):
        if p.get("id") in by_id and p.get("status", "open") == "open":
            all_pos[i]["last_price"] = by_id[p["id"]].get("last_price")
            all_pos[i]["symbol"] = by_id[p["id"]].get("symbol")
            all_pos[i]["url"] = by_id[p["id"]].get("url")
            all_pos[i]["chg_h24"] = by_id[p["id"]].get("chg_h24")

    lines = ["**Sol positions** (sorted price high → low)", ""]
    total = 0.0
    for i, r in enumerate(rows, 1):
        px = r.get("last_price") or 0
        val = r.get("value")
        if val:
            total += val
        pnl = r.get("pnl_pct")
        pnl_s = f"{pnl:+.1f}%" if pnl is not None else "n/a"
        val_s = f"${val:,.2f}" if val is not None else "n/a"
        lines.append(
            f"**{i}. {r.get('symbol')}**  price ${px:.8g}\n"
            f"   amt {r.get('amount')} | entry {r.get('entry')} | value {val_s} | PnL {pnl_s}\n"
            f"   `{r.get('token')}`\n"
            f"   close: `/pos close {r.get('symbol')}`"
        )
        lines.append("")
    lines.append(f"**Total mark:** ${total:,.2f}")
    lines.append("Refresh: `/pos refresh` · Close all: `/pos close all`")
    return "\n".join(lines)




def crypto_pnl_text(profile: dict, refresh: bool = True) -> str:
    """Open PnL + lifetime wins/losses for Sol desk."""
    # refresh open marks
    _ = list_positions_text(profile, refresh=refresh)
    opens = [p for p in (profile.get("sol_positions") or []) if p.get("status", "open") == "open"]
    closed = list(profile.get("crypto_closed") or [])
    wins = [c for c in closed if float(c.get("pnl_pct") or 0) > 0]
    losses = [c for c in closed if float(c.get("pnl_pct") or 0) <= 0]
    open_pnl_usd = 0.0
    open_lines = []
    for p in sorted(opens, key=lambda x: float(x.get("last_price") or 0), reverse=True):
        entry = float(p.get("entry") or 0)
        px = float(p.get("last_price") or 0)
        amt = float(p.get("amount") or 0)
        val = px * amt if px else 0
        cost = entry * amt if entry else 0
        pnl_u = val - cost
        open_pnl_usd += pnl_u
        pnl_p = ((px - entry) / entry * 100) if entry and px else 0
        open_lines.append(f"• {p.get('symbol')}: {pnl_p:+.1f}% (${pnl_u:+,.2f})")

    estimated_tracker_pnl = sum(float(c.get("pnl_usd") or 0) for c in closed)
    lines = [
        "**Sol PnL / Tracker Summary**",
        "",
        f"Open positions: {len(opens)} | mark PnL: ${open_pnl_usd:+,.2f}",
    ]
    lines.extend(open_lines or ["• none open"])
    lines.append("")
    lines.append(
        f"Tracker closes: {len(closed)} | estimated wins {len(wins)} | "
        f"estimated losses {len(losses)}"
    )
    wr = (len(wins) / len(closed) * 100) if closed else 0
    lines.append(
        f"Estimated tracker win rate: {wr:.0f}% | "
        f"estimated tracker PnL: ${estimated_tracker_pnl:+,.2f}"
    )
    lines.append("Tracker figures are not verified on-chain sale results.")
    if closed:
        lines.append("")
        lines.append("Last closes:")
        for c in closed[-5:][::-1]:
            lines.append(
                f"• {c.get('symbol')} {float(c.get('pnl_pct') or 0):+.1f}% "
                f"(${float(c.get('pnl_usd') or 0):+,.2f})"
            )
    ca = profile.get("crypto_alerts") or {}
    lines.append("")
    lines.append(
        f"Alerts: wins={'ON' if ca.get('wins', True) else 'off'} "
        f"losses={'ON' if ca.get('losses', True) else 'off'} "
        f"pnl_moves={'ON' if ca.get('pnl_moves', True) else 'off'} "
        f"@ {ca.get('pnl_threshold_pct', 20)}%"
    )
    return "\n".join(lines)


def record_crypto_close(profile: dict, pos: dict) -> str:
    """Move open pos stats into crypto_closed; return alert text if any."""
    entry = float(pos.get("entry") or 0)
    px = float(pos.get("last_price") or entry or 0)
    amt = float(pos.get("amount") or 0)
    pnl_pct = ((px - entry) / entry * 100) if entry and px else 0.0
    pnl_usd = (px - entry) * amt if entry else 0.0
    rec = {
        "id": pos.get("id"),
        "token": pos.get("token"),
        "symbol": pos.get("symbol"),
        "entry": entry,
        "exit": px,
        "amount": amt,
        "pnl_pct": pnl_pct,
        "pnl_usd": pnl_usd,
        "closed_at": datetime.utcnow().isoformat(),
        "result": "win" if pnl_pct > 0 else "loss",
        "close_type": "tracker_only",
        "realized_confirmed": False,
    }
    closed = list(profile.get("crypto_closed") or [])
    closed.append(rec)
    profile["crypto_closed"] = closed[-100:]
    ca = profile.get("crypto_alerts") or {}
    alert = ""
    if pnl_pct > 0 and ca.get("wins", True):
        alert = f"TRACKER WIN (unverified): {rec['symbol']} {pnl_pct:+.1f}% (${pnl_usd:+,.2f})"
    elif pnl_pct <= 0 and ca.get("losses", True):
        alert = f"TRACKER LOSS (unverified): {rec['symbol']} {pnl_pct:+.1f}% (${pnl_usd:+,.2f})"
    return alert


def check_open_pnl_alerts(profile: dict) -> list:
    """Return alert strings for open positions crossing threshold."""
    ca = profile.get("crypto_alerts") or {}
    if not ca.get("pnl_moves", True):
        return []
    thr = float(ca.get("pnl_threshold_pct") or 20)
    alerts = []
    for p in profile.get("sol_positions") or []:
        if p.get("status", "open") != "open":
            continue
        entry = float(p.get("entry") or 0)
        px = float(p.get("last_price") or 0)
        if not entry or not px:
            continue
        pnl_p = (px - entry) / entry * 100
        key = "alerted_up" if pnl_p >= thr else ("alerted_down" if pnl_p <= -thr else None)
        if pnl_p >= thr and not p.get("alerted_up"):
            p["alerted_up"] = True
            alerts.append(f"PNL ALERT: {p.get('symbol')} {pnl_p:+.1f}% (hit +{thr:.0f}%)")
        elif pnl_p <= -thr and not p.get("alerted_down"):
            p["alerted_down"] = True
            alerts.append(f"PNL ALERT: {p.get('symbol')} {pnl_p:+.1f}% (hit -{thr:.0f}%)")
    return alerts


def positions_keyboard(profile: dict) -> InlineKeyboardMarkup:
    positions = [p for p in (profile.get("sol_positions") or []) if p.get("status", "open") == "open"]
    rows = [
        [
            InlineKeyboardButton("Refresh", callback_data="pos_refresh"),
            InlineKeyboardButton("Close ALL", callback_data="pos_close_all"),
        ]
    ]
    # up to 8 individual close buttons
    for p in sorted(positions, key=lambda x: float(x.get("last_price") or 0), reverse=True)[:8]:
        sym = p.get("symbol") or p.get("id", "x")[:6]
        rows.append([InlineKeyboardButton(f"Close {sym}", callback_data=f"pos_close_{p.get('id')}")])
    rows.append([InlineKeyboardButton("Menu", callback_data="menu")])
    return InlineKeyboardMarkup(rows)



def main_menu_keyboard(profile: Dict) -> InlineKeyboardMarkup:
    """Home: pick Stocks/Futures desk OR Sol crypto desk — separate systems."""
    mode = (profile.get("trading_mode") or "paper").upper()
    preset = (profile.get("risk_preset") or "medium").upper()
    open_pos = len([p for p in (profile.get("sol_positions") or []) if p.get("status", "open") == "open"])
    last = profile.get("last_desk") or "home"
    switch_label = "SWITCH DESK (Sol)" if last != "crypto" else "SWITCH DESK (Stocks)"
    active = profile.get("active_account") or "no account"
    if len(str(active)) > 18:
        active = str(active)[:16] + ".."
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("=== STOCKS / FUTURES ===", callback_data="hub_stocks")],
        [InlineKeyboardButton(f"=== SOL CRYPTO ({open_pos} pos) ===", callback_data="hub_crypto")],
        [InlineKeyboardButton("TOGGLE DESK", callback_data="switch_desk")],
        [
            InlineKeyboardButton(f"Mode {mode}", callback_data="mode_menu"),
            InlineKeyboardButton(f"🛡️ Risk {preset}", callback_data="risk_preset_menu"),
        ],
        [
            InlineKeyboardButton(f"Acct: {active}", callback_data="switch_account"),
            InlineKeyboardButton("👤 Change account", callback_data="switch_account"),
        ],
    ])


def hub_stocks_keyboard(profile: Dict) -> InlineKeyboardMarkup:
    """Futures / stocks / Tradeify only — no memecoin tools."""
    mode = (profile.get("trading_mode") or "paper").upper()
    active = profile.get("active_account") or "No acct"
    if len(str(active)) > 16:
        active = str(active)[:14] + ".."
    bias = profile.get("daily_bias") or "bias?"
    strict = "ON" if profile.get("strict_mode") else "OFF"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📈 Analyze Chart", callback_data="analyze")],
        [
            InlineKeyboardButton("🎯 Strong Setup", callback_data="strong_opportunity"),
            InlineKeyboardButton("🕒 Session", callback_data="session_info"),
        ],
        [
            InlineKeyboardButton("📡 Status", callback_data="status"),
            InlineKeyboardButton("📊 Asset", callback_data="asset_info"),
        ],
        [
            InlineKeyboardButton("📓 Journal", callback_data="journal"),
            InlineKeyboardButton("📅 Weekly", callback_data="weekly"),
        ],
        [InlineKeyboardButton("⚙️ Stocks Desk Settings", callback_data="stocks_settings")],
        [
            InlineKeyboardButton(f"🔒 Strict {strict}", callback_data="toggle_strict"),
            InlineKeyboardButton(f"🧭 Bias {bias}", callback_data="status"),
        ],
        [
            InlineKeyboardButton(f"👤 Acct {active}", callback_data="switch_account"),
            InlineKeyboardButton(f"🔄 Mode {mode}", callback_data="mode_menu"),
        ],
        [InlineKeyboardButton("🔁 Switch to SOL Crypto", callback_data="switch_desk")],
        [InlineKeyboardButton("👥 Change Account", callback_data="switch_account")],
        [InlineKeyboardButton("🏠 Home", callback_data="menu")],
    ])


def stocks_settings_keyboard() -> InlineKeyboardMarkup:
    """Settings shortcuts for the Stocks/Futures desk."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛡️ Rules", callback_data="show_rules")],
        [InlineKeyboardButton("⚙️ Custom LIVE Rules (/customrules)", callback_data="customrules_btn")],
        [InlineKeyboardButton("🔔 Alerts", callback_data="alerts")],
        [InlineKeyboardButton("🕒 Session Timezone", callback_data="stock_timezone_menu")],
        [InlineKeyboardButton("⬅️ Back to Stocks Desk", callback_data="hub_stocks")],
        [InlineKeyboardButton("🏠 Home", callback_data="menu")],
    ])


def hub_crypto_keyboard(profile: Dict) -> InlineKeyboardMarkup:
    """SOL desk home: core tools and risk preset."""
    preset = (profile.get("risk_preset") or "medium").upper()
    open_pos = len([
        p for p in (profile.get("sol_positions") or [])
        if p.get("status", "open") == "open"
    ])
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔍 Scan memes", callback_data="crypto_scan"),
            InlineKeyboardButton("🎯 Snipe a contract", callback_data="crypto_snipe_start"),
        ],
        [
            InlineKeyboardButton("🧾 Buy Quote Preview", callback_data="crypto_buy_preview_start"),
        ],
        [
            InlineKeyboardButton("🔎 Copy-trade", callback_data="crypto_copyfind_sol"),
            InlineKeyboardButton("➕ Add copy wallet", callback_data="crypto_copyadd_start"),
        ],
        [
            InlineKeyboardButton("📋 Copy list", callback_data="copy_list_btn"),
            InlineKeyboardButton(f"📊 Positions ({open_pos})", callback_data="pos_menu"),
        ],
        [
            InlineKeyboardButton("🌐 KOLScan", callback_data="kolscan_btn"),
            InlineKeyboardButton("🔥 FOMO", callback_data="fomo_btn"),
        ],
        [
            InlineKeyboardButton("📊 Risk & Positions", callback_data="crypto_risk_positions"),
        ],
        [
            InlineKeyboardButton(f"🛡️ Risk {preset}", callback_data="risk_preset_menu"),
        ],
        [
            InlineKeyboardButton("⚙️ Crypto Settings", callback_data="crypto_settings"),
        ],
        [InlineKeyboardButton("TOGGLE -> STOCKS/FUTURES", callback_data="switch_desk")],
        [InlineKeyboardButton("🏠 Home", callback_data="menu")],
    ])


def hub_crypto_settings_keyboard(profile: Dict) -> InlineKeyboardMarkup:
    """SOL settings: all controls other than the home-screen risk preset."""
    norug = "ON" if profile.get("no_rug_mode", True) else "OFF"
    mev = "ON" if profile.get("anti_mev", True) else "OFF"
    xdisc = "ON" if profile.get("x_discovery_enabled", False) else "OFF"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"🧹 No Rug: {norug}", callback_data="toggle_norug"),
            InlineKeyboardButton(f"🛡️ Anti-MEV: {mev}", callback_data="toggle_antimev"),
        ],
        [
            InlineKeyboardButton("👛 Wallet", callback_data="wallet_info"),
            InlineKeyboardButton(f"𝕏 Discovery: {xdisc}", callback_data="toggle_x_discovery"),
        ],
        [
            InlineKeyboardButton("🔎 Search X", callback_data="x_discovery_search"),
        ],
        [
            InlineKeyboardButton("🔔 Crypto Alerts", callback_data="crypto_alerts_menu"),
        ],
        [
            InlineKeyboardButton("💸 Pump.fun Cashback", callback_data="pumpcash_menu"),
            InlineKeyboardButton("🔥 Incinerator", callback_data="incinerator_menu"),
        ],
        [
            InlineKeyboardButton("💰 Withdraw SOL", callback_data="deposit_start"),
        ],
        [InlineKeyboardButton("⬅️ Back to SOL Desk", callback_data="hub_crypto")],
        [InlineKeyboardButton("🏠 Home", callback_data="menu")],
    ])



def timezone_keyboard() -> InlineKeyboardMarkup:
    """Common timezone choices, stored as IANA timezone identifiers."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Eastern", callback_data="stocktzpick_eastern"),
            InlineKeyboardButton("Central", callback_data="stocktzpick_central"),
        ],
        [
            InlineKeyboardButton("Mountain", callback_data="stocktzpick_mountain"),
            InlineKeyboardButton("Pacific", callback_data="stocktzpick_pacific"),
        ],
        [
            InlineKeyboardButton("Alaska", callback_data="stocktzpick_alaska"),
            InlineKeyboardButton("Hawaii", callback_data="stocktzpick_hawaii"),
        ],
        [InlineKeyboardButton("UTC", callback_data="stocktzpick_utc")],
        [InlineKeyboardButton("⬅️ Stocks Desk Settings", callback_data="stocks_settings")],
        [InlineKeyboardButton("🏠 Home", callback_data="menu")],
    ])


def hub_account_keyboard(profile: Dict) -> InlineKeyboardMarkup:
    mode = (profile.get("trading_mode") or "paper").upper()
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Mode: " + mode, callback_data="mode_menu")],
        [InlineKeyboardButton("Progress", callback_data="status"), InlineKeyboardButton("Rules", callback_data="show_rules")],
        [InlineKeyboardButton("Switch account", callback_data="switch_account")],
        [
            InlineKeyboardButton("Stocks desk", callback_data="hub_stocks"),
            InlineKeyboardButton("Sol desk", callback_data="hub_crypto"),
        ],
        [InlineKeyboardButton("🏠 Home", callback_data="menu")],
    ])


def ui_header(profile: Dict, desk: str = "home") -> str:
    mode = (profile.get("trading_mode") or "paper").upper()
    preset = (profile.get("risk_preset") or "medium").upper()
    bias = profile.get("daily_bias") or "not set"
    active = profile.get("active_account") or "none"
    open_pos = len([p for p in (profile.get("sol_positions") or []) if p.get("status", "open") == "open"])
    norug = "ON" if profile.get("no_rug_mode", True) else "off"
    if desk == "stocks":
        return (
            "**Brazy Bot — STOCKS / FUTURES**\n"
            f"mode {mode} | bias {bias} | account {active}\n"
            "Charts, Tradeify rules, sessions, journal.\n"
        )
    if desk == "crypto":
        return (
            "**Brazy Bot — SOL CRYPTO**\n"
            f"risk {preset} | no_rug {norug} | positions {open_pos}\n"
            "Scan, snipe, copy, positions. Not Tradeify.\n"
        )
    return (
        "**Brazy Bot**\n"
        f"{mode} | risk {preset} | bias {bias}\n"
        f"account {active} | sol pos {open_pos}\n"
        "Pick a desk — stocks and Sol are separate.\n"
    )


async def sol_balance_line(profile: Dict) -> str:
    """Show the saved public Solana wallet balance."""
    wallet = (profile.get("sol_wallet") or "").strip()
    if not wallet:
        return "SOL balance: No wallet connected. Use /wallet to connect one."
    balance = await asyncio.to_thread(sol_rpc_balance, wallet)
    if balance is None:
        return "SOL balance: Unavailable (RPC request failed)."
    return f"SOL balance: {balance:.6f} SOL"



async def crypto_header_with_balance(profile: Dict) -> str:
    """Crypto header plus current saved wallet balance."""
    return ui_header(profile, "crypto") + "\n" + await sol_balance_line(profile)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        if not user_is_licensed(user_id):
            await update.message.reply_text(
                "Not licensed on this bot.\nYour Telegram ID: %s\nSend this ID to get access." % user_id
            )
            return
        profile = get_user_profile(user_id)
        if not profile.get("timezone"):
            await update.message.reply_text(
                "🕒 **Choose your timezone**\\n\\n"
                "This saves your preferred local timezone to your profile. "
                "Stock-market schedules remain based on New York time.",
                parse_mode="Markdown",
                reply_markup=timezone_keyboard(),
            )
            return
        text_out = (
            ui_header(profile, "home")
            + "\n"
            + await sol_balance_line(profile)
            + "\nTap a desk below.\n"
            + "STOCKS = charts + Tradeify\n"
            + "SOL CRYPTO = memes + wallet\n"
            + "Change account anytime if size/plan is wrong."
        )
        await update.message.reply_text(
            text_out,
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(profile),
        )
    except Exception as e:
        log.exception("start failed")
        try:
            await update.message.reply_text(
                "Bot error on /start: %s" % e,
                reply_markup=main_menu_keyboard(get_user_profile(user_id)),
            )
        except Exception:
            await update.message.reply_text("start error: %s" % e)



async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass
    user_id = query.from_user.id
    try:
        if not user_is_licensed(user_id):
            await safe_edit(query, "Not licensed. Your Telegram ID: %s" % user_id)
            return
        profile = get_user_profile(user_id)
        data = query.data
    except Exception as e:
        log.exception("button profile load")
        await safe_edit(query, "Button error: %s" % e)
        return

    try:
      await _button_dispatch(update, context, query, user_id, profile, data)
    except Exception as e:
        log.exception("button dispatch %s", data if "data" in dir() else "?")
        await safe_edit(query, "Button failed: %s" % e)


async def _button_dispatch(update, context, query, user_id, profile, data):
    """All button routes — errors bubble to button_handler."""
    if data == "wallet_balance":
        w = (profile.get("sol_wallet") or "").strip()
        if not w:
            await query.edit_message_text("No saved wallet address.")
            return
        bal = await asyncio.to_thread(sol_rpc_balance, w)
        bal_s = f"{bal:.6f} SOL" if bal is not None else "Unavailable"
        has_key = bool(profile.get("sol_burner_key"))
        await query.edit_message_text(
            f"**Sol wallet**\\nAddress: `{w}`\\n"
            f"Key loaded: {'YES (burner)' if has_key else 'no (watch only)'}\\n"
            f"Balance: {bal_s}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🔄 Refresh Balance", callback_data="wallet_balance"),
                    InlineKeyboardButton("🔎 View on Solscan", url=f"https://solscan.io/account/{w}"),
                ],
                [
                    InlineKeyboardButton("🗑️ Clear Wallet", callback_data="wallet_clear_confirm"),
                    InlineKeyboardButton("💸 Withdraw", callback_data="wallet_withdraw"),
                ],
            ]),
            disable_web_page_preview=True,
        )
        return

    if data == "wallet_clear_confirm":
        await query.edit_message_text(
            "⚠️ **Clear the saved wallet?**\\n\\n"
            "This removes the saved public address and burner key from Brazy. "
            "It does not transfer funds or delete the on-chain wallet.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Yes, clear wallet", callback_data="wallet_clear_yes")],
                [InlineKeyboardButton("Cancel", callback_data="wallet_balance")],
            ]),
        )
        return

    if data == "wallet_clear_yes":
        update_user_profile(user_id, {"sol_wallet": None, "sol_burner_key": None})
        await query.edit_message_text(
            "Saved wallet address and burner key cleared from Brazy. "
            "No on-chain funds were moved."
        )
        return

    if data == "wallet_withdraw":
        await query.edit_message_text(
            "💸 Withdrawals are currently disabled for safety. "
            "No funds were sent.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Sol desk", callback_data="hub_crypto")],
            ]),
        )
        return

    if data.startswith("size_"):
        size = int(data.split("_")[1])
        context.user_data["temp_size"] = size
        await query.edit_message_text(
            f"Size set to **${size:,}**\n\n**Step 2 — Choose plan:**",
            reply_markup=plan_keyboard(),
            parse_mode="Markdown"
        )
        return

    if data.startswith("plan_"):
        plan = data.split("_")[1]
        size = context.user_data.get("temp_size")
        if not size:
            await query.edit_message_text("Error. Please /start again.")
            return
        account_key = f"{plan}_{size}"
        rules = TRADEIFY_RULES.get(plan, {}).get(size)
        if not rules:
            await query.edit_message_text("Invalid combination. /start again.")
            return

        profile = get_user_profile(user_id)
        accounts = dict(profile.get("accounts") or {})
        accounts[account_key] = {
            "plan": plan, "size": size, "rules": rules,
            "created": datetime.utcnow().isoformat()
        }
        update_user_profile(user_id, {
            "accounts": accounts,
            "active_account": account_key,
        })
        profile = get_user_profile(user_id)
        await query.edit_message_text(
            f"Account set: **{plan} ${size:,}**\n"
            f"Max DD: ${rules['max_drawdown']:,}\n\n"
            "Pick a desk below. Change account anytime.",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(profile),
        )
        return

    if data == "menu":
        profile = get_user_profile(user_id)
        home_text = (
            ui_header(profile, "home")
            + "\n"
            + await sol_balance_line(profile)
            + "\nChoose a desk:"
        )
        await query.edit_message_text(
            home_text,
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(profile),
        )
        return

    if data == "analyze":
        profile = get_user_profile(user_id)
        await query.edit_message_text(
            ui_header(profile, "stocks") + "\nSend a chart screenshot now.\nCaption ticker (NQ, ES, AAPL).\nCrypto belongs on Sol desk.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("< Stocks desk", callback_data="hub_stocks")]]),
        )
        return

    if data == "hub_stocks":
        update_user_profile(user_id, {"last_desk": "stocks"})
        profile = get_user_profile(user_id)
        await query.edit_message_text(
            ui_header(profile, "stocks") + "\nCharts + Tradeify only. Sol tools are on the other desk.",
            parse_mode="Markdown",
            reply_markup=hub_stocks_keyboard(profile),
        )
        return

    if data == "hub_crypto":
        update_user_profile(user_id, {"last_desk": "crypto"})
        profile = get_user_profile(user_id)
        await query.edit_message_text(
            await crypto_header_with_balance(profile)
            + "\nSol memes only. Stocks/futures are on the other desk.",
            parse_mode="Markdown",
            reply_markup=hub_crypto_keyboard(profile),
        )
        return

    if data == "crypto_risk_positions":
        try:
            from solana_execution import status
            state = status()
        except Exception as exc:
            log.exception("Could not read Solana risk state")
            await query.edit_message_text(
                "Risk status is unavailable. No trade was placed. "
                "Check the bot logs before continuing.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ Sol desk", callback_data="hub_crypto")]
                ]),
            )
            return

        positions = state.get("positions", {})
        lines = [
            "SOLANA RISK & POSITIONS",
            "",
            "Mode: MONITOR ONLY",
            "Live trading: DISABLED",
            "Signing/submission: DISABLED",
            f"UTC day: {state.get('day_utc', 'unknown')}",
            f"Realized daily PnL: {state.get('daily_realized_pnl_sol', 'unknown')} SOL",
            f"Engine-tracked positions: {state.get('open_positions', 0)}",
            "",
            "Positions:",
        ]

        if positions:
            for mint, pos in list(positions.items())[:5]:
                lines.extend([
                    f"- Mint: {mint}",
                    f"  Entry: {pos.get('entry_sol', '?')} SOL",
                    f"  Status: {pos.get('status', 'unknown')}",
                    f"  Stop: {pos.get('stop_price', '?')}",
                    f"  Target: {pos.get('target_price', '?')}",
                ])
        else:
            lines.append("No engine-tracked positions.")

        lines.extend([
            "",
            "Note: these are risk-engine records, not verified live wallet holdings.",
        ])

        await query.edit_message_text(
            "\\n".join(lines),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh", callback_data="crypto_risk_positions")],
                [InlineKeyboardButton("⬅️ Sol desk", callback_data="hub_crypto")],
            ]),
        )
        return

    if data == "hub_account":
        profile = get_user_profile(user_id)
        await query.edit_message_text(
            ui_header(profile, "home") + "\n**Account**",
            parse_mode="Markdown",
            reply_markup=hub_account_keyboard(profile),
        )
        return

    if data == "deposit_start":
        profile = get_user_profile(user_id)
        if not profile.get("sol_burner_key"):
            await query.edit_message_text(
                await crypto_header_with_balance(profile) + "\nImport burner key first:\n/wallet key <secret>",
                parse_mode="Markdown",
                reply_markup=hub_crypto_keyboard(profile),
            )
            return
        update_user_profile(user_id, {"awaiting_deposit": {"step": "to"}})
        default = profile.get("deposit_address")
        extra = ("\nLast: `" + default + "`") if default else ""
        await query.edit_message_text(
            await crypto_header_with_balance(profile) + "\n**Withdraw**\nReply with withdraw address (where SOL goes)."
            + extra
            + "\n\nOr /withdraw <address> <amount>",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data="deposit_cancel")], [InlineKeyboardButton("⬅️ Sol desk", callback_data="hub_crypto")]]),
        )
        return


    if data.startswith("dep_amt_"):
        profile = get_user_profile(user_id)
        ad = profile.get("awaiting_deposit") or {}
        if ad.get("step") != "amount" or not ad.get("to"):
            await query.edit_message_text("Start withdraw again.", reply_markup=hub_crypto_keyboard(profile))
            return
        try:
            amount = float(data.replace("dep_amt_", ""))
        except ValueError:
            await query.answer("Bad amount")
            return
        secret = profile.get("sol_burner_key")
        to_addr = ad.get("to")
        if not secret:
            update_user_profile(user_id, {"awaiting_deposit": None})
            await query.edit_message_text("Burner key missing. /wallet key <secret>")
            return
        await query.edit_message_text(f"Sending {amount} SOL to `{to_addr}`…", parse_mode="Markdown")
        result = await asyncio.to_thread(sol_send_sol, secret, to_addr, amount)
        update_user_profile(user_id, {"awaiting_deposit": None})
        if result.get("ok"):
            update_user_profile(user_id, {"deposit_address": to_addr})
            await query.edit_message_text(
                f"**Sent** {amount} SOL\nTo: `{to_addr}`\nSig: `{result.get('sig')}`\n"
                f"https://solscan.io/tx/{result.get('sig')}",
                parse_mode="Markdown",
                reply_markup=hub_crypto_keyboard(get_user_profile(user_id)),
                disable_web_page_preview=True,
            )
        else:
            await query.edit_message_text(
                f"Send failed: {result.get('error')}",
                reply_markup=hub_crypto_keyboard(get_user_profile(user_id)),
            )
        return

    if data == "deposit_cancel":
        update_user_profile(user_id, {"awaiting_deposit": None})
        profile = get_user_profile(user_id)
        await query.edit_message_text(
            await crypto_header_with_balance(profile) + "\nWithdraw cancelled.",
            parse_mode="Markdown",
            reply_markup=hub_crypto_keyboard(profile),
        )
        return

    if data == "wallet_info":

        profile = get_user_profile(user_id)
        w = profile.get("sol_wallet")
        has_key = bool(profile.get("sol_burner_key"))
        if not w and not has_key:
            msg = await crypto_header_with_balance(profile) + "\nNo wallet.\n/wallet <pubkey> or /wallet key <burner_secret>"
        else:
            bal = await asyncio.to_thread(sol_rpc_balance, w) if w else None
            bal_s = f"{bal:.6f} SOL" if bal is not None else "n/a"
            msg = (
                ui_header(profile, "crypto")
                + "\nAddress: `" + str(w or "—") + "`\n"
                + "Key: " + ("BURNER loaded" if has_key else "watch only")
                + "\nBalance: " + bal_s
                + "\nhttps://solscan.io/account/" + str(w or "")
            )
        await query.edit_message_text(
            msg,
            parse_mode="Markdown",
            reply_markup=hub_crypto_settings_keyboard(profile),
            disable_web_page_preview=True,
        )
        return

    if data == "toggle_norug":
        profile = get_user_profile(user_id)
        new_v = not profile.get("no_rug_mode", True)
        update_user_profile(user_id, {"no_rug_mode": new_v})
        profile = get_user_profile(user_id)
        await query.edit_message_text(
            await crypto_header_with_balance(profile) + "\nno_rug now " + ("ON" if new_v else "OFF"),
            parse_mode="Markdown",
            reply_markup=hub_crypto_settings_keyboard(profile),
        )
        return

    if data == "toggle_antimev":
        profile = get_user_profile(user_id)
        new_v = not profile.get("anti_mev", True)
        update_user_profile(user_id, {"anti_mev": new_v})
        profile = get_user_profile(user_id)
        await query.edit_message_text(
            await crypto_header_with_balance(profile) + "\nanti_mev now " + ("ON" if new_v else "OFF"),
            parse_mode="Markdown",
            reply_markup=hub_crypto_settings_keyboard(profile),
        )
        return


    if data == "toggle_x_discovery":
        profile = get_user_profile(user_id)
        new_v = not profile.get("x_discovery_enabled", False)
        update_user_profile(user_id, {"x_discovery_enabled": new_v})
        profile = get_user_profile(user_id)
        await query.edit_message_text(
            await crypto_header_with_balance(profile)
            + "\nX Discovery preference: " + ("ON" if new_v else "OFF")
            + "\nNote: this switch saves your preference; automatic post scanning "
              "requires an X data source. Use Search X for live public searches.",
            parse_mode="Markdown",
            reply_markup=hub_crypto_settings_keyboard(profile),
            disable_web_page_preview=True,
        )
        return

    if data == "x_discovery_search":
        profile = get_user_profile(user_id)
        if not profile.get("x_discovery_enabled", False):
            await query.edit_message_text(
                await crypto_header_with_balance(profile)
                + "\nX Discovery is OFF. Turn it on in the SOL desk first, "
                  "or use the public search links below.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Enable X Discovery", callback_data="toggle_x_discovery")],
                    [InlineKeyboardButton("⬅️ SOL desk", callback_data="hub_crypto")],
                ]),
            )
            return

        await query.edit_message_text(
            "𝕏 **Live Memecoin Search**\n\n"
            "These open X's live search results. They are search links, "
            "not automatically scanned or verified token alerts.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "Solana memecoins",
                    url="https://x.com/search?q=Solana%20memecoin&src=typed_query&f=live"
                )],
                [InlineKeyboardButton(
                    "New Solana launches",
                    url="https://x.com/search?q=Solana%20token%20launch&src=typed_query&f=live"
                )],
                [InlineKeyboardButton(
                    "Token CA mentions",
                    url="https://x.com/search?q=Solana%20CA%20memecoin&src=typed_query&f=live"
                )],
                [InlineKeyboardButton("⬅️ SOL desk", callback_data="hub_crypto")],
            ]),
            disable_web_page_preview=True,
        )
        return

    if data == "stocks_settings":
        await query.edit_message_text(
            "⚙️ **Stocks Desk Settings**\\nManage trading rules, custom LIVE rules, alerts, and session display timezone.",
            parse_mode="Markdown",
            reply_markup=stocks_settings_keyboard(),
        )
        return

    if data in ("timezone_menu", "stock_timezone_menu"):
        profile = get_user_profile(user_id)
        current_tz = profile.get("timezone") or "not set"
        await query.edit_message_text(
            f"🕒 **Choose your timezone**\\nCurrent: `{current_tz}`\\n\\n"
            "This changes your saved preference. Stock-market schedules remain "
            "on New York time.",
            parse_mode="Markdown",
            reply_markup=timezone_keyboard(),
        )
        return

    timezone_choices = {
        # Keep old buttons working.
        "tzpick_eastern": "America/New_York",
        "tzpick_central": "America/Chicago",
        "tzpick_mountain": "America/Denver",
        "tzpick_pacific": "America/Los_Angeles",
        "tzpick_alaska": "America/Anchorage",
        "tzpick_hawaii": "Pacific/Honolulu",
        "tzpick_utc": "UTC",
        # New Stocks Desk and /session set buttons.
        "stocktzpick_eastern": "America/New_York",
        "stocktzpick_central": "America/Chicago",
        "stocktzpick_mountain": "America/Denver",
        "stocktzpick_pacific": "America/Los_Angeles",
        "stocktzpick_alaska": "America/Anchorage",
        "stocktzpick_hawaii": "Pacific/Honolulu",
        "stocktzpick_utc": "UTC",
    }
    if data in timezone_choices:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        selected_tz = timezone_choices[data]
        try:
            ZoneInfo(selected_tz)
        except (ZoneInfoNotFoundError, ValueError):
            await query.edit_message_text("That timezone is unavailable. Please try again.")
            return

        old_tz = profile.get("timezone")
        update_user_profile(user_id, {"timezone": selected_tz})
        if old_tz:
            await query.edit_message_text(
                f"✅ **Timezone updated**\\nSaved: `{selected_tz}`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ Stocks Desk Settings", callback_data="stocks_settings")],
                    [InlineKeyboardButton("🏠 Home", callback_data="menu")],
                ]),
            )
        else:
            await query.edit_message_text(
                f"✅ **Timezone saved: `{selected_tz}`**\\n\\n"
                "You can change it later in Stocks Desk Settings or with /session set.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🏠 Home", callback_data="menu")],
                ]),
            )
        return

    if data == "crypto_settings":
        profile = get_user_profile(user_id)
        await query.edit_message_text(
            await crypto_header_with_balance(profile)
            + "\\n**Crypto Settings**\\nManage safety filters, wallet, X discovery, alerts, cashback, and incinerator links.",
            parse_mode="Markdown",
            reply_markup=hub_crypto_settings_keyboard(profile),
            disable_web_page_preview=True,
        )
        return

    if data == "pumpcash_menu":
        profile = get_user_profile(user_id)
        code = profile.get("pump_cashback_code") or "none"
        await query.edit_message_text(
            await crypto_header_with_balance(profile)
            + "\\n**Pump.fun Cashback**\\n"
              "Saved code: `" + str(code) + "`\\n\\n"
              "Use `/pumpcash YOURCODE` to save or update your referral code. "
              "Cashback depends on eligibility and the applicable program terms; "
              "this bot does not execute trades or guarantee cashback.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🌐 Open Pump.fun", url="https://pump.fun")],
                [InlineKeyboardButton("⬅️ Crypto Settings", callback_data="crypto_settings")],
            ]),
            disable_web_page_preview=True,
        )
        return

    if data == "incinerator_menu":
        profile = get_user_profile(user_id)
        w = profile.get("sol_wallet") or ""
        msg = (
            await crypto_header_with_balance(profile)
            + "\\n**Solana Incinerator**\\n"
              "External tools may close eligible empty token accounts and reclaim rent. "
              "Review every account before confirming; transactions can be irreversible."
        )
        if w:
            msg += "\\nWallet: `" + str(w) + "`"
        await query.edit_message_text(
            msg,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔥 Sol Incinerator", url="https://sol-incinerator.com")],
                [InlineKeyboardButton("🌐 Alternative site", url="https://www.solincinerator.com")],
                [InlineKeyboardButton("⬅️ Crypto Settings", callback_data="crypto_settings")],
            ]),
            disable_web_page_preview=True,
        )
        return

    if data == "switch_desk":
        profile = get_user_profile(user_id)
        last = profile.get("last_desk") or "stocks"
        if last == "crypto":
            update_user_profile(user_id, {"last_desk": "stocks"})
            profile = get_user_profile(user_id)
            await query.edit_message_text(
                ui_header(profile, "stocks") + "\nSwitched to STOCKS / FUTURES.",
                parse_mode="Markdown",
                reply_markup=hub_stocks_keyboard(profile),
            )
        else:
            update_user_profile(user_id, {"last_desk": "crypto"})
            profile = get_user_profile(user_id)
            await query.edit_message_text(
                await crypto_header_with_balance(profile) + "\nSwitched to SOL CRYPTO.",
                parse_mode="Markdown",
                reply_markup=hub_crypto_keyboard(profile),
            )
        return

    if data == "crypto_pnl":
        profile = get_user_profile(user_id)
        msg = crypto_pnl_text(profile, refresh=True)
        alerts = check_open_pnl_alerts(profile)
        update_user_profile(user_id, {"sol_positions": profile.get("sol_positions") or []})
        if alerts:
            msg = "\n".join(alerts) + "\n\n" + msg
        await query.edit_message_text(
            msg[:4000],
            parse_mode="Markdown",
            reply_markup=hub_crypto_keyboard(get_user_profile(user_id)),
            disable_web_page_preview=True,
        )
        return

    if data in ("crypto_alerts_menu", "calert_wins", "calert_losses", "calert_pnl") or data.startswith("calert_thr_"):
        profile = get_user_profile(user_id)
        ca = dict(profile.get("crypto_alerts") or {})
        if data == "calert_wins":
            ca["wins"] = not ca.get("wins", True)
            update_user_profile(user_id, {"crypto_alerts": ca})
        elif data == "calert_losses":
            ca["losses"] = not ca.get("losses", True)
            update_user_profile(user_id, {"crypto_alerts": ca})
        elif data == "calert_pnl":
            ca["pnl_moves"] = not ca.get("pnl_moves", True)
            update_user_profile(user_id, {"crypto_alerts": ca})
        elif data.startswith("calert_thr_"):
            try:
                ca["pnl_threshold_pct"] = int(data.split("_")[-1])
            except Exception:
                ca["pnl_threshold_pct"] = 20
            update_user_profile(user_id, {"crypto_alerts": ca})
        profile = get_user_profile(user_id)
        ca = profile.get("crypto_alerts") or {}
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Wins " + ("ON" if ca.get("wins", True) else "off"), callback_data="calert_wins")],
            [InlineKeyboardButton("Losses " + ("ON" if ca.get("losses", True) else "off"), callback_data="calert_losses")],
            [InlineKeyboardButton("PnL moves " + ("ON" if ca.get("pnl_moves", True) else "off"), callback_data="calert_pnl")],
            [InlineKeyboardButton("Thresh 10%", callback_data="calert_thr_10"),
             InlineKeyboardButton("20%", callback_data="calert_thr_20"),
             InlineKeyboardButton("50%", callback_data="calert_thr_50")],
            [InlineKeyboardButton("⬅️ Sol desk", callback_data="hub_crypto")],
        ])
        await query.edit_message_text(
            await crypto_header_with_balance(profile) + "\n**Crypto alerts**\n"
            "Wins/losses on close. PnL moves on open threshold.\n"
            f"wins={ca.get('wins', True)} losses={ca.get('losses', True)} "
            f"pnl_moves={ca.get('pnl_moves', True)} @ {ca.get('pnl_threshold_pct', 20)}%",
            parse_mode="Markdown",
            reply_markup=kb,
        )
        return


    if data == "switch_account":
        profile = get_user_profile(user_id)
        accounts = profile.get("accounts") or {}
        rows = []
        for key in sorted(accounts.keys()):
            mark = " (active)" if key == profile.get("active_account") else ""
            rows.append([
                InlineKeyboardButton("👤 " + key + mark, callback_data="acct_pick_" + key),
                InlineKeyboardButton("🗑️ Delete", callback_data="acct_del_pick_" + key),
            ])
        rows.append([InlineKeyboardButton("Add new size/plan", callback_data="acct_new")])
        rows.append([InlineKeyboardButton("🏠 Home", callback_data="menu")])
        await query.edit_message_text(
            ui_header(profile, "home") + "\n**Change Tradeify account**\n"
            "Tap an account to switch, use 🗑️ Delete to remove a saved profile, or add a new size/plan.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return

    if data.startswith("acct_del_pick_"):
        key = data.replace("acct_del_pick_", "", 1)
        profile = get_user_profile(user_id)
        accounts = profile.get("accounts") or {}
        if key not in accounts:
            await query.edit_message_text("Account not found. Nothing was deleted.", reply_markup=main_menu_keyboard(profile))
            return
        active = profile.get("active_account")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🗑️ Yes, delete account", callback_data="acct_del_confirm_" + key)],
            [InlineKeyboardButton("❌ Cancel", callback_data="acct_del_cancel")],
        ])
        active_warning = "\n\n⚠️ This is your ACTIVE account. Deleting it will leave no active account selected." if key == active else ""
        await query.edit_message_text(
            "⚠️ CONFIRM ACCOUNT DELETION\n\n"
            + "Account: `" + key + "`\n"
            + "This removes this account profile and its saved rule settings from Brazy Bot.\n"
            + "It does NOT close or delete an account at your brokerage or prop firm."
            + active_warning,
            parse_mode="Markdown",
            reply_markup=kb,
        )
        return

    if data.startswith("acct_del_confirm_"):
        key = data.replace("acct_del_confirm_", "", 1)
        profile = get_user_profile(user_id)
        accounts = dict(profile.get("accounts") or {})
        if key not in accounts:
            await query.edit_message_text("Account not found. Nothing was deleted.", reply_markup=main_menu_keyboard(profile))
            return
        accounts.pop(key, None)
        updates = {"accounts": accounts}
        if profile.get("active_account") == key:
            updates.update({
                "active_account": None,
                "account_phase": "eval",
                "account_balance_profit": 0,
                "best_day_profit": 0,
            })
        update_user_profile(user_id, updates)
        profile = get_user_profile(user_id)
        remaining = len(profile.get("accounts") or {})
        await query.edit_message_text(
            "✅ Deleted account profile: `" + key + "`\n"
            + f"Remaining saved accounts: {remaining}.\n"
            + ("No active account is selected. Add or select an account before using account-specific rules." if not profile.get("active_account") else "Active account: `" + str(profile.get("active_account")) + "`"),
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(profile),
        )
        return

    if data == "acct_del_cancel":
        profile = get_user_profile(user_id)
        accounts = profile.get("accounts") or {}
        rows = []
        for key in sorted(accounts.keys()):
            mark = " (active)" if key == profile.get("active_account") else ""
            rows.append([
                InlineKeyboardButton("👤 " + key + mark, callback_data="acct_pick_" + key),
                InlineKeyboardButton("🗑️ Delete", callback_data="acct_del_pick_" + key),
            ])
        rows.append([InlineKeyboardButton("➕ Add new size/plan", callback_data="acct_new")])
        rows.append([InlineKeyboardButton("🏠 Home", callback_data="menu")])
        await query.edit_message_text(
            ui_header(profile, "home") + "\nDeletion cancelled. No account was removed.\n**Manage Tradeify accounts**",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return

    if data == "acct_new":
        await safe_edit(query, "New account — Step 1 size:", account_size_keyboard())
        return

    if data.startswith("acct_pick_"):
        key = data.replace("acct_pick_", "", 1)
        profile = get_user_profile(user_id)
        accounts = profile.get("accounts") or {}
        if key not in accounts:
            await query.edit_message_text("Account not found.", reply_markup=main_menu_keyboard(profile))
            return
        current = profile.get("active_account")
        if key == current:
            await query.edit_message_text(
                ui_header(profile, "home") + "\nAlready active: `" + key + "`\nNo changes made.",
                parse_mode="Markdown",
                reply_markup=main_menu_keyboard(profile),
            )
            return
        confirm_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Yes, switch account", callback_data="acct_confirm_" + key)],
            [InlineKeyboardButton("❌ Cancel — keep current account", callback_data="acct_cancel")],
        ])
        await query.edit_message_text(
            ui_header(profile, "home") + "\n⚠️ **Confirm account switch**\n\n"
            + "Current account: `" + str(current or "None") + "`\n"
            + "New account: `" + key + "`\n\n"
            + "Your bot's active account profile and rule limits will change only after you confirm.\n"
            + "If this is your funded account, double-check the account name before continuing.",
            parse_mode="Markdown",
            reply_markup=confirm_kb,
        )
        return

    if data.startswith("acct_confirm_"):
        key = data.replace("acct_confirm_", "", 1)
        profile = get_user_profile(user_id)
        accounts = profile.get("accounts") or {}
        if key not in accounts:
            await query.edit_message_text("Account not found. No change made.", reply_markup=main_menu_keyboard(profile))
            return
        update_user_profile(user_id, {"active_account": key})
        profile = get_user_profile(user_id)
        await query.edit_message_text(
            ui_header(profile, "home") + "\n✅ Account switch confirmed.\nActive: `" + key + "`\nPick a desk:",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(profile),
        )
        return

    if data == "acct_cancel":
        profile = get_user_profile(user_id)
        accounts = profile.get("accounts") or {}
        rows = []
        for key in sorted(accounts.keys()):
            mark = " (active)" if key == profile.get("active_account") else ""
            rows.append([
                InlineKeyboardButton("👤 " + key + mark, callback_data="acct_pick_" + key),
                InlineKeyboardButton("🗑️ Delete", callback_data="acct_del_pick_" + key),
            ])
        rows.append([InlineKeyboardButton("Add new size/plan", callback_data="acct_new")])
        rows.append([InlineKeyboardButton("🏠 Home", callback_data="menu")])
        await query.edit_message_text(
            ui_header(profile, "home") + "\nCancelled. Current account unchanged.\n**Change Tradeify account**",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return

    if data == "show_rules":
        active = profile.get("active_account")
        if not active:
            await query.edit_message_text("No active account. /start first.")
            return
        acc = profile["accounts"][active]
        r = acc["rules"]
        text = (
            f"**{active} Rules**\n\n"
            f"Profit Target: {f'$'+str(r['profit_target']) if r['profit_target'] else 'N/A'}\n"
            f"Max Trailing DD: ${r['max_drawdown']:,}\n"
            f"Daily Loss Limit: {f'$'+str(r['daily_loss_limit']) if r['daily_loss_limit'] else 'None'}\n"
            f"Consistency Eval: {r['consistency_eval']*100 if r['consistency_eval'] else 'None'}%\n"
            f"Consistency Funded: {r['consistency_funded']*100 if r['consistency_funded'] else 'None'}%\n"
            f"Max Contracts: {r['max_minis']} minis / {r['max_micros']} micros\n"
            f"Flatten by: 4:59 PM ET\n"
            f"News trading: Allowed"
        )
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=main_menu_keyboard(profile))
        return

    if data == "toggle_strict":
        new_val = not profile.get("strict_mode", True)
        update_user_profile(user_id, {"strict_mode": new_val})
        profile = get_user_profile(user_id)
        await query.edit_message_text(
            f"Strict mode is now {'ON' if new_val else 'OFF'}",
            reply_markup=main_menu_keyboard(profile)
        )
        return

    if data == "status":
        active = profile.get("active_account") or "None"
        text = (
            f"**Status**\n"
            f"Active: `{active}`\n"
            f"Daily Bias: {profile.get('daily_bias') or 'Not set'}\n"
            f"Strict: {'ON' if profile.get('strict_mode') else 'OFF'}\n"
            f"Mode: {profile.get('trading_mode', 'live').upper()}\n"
            f"Risk: {profile.get('risk_percent', 0.5)}%\n"
            f"Open positions: {len(profile.get('open_positions', []))}\n"
            f"Journal entries: {len(profile.get('journal', []))}\n"
            f"Active alerts: {len(profile.get('alerts', []))}"
        )
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=main_menu_keyboard(profile))
        return

    if data == "analyze":
        await query.edit_message_text(
            "Send a clear chart screenshot.\n\n"
            "I will run the full TJR checklist and reply with bias, quality, "
            "BUY/SELL/WAIT, entry/SL/TP, and a hypothesis of outcome."
        )
        return

    if data == "journal":
        entries = profile.get("journal", [])[-5:]
        if not entries:
            await query.edit_message_text("Journal is empty.\nUse /log after a trade.", reply_markup=main_menu_keyboard(profile))
            return
        text = "**Last 5 Journal Entries**\n\n"
        for e in reversed(entries):
            text += f"• {e.get('date', '')} | {e.get('result', '')} | {e.get('setup', '')}\n"
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=main_menu_keyboard(profile))
        return

    if data == "weekly":
        entries = profile.get("journal", [])
        if not entries:
            await query.edit_message_text("No trades logged yet.", reply_markup=main_menu_keyboard(profile))
            return
        # Simple weekly summary
        wins = sum(1 for e in entries if str(e.get("result", "")).startswith("+"))
        total = len(entries)
        text = (
            f"**Weekly Summary**\n"
            f"Total logged trades: {total}\n"
            f"Wins: {wins}\n"
            f"Win rate: {wins/total*100:.1f}%\n\n"
            "Full detailed report coming in next version."
        )
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=main_menu_keyboard(profile))
        return

    if data == "alerts":
        alerts = profile.get("alerts", [])
        if not alerts:
            await query.edit_message_text(
                "No price alerts set.\n\n"
                "Use: /alert ES 5800.25\n"
                "or /alert NQ liquidity_high",
                reply_markup=main_menu_keyboard(profile)
            )
            return
        text = "**Active Alerts**\n" + "\n".join(f"• {a}" for a in alerts)
        await query.edit_message_text(text, reply_markup=main_menu_keyboard(profile))
        return

    if data == "mode_menu":
        cur = profile.get("trading_mode", "paper")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Paper", callback_data="mode_set_paper")],
            [InlineKeyboardButton("Live (own money)", callback_data="mode_set_live")],
            [InlineKeyboardButton("Funded (Tradeify)", callback_data="mode_set_funded")],
            [InlineKeyboardButton("Menu", callback_data="menu")],
        ])
        msg = (
            f"**Trading Mode** (current: `{cur}`)" + "\n\n"
            "• **Paper** — practice, no hard blocks" + "\n"
            "• **Live** — your own money, custom rules only" + "\n"
            "• **Funded** — Tradeify prop rules + max payout path" + "\n\n"
            "Set custom live rules: /customrule"
        )
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=kb)
        return

    if data.startswith("mode_set_"):
        new_mode = data.replace("mode_set_", "")
        if new_mode not in ("paper", "live", "funded"):
            await safe_edit(query, "Unknown mode button. Open /start and choose again.", reply_markup=main_menu_keyboard(get_user_profile(user_id)))
            return
        update_user_profile(user_id, {"trading_mode": new_mode})
        profile = get_user_profile(user_id)
        blurbs = {
            "paper": "**PAPER MODE**" + "\n" + "Practice. Full analysis. Rules advisory only.",
            "live": "**LIVE MODE**" + "\n" + "Own capital. No Tradeify. Set rules with /customrule.",
            "funded": "**FUNDED MODE**" + "\n" + "Tradeify rules on. Max payout path. Set account with /start.",
        }
        await query.edit_message_text(
            blurbs[new_mode],
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(profile),
        )
        return

    if data == "kolscan_btn":
        await query.edit_message_text("Loading KOLScan…")
        out = fetch_kolscan_leaderboard(12)
        await query.edit_message_text(out[:4000], parse_mode="Markdown", disable_web_page_preview=True)
        return

    if data == "fomo_btn":
        await query.edit_message_text(fetch_fomo_board_help(), parse_mode="Markdown", disable_web_page_preview=True)
        return

    if data == "crypto_snipe_start":
        context.user_data["brazy_crypto_input"] = {"kind": "snipe"}
        await query.edit_message_text(
            "🎯 **Snipe a contract**\n\nSend the coin's contract/pair address in your next message.\n\nSend `cancel` to stop.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✖️ Cancel", callback_data="crypto_input_cancel")], [InlineKeyboardButton("⬅️ Sol desk", callback_data="hub_crypto")]]),
        )
        return

    if data == "crypto_buy_preview_start":
        context.user_data["brazy_crypto_input"] = {"kind": "buy_preview"}
        await query.edit_message_text(
            "🧾 **Jupiter Buy Quote Preview**\n\n"
            "Send the token mint address followed by the SOL amount.\n"
            "Example: `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v 0.001`\n\n"
            "Preview only — no purchase will be made. Maximum: 0.01 SOL.\n"
            "Send `cancel` to stop.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✖️ Cancel", callback_data="crypto_input_cancel")],
                [InlineKeyboardButton("⬅️ Sol desk", callback_data="hub_crypto")],
            ]),
        )
        return

    if data == "crypto_copyadd_start":
        context.user_data["brazy_crypto_input"] = {"kind": "copyadd_wallet"}
        await query.edit_message_text(
            "➕ **Add a copy-trade wallet**\n\nSend the trader's public wallet address. I'll save it to your Copy list.\n\nSend `cancel` to stop.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✖️ Cancel", callback_data="crypto_input_cancel")], [InlineKeyboardButton("📋 Open Copy list", callback_data="copy_list_btn")]]),
        )
        return

    if data == "crypto_copyfind_sol":
        profile = get_user_profile(user_id)
        await query.edit_message_text("Building the Solana copy-trade discovery board…")
        try:
            out = await asyncio.to_thread(
                crypto_copyfind, "solana", risk_preset=effective_profile_risk(profile),
                no_rug=profile.get("no_rug_mode", True), anti_mev=profile.get("anti_mev", True),
            )
            await query.edit_message_text(
                out[:4000], parse_mode="Markdown", disable_web_page_preview=True,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🌐 Open KOLScan", callback_data="kolscan_btn"), InlineKeyboardButton("🔥 Open FOMO", callback_data="fomo_btn")],
                    [InlineKeyboardButton("➕ Add trader wallet", callback_data="crypto_copyadd_start")],
                    [InlineKeyboardButton("📋 Copy list", callback_data="copy_list_btn")],
                    [InlineKeyboardButton("⬅️ Sol desk", callback_data="hub_crypto")],
                ]),
            )
        except Exception as exc:
            log.exception("Copy-find button failed")
            await query.edit_message_text(f"Copy-trade discovery failed ({type(exc).__name__}). Try again later.", reply_markup=hub_crypto_keyboard(profile))
        return

    if data == "crypto_input_cancel":
        context.user_data.pop("brazy_crypto_input", None)
        await query.edit_message_text("Cancelled.", reply_markup=hub_crypto_keyboard(get_user_profile(user_id)))
        return

    if data == "copy_list_btn":
        profile = get_user_profile(user_id)
        traders = profile.get("copy_traders") or []
        if not traders:
            msg = "**Copy list is empty.**\n\nTap **Add wallet** to enter a public wallet address, or use /copyadd <wallet> <name>."
        else:
            msg = "**Copy list**\n\n" + "\n".join(
                f"• {t.get('label')}: `{t.get('wallet')}`" for t in traders
            )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Add wallet (/copyadd)", callback_data="crypto_copyadd_start")],
            [InlineKeyboardButton("🔎 Find copy-trade wallets", callback_data="crypto_copyfind_sol")],
            [InlineKeyboardButton("⬅️ Sol desk", callback_data="hub_crypto")],
        ])
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=kb, disable_web_page_preview=True)
        return

    if data in ("customrules_btn", "customrules_preset"):
        profile = get_user_profile(user_id)
        rules = dict(profile.get("live_stock_rules") or {})
        defaults = {
            "risk_percent": 0.5, "max_daily_loss_pct": 2.0, "max_trades_day": 3,
            "min_rr": 2.0, "session": "NY open 9:30-11:30 ET", "a_plus_only": True,
            "require_htf_bias": True, "require_liquidity_sweep": True,
            "require_displacement_bos": True, "prefer_fvg_entry": True,
            "stop_beyond_swing": True, "no_revenge_trading": True,
        }
        if data == "customrules_preset":
            rules = dict(defaults)
            rules.update({"risk_percent": 0.25, "max_daily_loss_pct": 1.0, "max_trades_day": 2, "min_rr": 2.0})
            update_user_profile(user_id, {"live_stock_rules": rules})
        else:
            for k, v in defaults.items():
                rules.setdefault(k, v)
        msg = (
            "**⚙️ Custom LIVE Stock/Futures Rules — TJR style**\n"
            "Own-money LIVE stocks/futures only. Separate from funded and Sol meme rules.\n\n"
            f"• Risk/trade: {rules['risk_percent']}%\n• Max daily loss: {rules['max_daily_loss_pct']}%\n"
            f"• Max trades/day: {rules['max_trades_day']}\n• Minimum R:R: {rules['min_rr']}:1\n"
            f"• Session: {rules['session']}\n\n"
            "Process: Daily/4H/1H bias → mark liquidity → wait for sweep → displacement/BOS → 15m entry confirmation/FVG if present → stop beyond invalidation → scale out at planned levels. A+ only; never force a trade.\n\n"
            "Edit with /customrules risk 0.5, /customrules daily 2, /customrules trades 3, /customrules rr 2, or /customrules session NY-open."
        )
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🛡️ Conservative preset", callback_data="customrules_preset")],
            [InlineKeyboardButton("🔄 Reset defaults", callback_data="customrules_reset")],
            [InlineKeyboardButton("⬅️ Stocks desk", callback_data="hub_stocks")],
        ]))
        return

    if data == "customrules_reset":
        profile = get_user_profile(user_id)
        rules = {
            "risk_percent": 0.5, "max_daily_loss_pct": 2.0, "max_trades_day": 3,
            "min_rr": 2.0, "session": "NY open 9:30-11:30 ET", "a_plus_only": True,
            "require_htf_bias": True, "require_liquidity_sweep": True,
            "require_displacement_bos": True, "prefer_fvg_entry": True,
            "stop_beyond_swing": True, "no_revenge_trading": True,
        }
        update_user_profile(user_id, {"live_stock_rules": rules})
        await query.edit_message_text("LIVE stock/futures rules reset. Sol meme and funded rules were not changed.", reply_markup=hub_stocks_keyboard(get_user_profile(user_id)))
        return

    if data == "sol_custom_rules":
        profile = get_user_profile(user_id)
        await query.edit_message_text(
            sol_custom_rules_text(profile),
            parse_mode="Markdown",
            reply_markup=sol_custom_rules_keyboard(),
        )
        return

    if data.startswith("sol_custom_edit_"):
        field = data[len("sol_custom_edit_"):]
        if field not in SOL_CUSTOM_FIELDS:
            await safe_edit(query, "Unknown custom SOL setting.")
            return
        context.user_data["brazy_sol_rule_edit"] = field
        label = SOL_CUSTOM_FIELDS[field][0]
        profile = get_user_profile(user_id)
        current = (profile.get("custom_sol_rules") or SOL_CUSTOM_DEFAULTS).get(field, SOL_CUSTOM_DEFAULTS[field])
        await query.edit_message_text(
            f"**Edit {label}**\\nCurrent value: `{current}`\\n\\n"
            "Send the new value as a message. Send /cancel to stop.",
            parse_mode="Markdown",
        )
        return

    if data == "sol_custom_use":
        profile = get_user_profile(user_id)
        rules = dict(SOL_CUSTOM_DEFAULTS)
        rules.update({k: v for k, v in (profile.get("custom_sol_rules") or {}).items()
                      if k in SOL_CUSTOM_DEFAULTS})
        update_user_profile(user_id, {"custom_sol_rules": rules, "risk_preset": "custom"})
        profile = get_user_profile(user_id)
        await query.edit_message_text(
            "Custom SOL Rules are now active.\\n\\n" + format_risk_preset(profile),
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(profile),
        )
        return

    if data == "sol_custom_reset":
        profile = get_user_profile(user_id)
        update_user_profile(user_id, {"custom_sol_rules": dict(SOL_CUSTOM_DEFAULTS)})
        profile = get_user_profile(user_id)
        await query.edit_message_text(
            "Custom values reset to Medium defaults. Your active preset was not changed.\\n\\n"
            + sol_custom_rules_text(profile),
            parse_mode="Markdown",
            reply_markup=sol_custom_rules_keyboard(),
        )
        return

    if data == "risk_preset_menu":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Chill", callback_data="risk_set_chill")],
            [InlineKeyboardButton("Medium", callback_data="risk_set_medium")],
            [InlineKeyboardButton("Degen / Risky", callback_data="risk_set_degen")],
            [InlineKeyboardButton("⚙️ Custom SOL Rules", callback_data="sol_custom_rules")],
            [InlineKeyboardButton("Menu", callback_data="menu")],
        ])
        profile = get_user_profile(user_id)
        await query.edit_message_text(
            format_risk_preset(profile) + "\n\nPick a preset:",
            parse_mode="Markdown",
            reply_markup=kb,
        )
        return

    if data.startswith("risk_set_"):
        choice = data.replace("risk_set_", "")
        if choice not in ("chill", "medium", "degen"):
            await safe_edit(query, "Unknown risk preset button. Open /start and choose again.", reply_markup=main_menu_keyboard(get_user_profile(user_id)))
            return
        update_user_profile(user_id, {"risk_preset": choice})
        profile = get_user_profile(user_id)
        await query.edit_message_text(
            "Saved.\n\n" + format_risk_preset(profile),
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(profile),
        )
        return

    if data == "pos_menu":
        profile = get_user_profile(user_id)
        msg = list_positions_text(profile, refresh=True)
        # save any price updates
        update_user_profile(user_id, {"sol_positions": profile.get("sol_positions") or []})
        await query.edit_message_text(
            msg[:4000],
            parse_mode="Markdown",
            reply_markup=positions_keyboard(get_user_profile(user_id)),
            disable_web_page_preview=True,
        )
        return

    if data == "pos_refresh":
        profile = get_user_profile(user_id)
        msg = list_positions_text(profile, refresh=True)
        update_user_profile(user_id, {"sol_positions": profile.get("sol_positions") or []})
        await query.edit_message_text(
            msg[:4000],
            parse_mode="Markdown",
            reply_markup=positions_keyboard(get_user_profile(user_id)),
            disable_web_page_preview=True,
        )
        return

    if data == "pos_close_all":
        profile = get_user_profile(user_id)
        # refresh prices first for fair PnL
        list_positions_text(profile, refresh=True)
        positions = profile.get("sol_positions") or []
        alerts = []
        for p in positions:
            if p.get("status", "open") == "open":
                alerts.append(record_crypto_close(profile, p))
                p["status"] = "closed"
                p["closed_at"] = datetime.utcnow().isoformat()
        update_user_profile(user_id, {
            "sol_positions": positions,
            "crypto_closed": profile.get("crypto_closed") or [],
        })
        await query.edit_message_text(
            "**Closed all positions** (tracked in bot).\n"
            "To exit on-chain, sell on Jupiter / pump.fun / your wallet UI.\n"
            "https://jup.ag\n\n" + list_positions_text(get_user_profile(user_id), refresh=False),
            parse_mode="Markdown",
            reply_markup=positions_keyboard(get_user_profile(user_id)),
            disable_web_page_preview=True,
        )
        return

    if data.startswith("pos_close_"):
        pid = data.replace("pos_close_", "")
        profile = get_user_profile(user_id)
        list_positions_text(profile, refresh=True)
        positions = profile.get("sol_positions") or []
        closed_sym = pid
        alert = ""
        for p in positions:
            if str(p.get("id")) == pid and p.get("status", "open") == "open":
                alert = record_crypto_close(profile, p)
                p["status"] = "closed"
                p["closed_at"] = datetime.utcnow().isoformat()
                closed_sym = p.get("symbol") or pid
        update_user_profile(user_id, {
            "sol_positions": positions,
            "crypto_closed": profile.get("crypto_closed") or [],
        })
        msg_close = f"**Closed {closed_sym}** (bot tracker).\n"
        if alert:
            msg_close += alert + "\n"
        msg_close += "Sell on-chain via Jupiter if still holding:\nhttps://jup.ag\n\n"
        msg_close += list_positions_text(get_user_profile(user_id), refresh=True)[:3500]
        await query.edit_message_text(
            msg_close,
            parse_mode="Markdown",
            reply_markup=positions_keyboard(get_user_profile(user_id)),
            disable_web_page_preview=True,
        )
        return

    if data == "crypto_scan":
        profile = get_user_profile(user_id)
        await query.edit_message_text("Scanning memecoins under your risk preset…")
        out = await asyncio.to_thread(
            crypto_scan_memecoins, "solana", limit=8, risk_preset=effective_profile_risk(profile),
            no_rug=profile.get("no_rug_mode", True), anti_mev=profile.get("anti_mev", True),
        )
        await query.edit_message_text(out[:4000], parse_mode="Markdown", disable_web_page_preview=True)
        return

    if data == "crypto_snipe_help":
        await query.edit_message_text(
            "**Snipe / Copy**\n\n"
            "• `/cryptoscan sol` or `eth` or `base` — A/A+ memecoin scan\n"
            "• `/snipe <contract>` — full snipe brief\n"
            "• paste a contract alone — auto snipe brief\n"
            "• `/copyfind sol` — wallets/copy workflow + scan seeds\n"
            "• `/asset crypto` — lock chart analysis to crypto\n",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(get_user_profile(user_id)),
        )
        return

    if data == "asset_info":
        ap = profile.get("asset_profile", "auto")
        await query.edit_message_text(
            f"**Adaptive Asset Profile**\n\nCurrent: `{ap}`\n\n"
            "Change with:\n"
            "/asset auto\n/asset futures\n/asset crypto\n/asset forex\n/asset stocks\n\n"
            "This switches how charts are read (sessions, volatility, VWAP, liquidity).",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(profile)
        )
        return

    if data == "session_info":
        stock_tz = profile.get("timezone")
        info = get_current_session_info(stock_tz)
        bias = profile.get("daily_bias") or "Not set"
        strict = "ON" if profile.get("strict_mode") else "OFF"
        kill = f"**Killzone:** {info['killzone']}\n" if info.get("killzone") else ""
        trade_status = "Trading allowed" if info["allow_trading"] else "NO NEW TRADES — Flatten or stay flat"
        text = f"""**Session Intelligence**\n\n**Current Time:** {info['time_et']}\n**Active Session:** {info['session']}\n**Status:** {info['status']}\n{kill}\n**What you should do right now:**\n{info['action']}\n\n**Next:** {info['next']}\n\n**Trading Status:** {trade_status}\n\n**Your settings:** Bias = {bias} | Strict = {strict}"""
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=main_menu_keyboard(profile))
        return

    if data in {"opp_scan_stocks", "opp_scan_futures", "opp_scan_crypto", "opp_scan_all"}:
        category = data.removeprefix("opp_scan_")
        await query.edit_message_text(
            f"Scanning {category.upper()} for strong 15-minute setups…\n\n"
            "This may take a moment. Market data may be delayed.",
            reply_markup=strong_opportunity_scanner_keyboard(profile),
        )
        try:
            result = await asyncio.to_thread(scan_market_opportunities, category)
            chunks = [result[i:i+3500] for i in range(0, len(result), 3500)] or ["No scanner results."]
            await query.edit_message_text(
                chunks[0],
                reply_markup=strong_opportunity_scanner_keyboard(profile),
                disable_web_page_preview=True,
            )
            for chunk in chunks[1:]:
                await query.message.reply_text(chunk, disable_web_page_preview=True)
        except Exception as exc:
            log.exception("Strong Opportunity scanner button failed")
            await query.message.reply_text(
                f"Scanner failed ({type(exc).__name__}). Try again later.",
                reply_markup=strong_opportunity_scanner_keyboard(profile),
            )
        return

    if data == "strong_opportunity":
        await query.edit_message_text("Generating a strong opportunity idea... one moment.")
        await generate_strong_opportunity(query, profile)
        return

    # Never leave an unknown/stale button tap with no visible response.
    log.warning("Unhandled callback button: %r from user %s", data, user_id)
    await safe_edit(
        query,
        "That button action isn't available in this version. Please open /start again.",
        reply_markup=main_menu_keyboard(profile),
    )
    return


def strong_opportunity_scanner_keyboard(profile: Dict) -> InlineKeyboardMarkup:
    """Quick market-scan buttons shown inside the Strong Opportunity result."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 Scan Stocks", callback_data="opp_scan_stocks"),
         InlineKeyboardButton("📈 Scan Futures", callback_data="opp_scan_futures")],
        [InlineKeyboardButton("🪙 Scan Crypto", callback_data="opp_scan_crypto"),
         InlineKeyboardButton("🌐 Scan All Markets", callback_data="opp_scan_all")],
        [InlineKeyboardButton("🔄 New Strong Setup", callback_data="strong_opportunity"),
         InlineKeyboardButton("🏠 Home", callback_data="menu")],
    ])


async def generate_strong_opportunity(query, profile: Dict):
    """Generate a random strong opportunity with full approach explanation"""
    import random

    opportunities = [
        {
            "name": "Liquidity Sweep + BOS Reversal",
            "desc": "Price sweeps equal highs or equal lows, then immediately breaks structure in the opposite direction.",
            "approach": "Wait for the sweep to happen. Do not enter during the sweep. After the sweep, look for a clear Break of Structure (close beyond the recent swing). Then wait for price to return to the first Fair Value Gap or Order Block created by the displacement. Enter there.",
            "timing": "Best in the first 1-2 hours of New York or during high-volume sessions. After the BOS you can wait 1-3 minutes for the retrace into the FVG/OB.",
            "strategy": "Classic TJR / ICT model. Highest probability when it aligns with daily bias and happens at a higher-timeframe level.",
            "extra": "Invalidation is a close back beyond the swept level. Risk small. This is one of the cleanest A+ setups."
        },
        {
            "name": "Order Block Retest in Discount/Premium",
            "desc": "Strong displacement leaves an Order Block. Price later returns to it while still in discount (for buys) or premium (for sells).",
            "approach": "Identify the last opposing candle before the strong move. Mark the Order Block. Only consider entries when price returns to it AND the daily bias agrees AND price is on the correct side of equilibrium. Look for a small liquidity grab or rejection wick at the block.",
            "timing": "Can appear any time, but strongest when the original displacement happened during a high-volume window. After price taps the block, give it 1-2 minutes to show rejection before entering.",
            "strategy": "Pure Order Block + Equilibrium filter. Works extremely well on ES and NQ.",
            "extra": "If the Order Block gets cleanly broken with a close, it becomes a Breaker — do not trade it as demand/supply anymore."
        },
        {
            "name": "Fair Value Gap Fill + Continuation",
            "desc": "A strong candle leaves an imbalance (FVG). Price later fills part or all of the gap and then continues in the original direction.",
            "approach": "Mark the FVG. Wait for price to trade back into the gap. Look for rejection or a small BOS inside the gap. Enter in the direction of the original displacement. Best when the FVG sits in discount (buys) or premium (sells).",
            "timing": "Very common after New York open and during the London-New York overlap. After price enters the gap, wait 30-90 seconds for reaction before committing.",
            "strategy": "ICT Fair Value Gap continuation. Simple and high frequency when the market is trending.",
            "extra": "Not every gap will hold. Only take the ones that align with higher-timeframe bias and have at least one other confluence."
        },
        {
            "name": "Equilibrium Reaction (50% Retracement)",
            "desc": "Price reaches the 50% level of a clear dealing range (swing high to swing low) and reacts.",
            "approach": "Draw the dealing range. Mark the exact 50% line (equilibrium). When price reaches it, look for confluence: Order Block, FVG, or liquidity just beyond it. Enter only if daily bias agrees and you see rejection.",
            "timing": "Works throughout the day. Give price a minute or two at the level to show whether it respects it or slices through.",
            "strategy": "Premium/Discount filter. Keeps you from buying high and selling low.",
            "extra": "If price blasts through equilibrium with strong momentum, the range may be expanding — step aside."
        },
        {
            "name": "Equal Highs/Lows Trap (Turtle Soup style)",
            "desc": "Obvious equal highs or equal lows get raided, trapping breakout traders, then price reverses hard.",
            "approach": "Identify clean equal highs or lows. Wait for the raid (liquidity grab). Do not chase the breakout. After the grab, look for immediate displacement + BOS in the opposite direction. Enter on the first pullback into the new FVG or Order Block.",
            "timing": "Often happens around session opens or just before major news. After the trap, the reversal can be fast — be ready within 1-2 minutes.",
            "strategy": "Liquidity-based reversal. One of the highest R-multiple setups when clean.",
            "extra": "The more obvious the equal highs/lows are, the more stops are sitting there, and the better the potential reversal."
        },
        {
            "name": "Breaker Block Reversal",
            "desc": "A previous Order Block fails (gets broken), then price retests it from the other side and continues.",
            "approach": "When an Order Block is clearly broken with a close, mark it as a Breaker. Wait for price to come back and retest the broken level from the opposite side. Look for rejection. Enter in the new direction.",
            "timing": "Usually appears after a strong reversal day or session. Give the retest 1-2 minutes to show strength before entering.",
            "strategy": "ICT Breaker Block. Advanced but very high probability when the original block was obvious.",
            "extra": "Breakers work best when the break also swept liquidity. Confluence makes it much stronger."
        },
        {
            "name": "New York Open Killzone Sweep",
            "desc": "During the New York open window, price raids Asian or London session highs/lows then reverses.",
            "approach": "Mark the Asian range high and low. During the first 30-60 minutes of New York, wait for a clear sweep of one side. After the sweep, look for BOS and entry on the first FVG or Order Block.",
            "timing": "Strictly 9:30–11:00 AM ET is the highest probability window. Act within 1-3 minutes after the sweep + BOS.",
            "strategy": "Session-based liquidity raid. Very popular in ICT and TJR style trading.",
            "extra": "Avoid forcing trades if the sweep is messy or news is about to drop."
        },
        {
            "name": "Higher Timeframe POI + Lower Timeframe Entry",
            "desc": "Price reaches a clear higher-timeframe Point of Interest (Order Block, FVG, or liquidity), then you drop to lower timeframe for precise entry.",
            "approach": "On 15m or 1H mark the strong POI. When price arrives there, switch to 1m or 5m. Wait for a liquidity sweep + BOS on the lower timeframe in the direction of your higher-timeframe bias. Enter on the lower timeframe FVG/OB.",
            "timing": "Patience is key. Price can sit at the higher-timeframe level for several minutes. Do not enter until lower timeframe confirms.",
            "strategy": "Multi-timeframe confluence. One of the most reliable ways to get tight stops and high R.",
            "extra": "If lower timeframe shows no clear BOS, simply skip. Higher timeframe level alone is not enough."
        },
        {
            "name": "Volume Imbalance / Inefficiency Fill",
            "desc": "A rapid move leaves a volume imbalance (very thin volume area). Price often returns to rebalance it.",
            "approach": "Identify the thin volume or imbalance zone. Wait for price to return into it. Look for reaction (rejection wick or small structure shift). Enter in the direction of the larger trend or daily bias.",
            "timing": "Can happen any session. After price enters the imbalance, wait 30-90 seconds for reaction.",
            "strategy": "Orderflow / volume profile concept mixed with ICT imbalance ideas.",
            "extra": "Works especially well when the imbalance sits at a higher-timeframe level."
        },
        {
            "name": "SMT Divergence Reversal",
            "desc": "Two correlated markets (example ES vs NQ) diverge at highs or lows — one makes a new extreme while the other does not.",
            "approach": "Watch ES and NQ (or other correlated pairs) at the same time. When one makes a higher high / lower low and the other fails to confirm, prepare for reversal. Enter on the weaker market after a BOS.",
            "timing": "Most useful around session highs/lows and major turning points. Confirmation usually appears within 1-5 minutes of the divergence.",
            "strategy": "Smart Money Technique (SMT). Advanced ICT concept that shows institutional divergence.",
            "extra": "Best used as confluence, not as a standalone signal. Combine with liquidity and structure."
        },
        {
            "name": "Opening Range Break + Retest",
            "desc": "The first 15-30 minute range of New York is broken, then price retests the range boundary and continues.",
            "approach": "Mark the high and low of the first 15 or 30 minutes after 9:30 AM ET. Wait for a clean break. Do not chase the break. Wait for the retest of the broken level and look for rejection or small FVG. Enter in the break direction.",
            "timing": "Only valid in the first 1-2 hours of New York. After the retest, decide within 1-2 minutes.",
            "strategy": "Classic Opening Range Breakout filtered with ICT-style confirmation.",
            "extra": "Fails often on high-impact news days. Size down or skip when major data is releasing."
        },
        {
            "name": "Silver Bullet Style Window",
            "desc": "Specific high-probability time windows (example 10:00–11:00 AM ET) where algorithmic price delivery is strong.",
            "approach": "During the chosen window, look only for liquidity sweeps + BOS + FVG/OB entries. Ignore setups outside the window. Treat the window as a focused hunting period.",
            "timing": "Stay disciplined to the window. Most of the edge comes from the time filter itself.",
            "strategy": "Time-based ICT model (Silver Bullet style). Combines time + liquidity + structure.",
            "extra": "You do not need many trades. One clean setup inside the window is enough."
        },
        {
            "name": "Propulsion Block Continuation",
            "desc": "After a strong Order Block reaction, a smaller consolidation (propulsion) forms and then breaks in the same direction.",
            "approach": "After price reacts from an Order Block, mark the small consolidation that forms. Wait for the break of that small range in the original direction. Enter on the break or the first small retest.",
            "timing": "Usually develops 5-20 minutes after the initial Order Block reaction. Act quickly once the small range breaks.",
            "strategy": "Advanced Order Block continuation concept.",
            "extra": "Very clean when the original Order Block was at a higher-timeframe level."
        },
        {
            "name": "Unfinished Business / Void Fill",
            "desc": "Price left a clear inefficiency or void on a higher timeframe and later returns to complete it.",
            "approach": "On higher timeframe identify obvious voids or unfinished moves. When price returns to that area on lower timeframe, look for standard entry tools (sweep + BOS + FVG). Trade the completion of the void.",
            "timing": "These can take hours or days to fill. Once price arrives, treat it like any other high-quality POI.",
            "strategy": "Higher-timeframe inefficiency trading mixed with lower-timeframe precision.",
            "extra": "Patience is required. Do not force entries before price actually reaches the void."
        },
        {
            "name": "Judas Swing Reversal",
            "desc": "Early session move goes one direction (false move), traps traders, then reverses hard for the real move of the day.",
            "approach": "In the first 30-60 minutes, if price makes a strong move that sweeps liquidity and then quickly reverses with BOS, treat the first move as the Judas Swing. Enter the reversal on the first FVG or Order Block.",
            "timing": "Almost exclusive to the opening hour of New York (and sometimes London). Decision window is short — 1-3 minutes after the reversal BOS.",
            "strategy": "Classic ICT Judas Swing. Extremely powerful when it aligns with higher-timeframe bias.",
            "extra": "Not every early move is a Judas Swing. Only take it when liquidity is clearly taken and structure shifts hard."
        }
    ]

    # Playbook weighting from debriefs: prefer "repeat", bury "delete"
    import re as _re
    debriefs = profile.get("debriefs", []) or []
    repeat_keys = []
    delete_keys = []
    for d in debriefs:
        pb = (d.get("playbook") or "").lower()
        setup = (d.get("setup") or "").lower()
        if "delete" in pb:
            delete_keys.append(setup)
        elif "repeat" in pb:
            repeat_keys.append(setup)

    def _is_deleted(opp_name, opp_strat):
        blob = (opp_name + " " + opp_strat).lower()
        for k in delete_keys:
            if k and len(k) > 2 and k in blob:
                return True
        return False

    def _is_repeat(opp_name, opp_strat):
        blob = (opp_name + " " + opp_strat).lower()
        for k in repeat_keys:
            if k and len(k) > 2 and k in blob:
                return True
        return False

    preferred = [o for o in opportunities if _is_repeat(o["name"], o["strategy"]) and not _is_deleted(o["name"], o["strategy"])]
    normal = [o for o in opportunities if not _is_deleted(o["name"], o["strategy"]) and o not in preferred]
    # All presented as A+ ideas only — pool = preferred heavy, then normal random
    if preferred and random.random() < 0.65:
        pick = random.choice(preferred)
        edge_tag = "From YOUR playbook (marked repeat)"
    elif normal:
        pick = random.choice(normal)
        edge_tag = "A+ library setup"
    else:
        pick = random.choice(opportunities)
        edge_tag = "A+ library setup"

    bias = profile.get("daily_bias") or "Not set"
    strict = "ON" if profile.get("strict_mode") else "OFF"
    pre = rule_breach_precheck(profile)
    gate = ""
    if not pre.get("ok"):
        gate = "**RULE GATE: conditions not clear for new risk right now**\n" + "\n".join(f"• {b}" for b in pre["blockers"]) + "\n\nStudy the idea below — do not force entry.\n\n"

    text = f"""{gate}**Strong Opportunity (A+ only)**
**Source:** {edge_tag}

**Name:** {pick['name']}

**What it looks like:**
{pick['desc']}

**How to approach it (step-by-step):**
{pick['approach']}

**Timing & Patience:**
{pick['timing']}

**Strategy behind it:**
{pick['strategy']}

**Extra knowledge you should know:**
{pick['extra']}

**Entry precision (when you see it live):**
• Trigger: wait for confirmation close in direction (BOS or reclaim of FVG/OB) — do not front-run the sweep
• Invalidation: close back beyond the swept level / broken structure
• Partials: scale at ~1R–1.5R if needed, runner toward next liquidity (Tradeify: steady progress > lottery TP)
• Skip-if: messy overlapping candles or news spike in the next 1-3 minutes

**Quality bar:** A+ only — needs liquidity + structure + zone + bias align. Else WAIT.

**Your current settings:**
• Daily Bias: {bias}
• Strict Mode: {strict}

Only take it if it appears clean on your chart and matches your bias.
Send a screenshot if you see this forming and I will analyze it with you.
"""

    try:
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=strong_opportunity_scanner_keyboard(profile))
    except Exception:
        await query.message.reply_text(text, parse_mode="Markdown", reply_markup=strong_opportunity_scanner_keyboard(profile))



async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    profile = get_user_profile(user_id)

    if not profile.get("active_account"):
        await update.message.reply_text(
            "No active Tradeify account is selected, so I can analyze the chart but cannot apply account-specific limits. "
            "Set one with /account → Add new size/plan if you need those checks."
        )

    await update.message.chat.send_action(ChatAction.TYPING)

    try:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        image_bytes = await file.download_as_bytearray()

        caption = update.message.caption or ""
        analysis = await analyze_chart_with_ai(bytes(image_bytes), profile, caption)

        # Tip if no ticker for live MTF
        tip = ""
        if caption and not _extract_symbol_hint(caption, profile):
            tip = "\n\nTip: caption the ticker next time (e.g. NQ or BTC) so I can load 1m/15m/1h/4h live candles for full MTF."
        elif not caption:
            tip = "\n\nTip: add a caption with the ticker (e.g. ES, NQ, BTC, AAPL) for live multi-timeframe candles + full trade setup."

        # Telegram rejects messages over 4096 characters. Chart analysis can be
        # lengthy, so send it in safe-sized chunks instead of failing the whole reply.
        full_reply = str(analysis) + tip
        max_chars = 3500
        chunks = []
        remaining = full_reply.strip()
        while remaining:
            if len(remaining) <= max_chars:
                chunks.append(remaining)
                break
            split_at = remaining.rfind("\n\n", 0, max_chars)
            if split_at < max_chars // 2:
                split_at = remaining.rfind("\n", 0, max_chars)
            if split_at < max_chars // 2:
                split_at = remaining.rfind(" ", 0, max_chars)
            if split_at < max_chars // 2:
                split_at = max_chars
            chunks.append(remaining[:split_at].strip())
            remaining = remaining[split_at:].strip()
        for chunk in chunks:
            try:
                await update.message.reply_text(chunk, parse_mode="Markdown")
            except Exception:
                # A chunk may cut through Markdown formatting; plain text is safer.
                await update.message.reply_text(chunk)
    except Exception as exc:
        log.exception("Chart photo analysis failed for user %s", user_id)
        await safe_reply(update, "I received the chart, but analysis failed this time. Check the bot logs/API configuration and try again. Error: " + str(exc)[:500])

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Voice note support — converts to text then processes"""
    user_id = update.effective_user.id
    profile = get_user_profile(user_id)

    await update.message.chat.send_action(ChatAction.TYPING)
    await update.message.reply_text(
        "Voice note received.\n"
        "Transcription + analysis will be added in the next update once Whisper is fully wired.\n"
        "For now please send text or chart screenshots."
    )

async def bias_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Usage: /bias bullish | bearish | neutral")
        return
    bias = context.args[0].lower()
    if bias not in ("bullish", "bearish", "neutral"):
        await update.message.reply_text("Use: bullish, bearish, or neutral")
        return
    update_user_profile(user_id, {"daily_bias": bias})
    await update.message.reply_text(f"Daily bias set to **{bias.upper()}**", parse_mode="Markdown")

async def log_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Quick journal log: /log +2R OrderBlock or /log -1R FVG"""
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Usage: /log +2R OrderBlock\nor /log -1R liquidity_sweep")
        return
    result = context.args[0]
    setup = " ".join(context.args[1:]) if len(context.args) > 1 else "unspecified"
    entry = {
        "date": datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
        "result": result,
        "setup": setup,
        "account": get_user_profile(user_id).get("active_account")
    }
    add_journal_entry(user_id, entry)
    await update.message.reply_text(f"Logged: {result} | {setup}")
    await start_debrief(update, user_id, {"result": result, "setup": setup, "source": "log"})

async def alert_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Usage: /alert ES 5800.5\nor /alert NQ equal_highs")
        return
    alert_text = " ".join(context.args)
    profile = get_user_profile(user_id)
    alerts = profile.get("alerts", [])
    alerts.append(alert_text)
    update_user_profile(user_id, {"alerts": alerts})
    await update.message.reply_text(f"Alert added: {alert_text}")

async def risk_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Usage: /risk 0.5")
        return
    try:
        risk = float(context.args[0])
        update_user_profile(user_id, {"risk_percent": risk})
        await update.message.reply_text(f"Risk per trade set to {risk}%")
    except ValueError:
        await update.message.reply_text("Please enter a number, example: /risk 0.5")

async def rules_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    profile = get_user_profile(user_id)
    active = profile.get("active_account")
    if not active:
        await update.message.reply_text("No active account. /start first.")
        return
    acc = profile["accounts"][active]
    r = acc["rules"]
    text = (
        f"**{active}**\n"
        f"Max DD: ${r['max_drawdown']:,}\n"
        f"Daily Loss: {r['daily_loss_limit'] or 'None'}\n"
        f"Max Contracts: {r['max_minis']} minis\n"
        f"Flatten by 4:59 PM ET"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Help + strategy questions + troubleshooting"""
    text = (
        "**Brazy Bot — Help**\n\n"
        "I am your private trading mentor and execution assistant.\n\n"
        "**What I can do:**\n"
        "• Analyze any chart screenshot with full TJR checklist\n"
        "• Give Strong Opportunity ideas with complete approach\n"
        "• Answer any strategy or concept question simply\n"
        "• Enforce your Tradeify rules and risk\n"
        "• Daily Market Open Report + Hourly Opportunities\n"
        "• Journal, alerts, bias, weekly summary\n\n"
        "**Key Commands:**\n"
        "/start — setup or menu\n"
        "/scan stocks|futures|crypto|all — scan for setups\n"
        "/bias bullish|bearish|neutral\n"
        "/checklist — pre-trade checklist\n"
        "/notes — TJR master notes\n"
        "/log +2R OrderBlock\n"
        "/alert ES 5800\n"
        "/risk 0.5\n"
        "/rules — your Tradeify rules\n/target profit 200 — set daily targets (per account)\n/sentiment — market sentiment\n/asset futures|crypto|forex|stocks — adaptive profile\n/phase eval|funded — account phase\n/progress — max payout path dashboard\n/debriefs — recent post-trade debriefs\n/mode paper|live|funded — trading mode\n/customrule — legacy live own-money rules\n/customrules — TJR-style live stock/futures rules (separate from funded/SOL)\n/performance — log trade performance\n/stats — performance stats\n"
        "/help — this message\n\n"
        "**Buttons:**\n"
        "Strong Opportunity • Analyze Chart • Status • Journal • Weekly\n\n"
        "Just type normal questions anytime:\n"
        "“what is a fair value gap?”\n"
        "“explain breaker blocks”\n"
        "“strong opportunity”\n"
        "“why did this fail?”\n\n"
        "Send charts. Ask anything. I am here for you."
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catch normal text questions about strategy, concepts, trouble, etc."""
    user_id = update.effective_user.id
    if not user_is_licensed(user_id):
        await update.message.reply_text(
            f"Not licensed. Your Telegram ID: `{user_id}`",
            parse_mode="Markdown",
        )
        return
    profile = get_user_profile(user_id)
    raw_text = (update.message.text or "").strip()
    text = raw_text.lower()

    pending_sol_rule = context.user_data.get("brazy_sol_rule_edit")
    if pending_sol_rule:
        if text in ("/cancel", "cancel", "stop"):
            context.user_data.pop("brazy_sol_rule_edit", None)
            await update.message.reply_text("Custom SOL edit cancelled.")
            return

        field = pending_sol_rule
        label, low, high = SOL_CUSTOM_FIELDS[field]
        try:
            if field == "min_grade":
                value = raw_text.strip().upper()
                if value not in ("A+", "A", "B"):
                    raise ValueError("Enter A+, A, or B.")
            else:
                number = float(raw_text.replace(",", "").replace("$", "").replace("%", "").strip())
                if not low <= number <= high:
                    raise ValueError(f"Value must be between {low} and {high}.")
                if field in ("max_positions", "min_liq_usd", "min_pair_age_min",
                             "copy_size_pct_of_their_buy"):
                    if not number.is_integer():
                        raise ValueError("This setting must be a whole number.")
                    value = int(number)
                else:
                    value = number
        except ValueError as exc:
            await update.message.reply_text(
                f"Invalid value for {label}: {exc}\\nPlease try again, or send /cancel."
            )
            return

        profile = get_user_profile(user_id)
        rules = dict(SOL_CUSTOM_DEFAULTS)
        rules.update({k: v for k, v in (profile.get("custom_sol_rules") or {}).items()
                      if k in SOL_CUSTOM_DEFAULTS})
        rules[field] = value
        update_user_profile(user_id, {"custom_sol_rules": rules})
        context.user_data.pop("brazy_sol_rule_edit", None)
        profile = get_user_profile(user_id)
        await update.message.reply_text(
            sol_custom_rules_text(profile),
            parse_mode="Markdown",
            reply_markup=sol_custom_rules_keyboard(),
        )
        return


    # Guided Snipe / Copy-add buttons. Consume the next user message as the requested input.
    pending_crypto = context.user_data.get("brazy_crypto_input")
    if pending_crypto:
        if text in {"cancel", "/cancel", "stop"}:
            context.user_data.pop("brazy_crypto_input", None)
            await update.message.reply_text("Cancelled.", reply_markup=hub_crypto_keyboard(profile))
            return
        kind = pending_crypto.get("kind")
        if kind == "buy_preview":
            parts = raw_text.split()
            if len(parts) != 2:
                await update.message.reply_text(
                    "Send exactly two values: token mint and SOL amount.\n"
                    "Example: `TOKEN_MINT 0.001`\n"
                    "Maximum: 0.01 SOL. Send `cancel` to stop.",
                    parse_mode="Markdown",
                )
                return

            mint, amount_text = parts
            try:
                amount_sol = float(amount_text)
            except ValueError:
                await update.message.reply_text(
                    "The SOL amount must be a number, for example `0.001`.",
                    parse_mode="Markdown",
                )
                return

            if not 0 < amount_sol <= 0.01:
                await update.message.reply_text(
                    "Amount must be greater than 0 and no more than 0.01 SOL."
                )
                return

            context.user_data.pop("brazy_crypto_input", None)
            await update.message.reply_text(
                "Fetching a Jupiter quote… No transaction will be signed or sent."
            )

            try:
                from jupiter_preview import quote_preview
                result = await asyncio.to_thread(quote_preview, mint, amount_sol)
                response = (
                    "🧾 **JUPITER BUY QUOTE — PREVIEW ONLY**\n\n"
                    f"**Input:** {result['input_sol']:.6f} SOL\n"
                    f"**Token mint:** `{result['mint']}`\n"
                    f"**Estimated output:** {result['output']:.8g} tokens\n"
                    f"**Minimum output:** {result['minimum']:.8g} tokens\n"
                    f"**Slippage:** {result['slippage_bps']} bps "
                    f"({result['slippage_bps'] / 100:.2f}%)\n"
                    f"**Price impact:** {result['impact_pct']:.4f}%\n"
                    f"**Route steps:** {result['routes']}\n\n"
                    "⚠️ This is an estimate, not an order. No wallet key was loaded; "
                    "no transaction was signed or sent."
                )
                await update.message.reply_text(
                    response,
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🧾 Another quote", callback_data="crypto_buy_preview_start")],
                        [InlineKeyboardButton("⬅️ Sol desk", callback_data="hub_crypto")],
                    ]),
                )
            except Exception:
                log.exception("Jupiter quote preview failed")
                await update.message.reply_text(
                    "Quote preview failed. No trade was submitted. "
                    "Check the bot logs for details, or return to the Sol desk.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🧾 Try again", callback_data="crypto_buy_preview_start")],
                        [InlineKeyboardButton("⬅️ Sol desk", callback_data="hub_crypto")],
                    ]),
                )
            return

        if kind == "snipe":
            address = raw_text.split()[0].strip()
            if not looks_like_contract(address):
                await update.message.reply_text("That doesn't look like a supported contract/pair address. Send the coin address, or `cancel`.", parse_mode="Markdown")
                return
            context.user_data.pop("brazy_crypto_input", None)
            await update.message.reply_text("Analyzing contract under your risk and safety presets…")
            out = await asyncio.to_thread(
                crypto_snipe_token, address, risk_preset=effective_profile_risk(profile),
                no_rug=profile.get("no_rug_mode", True), anti_mev=profile.get("anti_mev", True),
            )
            await update.message.reply_text(out[:4000], parse_mode="Markdown", disable_web_page_preview=True,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎯 Snipe another", callback_data="crypto_snipe_start")], [InlineKeyboardButton("⬅️ Sol desk", callback_data="hub_crypto")]]))
            return
        if kind == "copyadd_wallet":
            wallet = raw_text.strip()
            if len(wallet) < 20 or len(wallet) > 120 or any(ch.isspace() for ch in wallet):
                await update.message.reply_text("Please send a single public wallet address (no spaces), or `cancel`.", parse_mode="Markdown")
                return
            traders = list(profile.get("copy_traders") or [])
            if any(str(t.get("wallet", "")).lower() == wallet.lower() for t in traders):
                context.user_data.pop("brazy_crypto_input", None)
                await update.message.reply_text("That wallet is already in your Copy list.", reply_markup=hub_crypto_keyboard(profile))
                return
            label = wallet[:8] + "…"
            traders.append({"wallet": wallet, "label": label, "source": "manual"})
            update_user_profile(user_id, {"copy_traders": traders[-25:]})
            context.user_data.pop("brazy_crypto_input", None)
            await update.message.reply_text(
                f"Added **{label}** to your Copy list.\n`{wallet}`\n\nThis saves the wallet for tracking/discovery; it does not automatically copy trades or execute orders.\nhttps://kolscan.io/account/{wallet}",
                parse_mode="Markdown", disable_web_page_preview=True,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 View Copy list", callback_data="copy_list_btn")], [InlineKeyboardButton("🔎 Find more wallets", callback_data="crypto_copyfind_sol")], [InlineKeyboardButton("⬅️ Sol desk", callback_data="hub_crypto")]]),
            )
            return

    # Withdraw flow (burner send)
    if profile.get("awaiting_deposit"):
        ad = profile.get("awaiting_deposit") or {}
        step = ad.get("step")
        if raw_text.lower() in ("cancel", "/withdraw cancel", "/deposit cancel"):
            update_user_profile(user_id, {"awaiting_deposit": None})
            await update.message.reply_text("Withdraw cancelled.")
            return
        if step == "to":
            to_addr = raw_text.strip()
            if len(to_addr) < 32:
                await update.message.reply_text("Need a valid Sol address. Or /withdraw cancel")
                return
            update_user_profile(user_id, {"awaiting_deposit": {"step": "amount", "to": to_addr}})
            kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("0.1 SOL", callback_data="dep_amt_0.1"),
                    InlineKeyboardButton("0.5 SOL", callback_data="dep_amt_0.5"),
                    InlineKeyboardButton("1 SOL", callback_data="dep_amt_1"),
                ],
                [InlineKeyboardButton("Cancel", callback_data="deposit_cancel")],
            ])
            await update.message.reply_text(
                f"To: `{to_addr}`\nPick amount or type SOL amount (e.g. 0.25)",
                parse_mode="Markdown",
                reply_markup=kb,
            )
            return
        if step == "amount":
            try:
                amount = float(raw_text.strip().replace("sol", "").strip())
                if not __import__("math").isfinite(amount) or amount <= 0:
                    raise ValueError("amount must be finite and greater than zero")
            except (ValueError, TypeError):
                await update.message.reply_text("Send a finite amount greater than zero, e.g. 0.5 SOL")
                return
            to_addr = ad.get("to")
            secret = profile.get("sol_burner_key")
            if not secret:
                update_user_profile(user_id, {"awaiting_deposit": None})
                await update.message.reply_text("Burner key missing. /wallet key <secret>")
                return
            await update.message.reply_text(f"Sending {amount} SOL…")
            result = await asyncio.to_thread(sol_send_sol, secret, to_addr, amount)
            update_user_profile(user_id, {"awaiting_deposit": None})
            if result.get("ok"):
                update_user_profile(user_id, {"deposit_address": to_addr})
                await update.message.reply_text(
                    f"**Sent** {amount} SOL\nTo: `{to_addr}`\nSig: `{result.get('sig')}`\n"
                    f"https://solscan.io/tx/{result.get('sig')}",
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                )
            else:
                await update.message.reply_text(f"Send failed: {result.get('error')}")
            return

    # Auto snipe if user pasted a contract
    if looks_like_contract(raw_text):
        await update.message.reply_text("Contract detected — snipe brief under your risk preset…")
        out = await asyncio.to_thread(
            crypto_snipe_token, raw_text, risk_preset=effective_profile_risk(profile),
            no_rug=profile.get("no_rug_mode", True), anti_mev=profile.get("anti_mev", True),
        )
        await update.message.reply_text(out[:4000], parse_mode="Markdown", disable_web_page_preview=True)
        return

    # Post-trade debrief flow
    if profile.get("awaiting_debrief"):
        handled = await handle_debrief_flow(update, context, profile)
        if handled:
            return

    # Continue multi-step performance logging if active
    if profile.get("awaiting_performance"):
        handled = await handle_performance_flow(update, context, profile)
        if handled:
            return

    # Strong / random opportunity trigger
    if any(k in text for k in ["strong opportunity", "random opportunity", "give me an opportunity", "opportunity idea", "strong setup"]):
        await update.message.chat.send_action(ChatAction.TYPING)
        # Reuse the same generator via a fake query-like object
        class FakeQuery:
            def __init__(self, message):
                self.message = message
            async def edit_message_text(self, *args, **kwargs):
                await self.message.reply_text(args[0] if args else kwargs.get("text", ""), parse_mode=kwargs.get("parse_mode"), reply_markup=kwargs.get("reply_markup"))
        await generate_strong_opportunity(FakeQuery(update.message), profile)
        return

    # Answer ordinary text messages too; the previous keyword gate ignored most
    # natural-language questions and made the bot feel unresponsive.
    if raw_text:
        await update.message.chat.send_action(ChatAction.TYPING)

        if openai_client or anthropic_client:
            prompt = (
                "You are Axion’s patient trading mentor and assistant. "
                "Answer the user's actual message, even if it is phrased casually. "
                "Explain TJR / ICT and trading concepts in simple language, use short "
                "sentences and concrete examples, and ask a brief clarifying question "
                "when the request is ambiguous. Never claim certainty about market "
                "outcomes, and do not claim to have live market data unless a tool "
                "actually supplied it. For requests outside trading, respond helpfully "
                "or explain your scope briefly.\n\n"
                f"User message: {update.message.text}"
            )
            providers = []
            if anthropic_client:
                providers.append("anthropic")
            if openai_client:
                providers.append("openai")
            for provider in providers:
                try:
                    if provider == "anthropic":
                        msg = await asyncio.to_thread(
                            anthropic_client.messages.create,
                            model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929"),
                            max_tokens=700,
                            messages=[{"role": "user", "content": prompt}],
                        )
                        answer = msg.content[0].text if msg.content else ""
                    else:
                        resp = await asyncio.to_thread(
                            openai_client.chat.completions.create,
                            model=os.getenv("OPENAI_MODEL", "gpt-4o"),
                            messages=[{"role": "user", "content": prompt}],
                            max_tokens=700,
                        )
                        answer = resp.choices[0].message.content or ""
                    if answer.strip():
                        await update.message.reply_text(answer[:4000])
                        return
                    log.warning("AI provider %s returned an empty answer", provider)
                except Exception:
                    log.exception("AI text reply failed using %s; trying next configured provider", provider)

        await update.message.reply_text(
            "I received your message, but AI replies aren't available right now.\n"
            "Check that ANTHROPIC_API_KEY or OPENAI_API_KEY is set correctly in .env, "
            "then check the bot logs for the exact API error. You can still use /help "
            "and the menu buttons."
        )
        return



# ---------------------------------------------------------------------------
# Session Intelligence (strongest version)
# ---------------------------------------------------------------------------
def get_current_session_info(display_timezone=None):
    """Calculate sessions in ET; optionally format the displayed clock in a user timezone."""
    now = datetime.now(NY_TZ)
    hour = now.hour
    minute = now.minute
    t = hour + minute / 60.0
    weekday = now.weekday()  # 0=Mon ... 6=Sun

    if weekday >= 5:
        return {
            "session": "Weekend / Closed",
            "status": "Markets closed",
            "action": "Rest. Review journal. No trading.",
            "next": "Asia opens Sunday ~6:00 PM ET",
            "killzone": None,
            "allow_trading": False
        }

    # Futures almost 24h Sun-Fri. Key sessions in ET:
    # Asia: roughly 7:00 PM – 3:00 AM ET
    # London: 3:00 AM – 8:00 AM ET
    # New York: 8:00 AM – 5:00 PM ET (RTH 9:30-16:00)
    # Overlap London/NY: 8:00 AM – 12:00 PM ET

    if 19 <= hour or hour < 3:
        session = "Asia"
        status = "Asia session active"
        action = "Lower volume. Focus on range highs/lows and liquidity builds for later. Prefer smaller size or observation."
        next_s = "London begins around 3:00 AM ET"
        killzone = "Asia range formation"
        allow = True
    elif 3 <= hour < 8:
        session = "London"
        status = "London session active"
        action = "Good for sweeps of Asia range. Watch for Judas moves and early structure breaks."
        next_s = "New York open / Killzone at 9:30 AM ET"
        killzone = "London open & Asia sweep window"
        allow = True
    elif 8 <= hour < 9.5:
        session = "London / Pre-New York"
        status = "Pre-New York + London still active"
        action = "Prepare levels. Mark Asia + London highs/lows. Get ready for New York open."
        next_s = "New York RTH opens 9:30 AM ET"
        killzone = "Pre-NY positioning"
        allow = True
    elif 9.5 <= t < 11.5:
        session = "New York Killzone"
        status = "New York Open Killzone (highest probability window)"
        action = "Best time of day for A+ setups. Look for liquidity sweeps of prior session extremes + BOS + FVG/OB entries."
        next_s = "NY continues until 4:00 PM ET. Power Hour later."
        killzone = "NY Open Killzone (9:30–11:30 AM ET)"
        allow = True
    elif 11.5 <= t < 13.5:
        session = "New York Mid-Day"
        status = "NY mid-session (often slower)"
        action = "Lower edge. Only take clean A+ setups that align with daily bias. Avoid forcing."
        next_s = "Power Hour begins ~3:00 PM ET"
        killzone = None
        allow = True
    elif 13.5 <= t < 16.0:
        session = "New York Afternoon / Power Hour"
        status = "NY Afternoon + Power Hour"
        action = "Increased volume possible into close. Watch for continuation or reversal at key levels. Start preparing to flatten."
        next_s = "Hard flatten by 4:59 PM ET"
        killzone = "Power Hour"
        allow = True
    elif 16.0 <= t < 17.0:
        session = "Post-RTH / Flatten Window"
        status = "Market close approaching"
        action = "FLATTEN ALL POSITIONS NOW. Tradeify requires flat by 4:59 PM ET. No new trades."
        next_s = "Asia opens ~7:00 PM ET"
        killzone = None
        allow = False
    else:
        session = "Evening / Asia transition"
        status = "Transitioning toward Asia"
        action = "Light volume. Good time for review or marking levels for tomorrow."
        next_s = "Asia active overnight"
        killzone = None
        allow = True

    return {
        "session": session,
        "status": status,
        "action": action,
        "next": next_s,
        "killzone": killzone,
        "allow_trading": allow,
        "time_et": (
            now.astimezone(__import__("pytz").timezone(display_timezone)).strftime("%I:%M %p %Z").lstrip("0")
            if display_timezone else now.strftime("%H:%M ET")
        )
    }


async def session_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show session intelligence, or configure its display timezone with /session set."""
    profile = get_user_profile(update.effective_user.id)

    if context.args and context.args[0].lower() == "set":
        await update.message.reply_text(
            "🕒 Choose the timezone for your stock-session display. "
            "Market session calculations remain based on Eastern Time.",
            reply_markup=timezone_keyboard(),
        )
        return

    stock_tz = profile.get("timezone")
    info = get_current_session_info(stock_tz)
    bias = profile.get("daily_bias") or "Not set"
    strict = "ON" if profile.get("strict_mode") else "OFF"

    kill = f"**Killzone:** {info['killzone']}\n" if info.get("killzone") else ""
    trade_status = "Trading allowed" if info["allow_trading"] else "NO NEW TRADES — Flatten or stay flat"

    text = f"""**Session Intelligence**

**Current Time:** {info['time_et']}
**Active Session:** {info['session']}
**Status:** {info['status']}
{kill}
**What you should do right now:**
{info['action']}

**Next:** {info['next']}

**Trading Status:** {trade_status}

**Your settings:** Bias = {bias} | Strict = {strict}

Type /checklist before any entry.
"""
    await update.message.reply_text(text, parse_mode="Markdown")


async def send_session_transition_alert(app):
    """Alert when major session changes or flatten window hits"""
    info = get_current_session_info()
    profiles = load_profiles()
    if not profiles:
        return

    # Only send on important transitions
    important = ["New York Killzone", "Post-RTH / Flatten Window", "London", "New York Afternoon / Power Hour"]
    if info["session"] not in important and "Killzone" not in str(info.get("killzone")):
        return

    text = f"""**Session Change Alert**

**Now:** {info['session']} ({info['time_et']})
**Status:** {info['status']}

**Action:**
{info['action']}

**Next:** {info['next']}
"""
    for uid in profiles:
        try:
            await app.bot.send_message(chat_id=int(uid), text=text, parse_mode="Markdown")
        except Exception as e:
            log.error(f"Session alert failed for {uid}: {e}")



# ---------------------------------------------------------------------------
# Daily Market Open Report + Hourly Opportunities
# ---------------------------------------------------------------------------

async def send_daily_open_report(app):
    """Sent every weekday around market open — includes ALL Tradeify accounts"""
    profiles = load_profiles()
    if not profiles:
        return

    today = datetime.now(NY_TZ).strftime("%A, %B %d")
    news = (
        "• Check Economic Calendar (CPI, FOMC, NFP, etc.)\n"
        "• Major events can cause high volatility — size down or wait\n"
        "• Focus on clean A+ setups only"
    )

    for uid, profile in profiles.items():
        try:
            chat_id = int(uid)
            active = profile.get("active_account") or "None"
            bias = profile.get("daily_bias") or "Not set yet"
            journal_count = len(profile.get("journal", []))
            risk = profile.get("risk_percent", 0.5)
            accounts = profile.get("accounts", {})

            # Build per-account section
            if accounts:
                acc_lines = []
                for key, acc in accounts.items():
                    rules = acc.get("rules", {})
                    p_target = acc.get("daily_profit_target") or profile.get("daily_profit_target") or "Not set"
                    m_loss = acc.get("daily_max_loss") or profile.get("daily_max_loss") or "Not set"
                    marker = " ← ACTIVE" if key == active else ""
                    acc_lines.append(
                        f"• `{key}`{marker}\n"
                        f"  DD: ${rules.get('max_drawdown', '?')} | "
                        f"Target: {p_target} | Max Loss: {m_loss}"
                    )
                accounts_block = "**All Tradeify Accounts**\n" + "\n".join(acc_lines)
            else:
                accounts_block = "**All Tradeify Accounts**\nNo accounts configured yet. Use /start"

            text = f"""**Market Open Report — {today}**

{accounts_block}

**Active Focus**
• Active Account: `{active}`
• Daily Bias: {bias}
• Risk per trade: {risk}%
• Journal entries: {journal_count}

**What You Need To Do Today**
1. Set or confirm Daily Bias (`/bias bullish` or `/bias bearish`)
2. Mark key liquidity + FVGs on higher timeframe
3. Only take A or A+ setups (Strict mode on)
4. Flatten all positions by 4:59 PM ET
5. Log trades (`/log` or `/performance`)

**Market News / Focus**
{news}

**Quick Tip**
Wait for liquidity to be taken first. Quality over quantity. Path is max payout — protect the account.

Commands: /session | /sentiment | /checklist | /progress | /phase
"""
            await app.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
        except Exception as e:
            log.error(f"Daily report failed for {uid}: {e}")


async def send_hourly_opportunities(app):
    """Resets every hour — 3 to 5 possible opportunity ideas"""
    profiles = load_profiles()
    if not profiles:
        return

    now = datetime.now(NY_TZ).strftime("%H:%M ET")

    # Simple rotating ideas (in real use this can be AI-generated)
    ideas = [
        "1. Watch for liquidity sweep above/below overnight high/low + BOS",
        "2. Fair Value Gap in discount/premium after New York open",
        "3. Order Block retest after strong displacement",
        "4. Equal highs/lows that have not been swept yet",
        "5. Equilibrium (50%) of the current dealing range for entry",
        "6. SMT divergence between ES and NQ if available",
        "7. Clean Break of Structure + immediate FVG fill"
    ]

    # Pick 3-5
    import random
    selected = random.sample(ideas, k=random.randint(3, 5))

    text = f"""**Hourly Opportunities** — {now}

Possible focus areas right now:
""" + "\n".join(selected) + """

These reset every hour.
Send a chart if you see something forming.
"""

    for uid in profiles:
        try:
            await app.bot.send_message(chat_id=int(uid), text=text, parse_mode="Markdown")
        except Exception as e:
            log.error(f"Hourly opportunities failed for {uid}: {e}")



# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def notes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send TJR Master Notes"""
    text = """**TJR Master Notes (Quick Version)**

**Core Model**
Daily Bias → Liquidity Sweep → Break of Structure → Price returns to FVG or Order Block at/near Equilibrium → Enter

**Key Building Blocks**
• Liquidity: equal highs/lows or obvious swing points (stops)
• BOS: close beyond recent significant high/low
• FVG: imbalance left by strong candle
• Order Block: last opposing candle before displacement
• Equilibrium: 50% of the dealing range (buy discount / sell premium)

**Risk Rules**
• Risk 0.5–1% per trade max
• 1–2 high quality trades per day
• Flatten by 4:59 PM ET
• Never trade against daily bias
• Strict Mode = only A / A+ setups

**One-Sentence Edge**
Trade only when liquidity is taken, structure breaks, and price returns to an imbalance in the direction of your daily bias at a fair price.
"""
    await update.message.reply_text(text, parse_mode="Markdown")


async def checklist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pre-trade checklist"""
    profile = get_user_profile(update.effective_user.id)
    bias = profile.get("daily_bias") or "Not set"
    strict = "ON" if profile.get("strict_mode") else "OFF"
    active = profile.get("active_account") or "None"
    text = f"""**Pre-Trade Checklist**

Active Account: `{active}`
Daily Bias: {bias}
Strict Mode: {strict}

Before you enter, confirm:
[ ] Daily Bias is set and I am trading with it
[ ] Liquidity has been swept (or is about to be)
[ ] Clear Break of Structure in my bias direction
[ ] Entry is at FVG or Order Block
[ ] Price is in discount (buy) or premium (sell)
[ ] I know my exact stop loss
[ ] Position size respects my risk % and Tradeify max contracts
[ ] This is an A or A+ setup (if Strict Mode ON)
[ ] I will flatten by 4:59 PM ET
[ ] I will log the trade after

If any box is unchecked → WAIT.
"""
    await update.message.reply_text(text, parse_mode="Markdown")






async def target_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set daily profit target and/or max loss — per active Tradeify account or global
    Usage: /target profit 200
           /target loss 100
           /target profit 2R
           /target clear
           /target show
    """
    user_id = update.effective_user.id
    profile = get_user_profile(user_id)
    active = profile.get("active_account")

    def get_targets():
        # Prefer per-account targets if they exist
        if active and active in profile.get("accounts", {}):
            acc = profile["accounts"][active]
            p = acc.get("daily_profit_target") or profile.get("daily_profit_target")
            l = acc.get("daily_max_loss") or profile.get("daily_max_loss")
            scope = f"account {active}" if (acc.get("daily_profit_target") or acc.get("daily_max_loss")) else "global"
            return p, l, scope
        return profile.get("daily_profit_target"), profile.get("daily_max_loss"), "global"

    if not context.args or context.args[0].lower() == "show":
        profit, loss, scope = get_targets()
        await update.message.reply_text(
            f"**Daily Targets** (scope: {scope})\n"
            f"Active Account: `{active or "None"}`\n"
            f"Profit Target: {profit or "Not set"}\n"
            f"Max Loss: {loss or "Not set"}\n\n"
            f"Usage:\n"
            f"/target profit 200\n"
            f"/target loss 100\n"
            f"/target profit 2R\n"
            f"/target clear\n"
            f"(Targets apply to the active Tradeify account. Switch account first if needed.)",
            parse_mode="Markdown"
        )
        return

    action = context.args[0].lower()

    if action == "clear":
        if active and active in profile.get("accounts", {}):
            profiles = load_profiles()
            uid = str(user_id)
            profiles[uid]["accounts"][active]["daily_profit_target"] = None
            profiles[uid]["accounts"][active]["daily_max_loss"] = None
            save_profiles(profiles)
            await update.message.reply_text(f"Targets cleared for **{active}**", parse_mode="Markdown")
        else:
            update_user_profile(user_id, {"daily_profit_target": None, "daily_max_loss": None})
            await update.message.reply_text("Global daily targets cleared.")
        return

    if action in ("profit", "loss") and len(context.args) >= 2:
        value = " ".join(context.args[1:])
        if active and active in profile.get("accounts", {}):
            profiles = load_profiles()
            uid = str(user_id)
            if action == "profit":
                profiles[uid]["accounts"][active]["daily_profit_target"] = value
            else:
                profiles[uid]["accounts"][active]["daily_max_loss"] = value
            save_profiles(profiles)
            await update.message.reply_text(f"Daily {action} target set to **{value}** for account `{active}`", parse_mode="Markdown")
        else:
            if action == "profit":
                update_user_profile(user_id, {"daily_profit_target": value})
            else:
                update_user_profile(user_id, {"daily_max_loss": value})
            await update.message.reply_text(f"Daily {action} target set to **{value}** (global)", parse_mode="Markdown")
        return

    await update.message.reply_text("Usage: /target profit 200  or  /target loss 100  or  /target clear")


async def check_daily_targets_and_alert(app):
    """Periodic check — remind user of targets and warn near limits (simple version)"""
    profiles = load_profiles()
    if not profiles:
        return

    now = datetime.now(NY_TZ)
    # Only during active trading hours
    if now.weekday() >= 5 or not (9 <= now.hour <= 16):
        return

    for uid, profile in profiles.items():
        profit_t = profile.get("daily_profit_target")
        loss_t = profile.get("daily_max_loss")
        if not profit_t and not loss_t:
            continue

        try:
            text = f"**Daily Target Reminder** ({now.strftime('%H:%M ET')})\n\n"
            if profit_t:
                text += f"Profit Target: **{profit_t}**\n"
            if loss_t:
                text += f"Max Loss Limit: **{loss_t}**\n"
            text += "\nStay disciplined. Hit target → done for the day. Hit max loss → stop."
            await app.bot.send_message(chat_id=int(uid), text=text, parse_mode="Markdown")
        except Exception as e:
            log.error(f"Daily target alert failed for {uid}: {e}")



async def fetch_fear_greed():
    """Try free public Fear & Greed style endpoints. Returns dict or None."""
    import requests as req
    sources = []
    # feargreedchart.com free endpoint
    try:
        r = req.get("https://feargreedchart.com/api/?action=score", timeout=8)
        if r.ok:
            data = r.json()
            score = data.get("score") or data.get("value") or (data.get("data") or {}).get("score")
            if score is not None:
                sources.append(("FearGreedChart", float(score)))
    except Exception as e:
        log.debug(f"F&G chart fetch fail: {e}")

    # Alternative simple endpoint sometimes used
    try:
        r = req.get("https://api.alternative.me/fng/?limit=1", timeout=8)
        if r.ok:
            data = r.json()
            score = float(data["data"][0]["value"])
            sources.append(("Alternative.me", score))
    except Exception as e:
        log.debug(f"Alt.me F&G fail: {e}")

    if not sources:
        return None
    # Average if multiple
    avg = sum(s for _, s in sources) / len(sources)
    label = "Extreme Fear" if avg < 25 else "Fear" if avg < 45 else "Neutral" if avg < 55 else "Greed" if avg < 75 else "Extreme Greed"
    return {"score": round(avg, 1), "label": label, "sources": sources}


async def sentiment_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Improved market sentiment: live Fear & Greed when possible + structured AI read"""
    user_id = update.effective_user.id
    profile = get_user_profile(user_id)
    await update.message.chat.send_action(ChatAction.TYPING)

    fg = await fetch_fear_greed()
    fg_text = "Live Fear & Greed unavailable right now"
    if fg:
        srcs = ", ".join(f"{n}: {s}" for n, s in fg["sources"])
        fg_text = f"**Live Fear & Greed:** {fg['score']} ({fg['label']})\nSources: {srcs}"

    prompt = f"""You are a futures day-trading analyst focused on ES and NQ.
Give a practical sentiment snapshot for the next few hours.

Live data provided:
{fg_text}

Reply in this exact structure:

**Market Sentiment Snapshot**
**Overall Bias:** Risk-On / Risk-Off / Neutral / Mixed
**Fear & Greed context:** (interpret the live number if present, else say no live data)
**ES (S&P):** Bullish / Bearish / Neutral — one short reason
**NQ (Nasdaq):** Bullish / Bearish / Neutral — one short reason
**Key Drivers:** 2-4 short bullets
**Next 1-4 hours watch:** one clear sentence
**Sentiment Score:** 0-100 (50 = neutral)
**Day-trader advice:** one direct sentence (confluence only, not a signal)

Rules: Be concise. No predictions of exact price. If live data is missing, say so and give a framework read.
"""

    answer = None
    try:
        if anthropic_client:
            msg = anthropic_client.messages.create(
                model="claude-3-5-sonnet-20241022",
                max_tokens=700,
                messages=[{"role": "user", "content": prompt}]
            )
            answer = msg.content[0].text
        elif openai_client:
            resp = openai_client.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=700
            )
            answer = resp.choices[0].message.content
    except Exception as e:
        log.error(f"Sentiment AI error: {e}")

    if not answer:
        answer = (
            f"**Market Sentiment Snapshot**\n{fg_text}\n\n"
            "AI layer unavailable. Manual check:\n"
            "• ES/NQ structure (HH/HL or LH/LL)?\n"
            "• Above or below VWAP / overnight range?\n"
            "• High-impact news in next 2 hours?\n"
            "• Risk-on (NQ strong) or risk-off?"
        )

    bias = profile.get("daily_bias") or "Not set"
    answer += f"\n\n**Your Daily Bias:** {bias}\nSentiment is confluence only — never trade it alone."
    await update.message.reply_text(answer, parse_mode="Markdown")


async def performance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start manual performance tracking — AI asks for all needed details"""
    user_id = update.effective_user.id
    # Begin multi-step intake
    update_user_profile(user_id, {
        "awaiting_performance": {
            "step": 1,
            "data": {}
        }
    })
    await update.message.reply_text(
        "**Performance Log — Step 1/6**\n\n"
        "What was the **result** of the trade or session?\n"
        "Examples: `+2R`  |  `+150`  |  `-1R`  |  `-80`  |  `breakeven`",
        parse_mode="Markdown"
    )


async def handle_performance_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, profile: Dict) -> bool:
    """Return True if we handled a performance step"""
    state = profile.get("awaiting_performance")
    if not state:
        return False

    user_id = update.effective_user.id
    text = update.message.text.strip()
    step = state.get("step", 1)
    data = state.get("data", {})

    if step == 1:
        data["result"] = text
        state["step"] = 2
        state["data"] = data
        update_user_profile(user_id, {"awaiting_performance": state})
        await update.message.reply_text(
            "**Step 2/6**\nWhat was the **setup / strategy** used?\n"
            "Examples: `Liquidity Sweep + BOS` | `Order Block Retest` | `FVG Continuation` | `Other`"
        )
        return True

    if step == 2:
        data["setup"] = text
        state["step"] = 3
        state["data"] = data
        update_user_profile(user_id, {"awaiting_performance": state})
        await update.message.reply_text(
            "**Step 3/6**\nWhich **instrument & session**?\n"
            "Examples: `ES New York` | `NQ London` | `ES NY Killzone`"
        )
        return True

    if step == 3:
        data["instrument_session"] = text
        state["step"] = 4
        state["data"] = data
        update_user_profile(user_id, {"awaiting_performance": state})
        await update.message.reply_text(
            "**Step 4/6**\nDid you follow your plan and rules?\n"
            "Reply: `Yes` / `No` / `Partially` — and short reason if No or Partially"
        )
        return True

    if step == 4:
        data["followed_plan"] = text
        state["step"] = 5
        state["data"] = data
        update_user_profile(user_id, {"awaiting_performance": state})
        await update.message.reply_text(
            "**Step 5/6**\nWhat was your **emotional state**?\n"
            "Examples: `Calm` | `FOMO` | `Revenge` | `Confident` | `Scared` | `Bored`"
        )
        return True

    if step == 5:
        data["emotion"] = text
        state["step"] = 6
        state["data"] = data
        update_user_profile(user_id, {"awaiting_performance": state})
        await update.message.reply_text(
            "**Step 6/6**\nAny **lesson or note** for future you?\n"
            "(Or type `skip`)"
        )
        return True

    if step == 6:
        data["lesson"] = text if text.lower() != "skip" else ""
        data["date"] = datetime.now(NY_TZ).strftime("%Y-%m-%d %H:%M ET")
        data["account"] = profile.get("active_account")

        # Save
        logs = profile.get("performance_logs", [])
        logs.append(data)
        update_user_profile(user_id, {
            "performance_logs": logs,
            "awaiting_performance": None
        })

        # Also add simplified version to journal
        add_journal_entry(user_id, {
            "date": data["date"],
            "result": data.get("result"),
            "setup": data.get("setup"),
            "account": data.get("account"),
            "emotion": data.get("emotion"),
            "followed_plan": data.get("followed_plan"),
            "lesson": data.get("lesson")
        })

        summary = (
            f"**Performance Logged**\n\n"
            f"Result: {data.get('result')}\n"
            f"Setup: {data.get('setup')}\n"
            f"Instrument/Session: {data.get('instrument_session')}\n"
            f"Followed Plan: {data.get('followed_plan')}\n"
            f"Emotion: {data.get('emotion')}\n"
            f"Lesson: {data.get('lesson') or '—'}\n"
            f"Account: {data.get('account') or '—'}\n\n"
            f"Saved. Type /performance to start another or /stats for summary."
        )
        await update.message.reply_text(summary, parse_mode="Markdown")
        await start_debrief(update, user_id, {
            "result": data.get("result"),
            "setup": data.get("setup"),
            "source": "performance",
        })
        return True

    return False


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show performance stats from manual logs"""
    user_id = update.effective_user.id
    profile = get_user_profile(user_id)
    logs = profile.get("performance_logs", []) or profile.get("journal", [])

    if not logs:
        await update.message.reply_text("No performance logs yet.\nUse /performance to log a trade or session.")
        return

    total = len(logs)
    wins = sum(1 for x in logs if str(x.get("result", "")).startswith("+"))
    losses = sum(1 for x in logs if str(x.get("result", "")).startswith("-"))
    be = total - wins - losses
    winrate = (wins / total * 100) if total else 0

    # Emotion breakdown
    emotions = {}
    for x in logs:
        e = (x.get("emotion") or "unknown").lower()
        emotions[e] = emotions.get(e, 0) + 1
    top_emotions = sorted(emotions.items(), key=lambda kv: -kv[1])[:3]
    emo_text = ", ".join(f"{k}({v})" for k, v in top_emotions) if top_emotions else "—"

    text = (
        f"**Performance Stats**\n\n"
        f"Total logged: {total}\n"
        f"Wins: {wins} | Losses: {losses} | BE/Other: {be}\n"
        f"Win rate: {winrate:.1f}%\n"
        f"Top emotions: {emo_text}\n\n"
        f"Active account: `{profile.get('active_account') or 'None'}`\n"
        f"Use /performance to add more."
    )
    await update.message.reply_text(text, parse_mode="Markdown")



async def asset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Switch adaptive asset profile: auto / futures / crypto / forex / stocks"""
    user_id = update.effective_user.id
    profile = get_user_profile(user_id)
    current = profile.get("asset_profile", "auto")

    if not context.args:
        await update.message.reply_text(
            f"**Adaptive Asset Profile**\n\n"
            f"Current: `{current}`\n\n"
            f"Usage:\n"
            f"/asset auto — detect from chart\n"
            f"/asset futures — ES, NQ, CL, GC, etc.\n"
            f"/asset crypto — ALL crypto BTC/ETH/alts (24/7)\n"
            f"/asset forex — EURUSD, GBPUSD, etc.\n"
            f"/asset stocks — individual stocks / equity CFDs\n\n"
            f"Profile changes how the AI reads sessions, volatility, VWAP, and liquidity.",
            parse_mode="Markdown"
        )
        return

    choice = context.args[0].lower()
    valid = ("auto", "futures", "crypto", "forex", "stocks")
    if choice not in valid:
        await update.message.reply_text("Use: /asset auto|futures|crypto|forex|stocks")
        return

    update_user_profile(user_id, {"asset_profile": choice})
    tips = {
        "auto": "AI will detect asset class from each chart and ask if unclear.",
        "futures": "Session-aware, VWAP/overnight levels, Tradeify flatten rules emphasized.",
        "crypto": "24/7 mode, higher volatility tolerance, no traditional RTH flatten.",
        "forex": "London/NY killzones and Asian range prioritized.",
        "stocks": "RTH gaps, news sensitivity, volume/VWAP emphasis.",
    }
    await update.message.reply_text(
        f"Asset profile set to **{choice}**\n{tips[choice]}",
        parse_mode="Markdown"
    )







async def phase_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set eval vs funded phase for max-payout coaching"""
    user_id = update.effective_user.id
    if not context.args or context.args[0].lower() not in ("eval", "funded"):
        profile = get_user_profile(user_id)
        phase = profile.get("account_phase", "eval")
        await update.message.reply_text(
            f"**Account phase:** `{phase}`\n\n"
            f"/phase eval — evaluation (hit profit target, respect rules)\n"
            f"/phase funded — funded (protect DD, consistency, push payout)",
            parse_mode="Markdown"
        )
        return
    phase = context.args[0].lower()
    update_user_profile(user_id, {"account_phase": phase})
    if phase == "eval":
        msg = "Phase set to **eval**. Focus: hit profit target clean, min days, no rule breaks."
    else:
        msg = "Phase set to **funded**. Focus: protect trailing DD, respect consistency, build toward max payout."
    await update.message.reply_text(msg, parse_mode="Markdown")


async def progress_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Log or show progress toward Tradeify profit target / max payout path"""
    user_id = update.effective_user.id
    profile = get_user_profile(user_id)
    active = profile.get("active_account")

    if context.args and context.args[0].lower() == "set" and len(context.args) >= 2:
        try:
            val = float(context.args[1].replace("$", "").replace(",", ""))
            update_user_profile(user_id, {"account_balance_profit": val})
            await update.message.reply_text(f"Current profit progress set to **${val:,.0f}**", parse_mode="Markdown")
            return
        except ValueError:
            await update.message.reply_text("Usage: /progress set 1200")
            return

    if context.args and context.args[0].lower() == "day" and len(context.args) >= 2:
        try:
            val = float(context.args[1].replace("$", "").replace(",", ""))
            prev = profile.get("best_day_profit") or 0
            if val > prev:
                update_user_profile(user_id, {"best_day_profit": val})
            await update.message.reply_text(f"Today/best day note logged: **${val:,.0f}** (best stored if higher)", parse_mode="Markdown")
            return
        except ValueError:
            await update.message.reply_text("Usage: /progress day 400")
            return

    # Show payout path dashboard
    phase = profile.get("account_phase", "eval")
    profit = profile.get("account_balance_profit") or 0
    best_day = profile.get("best_day_profit") or 0
    bias = profile.get("daily_bias") or "Not set"
    strict = "ON" if profile.get("strict_mode") else "OFF"

    target = None
    max_dd = None
    daily_loss = None
    consistency = None
    plan_size = active or "No account"

    if active and active in profile.get("accounts", {}):
        acc = profile["accounts"][active]
        rules = acc.get("rules", {})
        target = rules.get("profit_target")
        max_dd = rules.get("max_drawdown")
        daily_loss = rules.get("daily_loss_limit")
        if phase == "eval":
            consistency = rules.get("consistency_eval")
        else:
            consistency = rules.get("consistency_funded")
        plan_size = active

    left = (target - profit) if target else None
    pct = (profit / target * 100) if target and target > 0 else None

    cons_line = "None on this plan/phase"
    if consistency:
        # consistency is max fraction of total profit from one day
        if target and profit > 0:
            max_one_day = consistency * max(profit, target * 0.5)
            cons_line = f"{consistency*100:.0f}% rule — avoid one day dominating (best day logged: ${best_day:,.0f})"
        else:
            cons_line = f"{consistency*100:.0f}% of profits from a single day max — keep days balanced"

    text = f"""**Max Payout Path**

**Account:** `{plan_size}`
**Phase:** {phase.upper()}
**Strict:** {strict} | **Bias:** {bias}

**Progress**
• Current profit: ${profit:,.0f}
• Profit target: {f"${target:,.0f}" if target else "N/A (e.g. Lightning funded style)"}
• Remaining: {f"${left:,.0f}" if left is not None else "—"}
• Progress: {f"{pct:.0f}%" if pct is not None else "—"}

**Risk walls (do not touch)**
• Max trailing DD: {f"${max_dd:,.0f}" if max_dd else "—"}
• Daily loss limit: {f"${daily_loss:,.0f}" if daily_loss else "None / plan dependent"}

**Consistency**
{cons_line}

**What to do**
• Eval: clean path to target — no hero trades
• Funded: protect DD, balance days, scale only when stable
• Update profit: `/progress set 1500`
• Log big day: `/progress day 400`
• Switch phase: `/phase eval` or `/phase funded`

I only greenlight setups that serve this path.
"""
    await update.message.reply_text(text, parse_mode="Markdown")





async def mode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set trading mode: paper | live | funded"""
    user_id = update.effective_user.id
    profile = get_user_profile(user_id)
    if not context.args:
        cur = profile.get("trading_mode", "paper")
        cr = profile.get("custom_rules") or {}
        msg = (
            "**Mode:** `" + str(cur) + "`" + "\n\n"
            "/mode paper — practice" + "\n"
            "/mode live — own money (+ /customrule)" + "\n"
            "/mode funded — Tradeify prop rules" + "\n\n"
            "Custom (live): daily_loss=" + str(cr.get("max_daily_loss"))
            + " risk%=" + str(cr.get("max_risk_percent"))
            + " flatten=" + str(cr.get("flatten_et"))
            + " notes=" + str(cr.get("notes") or "—")
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
        return
    choice = context.args[0].lower()
    if choice in ("own", "personal", "cash"):
        choice = "live"
    if choice in ("prop", "tradeify"):
        choice = "funded"
    if choice not in ("paper", "live", "funded"):
        await update.message.reply_text("Use: /mode paper | live | funded")
        return
    update_user_profile(user_id, {"trading_mode": choice})
    msgs = {
        "paper": "**PAPER** — practice. Full bot. No hard blocks.",
        "live": "**LIVE** — own capital. No Tradeify. /customrule to set your rules.",
        "funded": "**FUNDED** — Tradeify rules + max payout. /start if account not set.",
    }
    await update.message.reply_text(msgs[choice], parse_mode="Markdown")


async def customrule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Custom rules for LIVE (own money) mode."""
    user_id = update.effective_user.id
    profile = get_user_profile(user_id)
    cr = dict(profile.get("custom_rules") or {})
    for k, v in (("max_daily_loss", None), ("max_risk_percent", None), ("flatten_et", None), ("notes", "")):
        cr.setdefault(k, v)

    if not context.args or context.args[0].lower() == "show":
        msg = (
            "**Custom rules (LIVE mode only)**" + "\n"
            "Daily loss cap: " + str(cr.get("max_daily_loss") or "not set") + "\n"
            "Risk %/trade: " + str(cr.get("max_risk_percent") or "not set") + "\n"
            "Flatten ET: " + str(cr.get("flatten_et") or "not set") + "\n"
            "Notes: " + str(cr.get("notes") or "—") + "\n\n"
            "/customrule daily 200" + "\n"
            "/customrule risk 0.5" + "\n"
            "/customrule flatten 16:59" + "\n"
            "/customrule notes your text" + "\n"
            "/customrule clear"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    action = context.args[0].lower()
    if action == "clear":
        update_user_profile(user_id, {"custom_rules": {"max_daily_loss": None, "max_risk_percent": None, "flatten_et": None, "notes": ""}})
        await update.message.reply_text("Custom rules cleared.")
        return
    if action == "daily" and len(context.args) >= 2:
        try:
            cr["max_daily_loss"] = float(context.args[1].replace("$", ""))
            update_user_profile(user_id, {"custom_rules": cr})
            await update.message.reply_text("Max daily loss set to $" + str(cr["max_daily_loss"]) + " (live)")
        except ValueError:
            await update.message.reply_text("Usage: /customrule daily 200")
        return
    if action == "risk" and len(context.args) >= 2:
        try:
            cr["max_risk_percent"] = float(context.args[1].replace("%", ""))
            update_user_profile(user_id, {"custom_rules": cr})
            await update.message.reply_text("Max risk/trade set to " + str(cr["max_risk_percent"]) + "% (live)")
        except ValueError:
            await update.message.reply_text("Usage: /customrule risk 0.5")
        return
    if action == "flatten" and len(context.args) >= 2:
        cr["flatten_et"] = context.args[1]
        update_user_profile(user_id, {"custom_rules": cr})
        await update.message.reply_text("Flatten set to " + str(cr["flatten_et"]) + " ET (live)")
        return
    if action == "notes" and len(context.args) >= 2:
        cr["notes"] = " ".join(context.args[1:])
        update_user_profile(user_id, {"custom_rules": cr})
        await update.message.reply_text("Notes saved: " + cr["notes"])
        return
    await update.message.reply_text("Usage: /customrule show|daily|risk|flatten|notes|clear")


async def customrules_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Separate TJR-style custom rules for LIVE own-money stocks/futures, not funded or Sol crypto."""
    user_id = update.effective_user.id
    profile = get_user_profile(user_id)
    rules = dict(profile.get("live_stock_rules") or {})
    defaults = {
        "risk_percent": 0.5,
        "max_daily_loss_pct": 2.0,
        "max_trades_day": 3,
        "min_rr": 2.0,
        "session": "NY open 9:30-11:30 ET",
        "a_plus_only": True,
        "require_htf_bias": True,
        "require_liquidity_sweep": True,
        "require_displacement_bos": True,
        "prefer_fvg_entry": True,
        "stop_beyond_swing": True,
        "no_revenge_trading": True,
    }
    for key, value in defaults.items():
        rules.setdefault(key, value)

    if not context.args or context.args[0].lower() in ("show", "help"):
        msg = (
            "**⚙️ Custom LIVE Stock/Futures Rules — TJR style**\n"
            "Separate from Tradeify funded rules and separate from Sol meme risk presets.\n\n"
            f"• Risk/trade: {rules['risk_percent']}%\n"
            f"• Max daily loss: {rules['max_daily_loss_pct']}%\n"
            f"• Max trades/day: {rules['max_trades_day']}\n"
            f"• Minimum TP R:R: {rules['min_rr']}:1\n"
            f"• Session: {rules['session']}\n"
            "• A+ setups only; no forcing trades\n"
            "• Set higher-timeframe bias first (Daily/4H/1H); use 15m for entries\n"
            "• Prefer liquidity sweep + displacement/BOS + FVG/OTE confluence\n"
            "• Stop beyond invalidation; define risk before entry\n"
            "• No revenge trading; stop when daily loss limit is reached\n\n"
            "**Commands**\n"
            "/customrules risk 0.5\n"
            "/customrules daily 2\n"
            "/customrules trades 3\n"
            "/customrules rr 2\n"
            "/customrules session NY-open\n"
            "/customrules reset"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🛡️ Conservative preset", callback_data="customrules_preset")],
            [InlineKeyboardButton("📋 Show rules", callback_data="customrules_btn")],
            [InlineKeyboardButton("🏠 Stocks desk", callback_data="hub_stocks")],
        ])
        await update.effective_message.reply_text(msg, parse_mode="Markdown", reply_markup=kb)
        return

    action = context.args[0].lower()
    if action == "reset":
        update_user_profile(user_id, {"live_stock_rules": defaults})
        await update.effective_message.reply_text("LIVE stock/futures rules reset to the TJR-style defaults. Sol meme rules and funded rules were not changed.")
        return
    try:
        if action == "risk" and len(context.args) >= 2:
            val = float(context.args[1].replace("%", ""))
            if not 0 < val <= 5:
                raise ValueError
            rules["risk_percent"] = val
        elif action == "daily" and len(context.args) >= 2:
            val = float(context.args[1].replace("%", ""))
            if not 0 < val <= 20:
                raise ValueError
            rules["max_daily_loss_pct"] = val
        elif action == "trades" and len(context.args) >= 2:
            val = int(context.args[1])
            if not 1 <= val <= 20:
                raise ValueError
            rules["max_trades_day"] = val
        elif action == "rr" and len(context.args) >= 2:
            val = float(context.args[1].replace(":1", ""))
            if not 0.5 <= val <= 10:
                raise ValueError
            rules["min_rr"] = val
        elif action == "session" and len(context.args) >= 2:
            session = " ".join(context.args[1:]).strip()
            if len(session) > 60:
                raise ValueError
            rules["session"] = session
        else:
            await update.effective_message.reply_text("Usage: /customrules risk 0.5 | daily 2 | trades 3 | rr 2 | session NY-open | reset")
            return
    except ValueError:
        await update.effective_message.reply_text("Invalid value. Use risk 0.1-5%, daily 0.1-20%, trades 1-20, or R:R 0.5-10.")
        return
    update_user_profile(user_id, {"live_stock_rules": rules})
    await update.effective_message.reply_text(
        f"Saved LIVE stock/futures rule: {action}.\nRisk {rules['risk_percent']}% | daily loss {rules['max_daily_loss_pct']}% | max trades {rules['max_trades_day']} | min R:R {rules['min_rr']}:1 | {rules['session']}\nSol and funded rules unchanged. Use /customrules to review.",
    )


async def paper_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Legacy: force paper mode"""
    update_user_profile(update.effective_user.id, {"trading_mode": "paper"})
    await update.message.reply_text("**PAPER MODE**" + "\n" + "Practice on. /mode live or /mode funded to switch.", parse_mode="Markdown")


async def cryptoscan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chain = context.args[0] if context.args else "solana"
    profile = get_user_profile(update.effective_user.id)
    rp = profile.get("risk_preset", "medium")
    chain = chain if context.args else profile.get("crypto_chain", "solana")
    if not context.args:
        chain = "solana"
    await update.message.reply_text("Scanning Sol under your risk + safety presets…")
    out = await asyncio.to_thread(
        crypto_scan_memecoins, chain, limit=8, risk_preset=rp,
        no_rug=profile.get("no_rug_mode", True), anti_mev=profile.get("anti_mev", True),
    )
    await update.message.reply_text(out[:4000], parse_mode="Markdown", disable_web_page_preview=True)


async def snipe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /snipe <token_or_pair_address> [chain]")
        return
    address = context.args[0]
    chain = context.args[1] if len(context.args) > 1 else None
    profile = get_user_profile(update.effective_user.id)
    rp = profile.get("risk_preset", "medium")
    await update.message.reply_text("Analyzing Sol contract under risk + no_rug/anti_mev…")
    out = await asyncio.to_thread(
        crypto_snipe_token, address, chain or "solana", risk_preset=rp,
        no_rug=profile.get("no_rug_mode", True), anti_mev=profile.get("anti_mev", True),
    )
    await update.message.reply_text(out[:4000], parse_mode="Markdown", disable_web_page_preview=True)


async def copyfind_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chain = context.args[0] if context.args else "solana"
    profile = get_user_profile(update.effective_user.id)
    rp = profile.get("risk_preset", "medium")
    if not context.args:
        chain = "solana"
    await update.message.reply_text("Building Sol copy board under risk + safety…")
    out = await asyncio.to_thread(
        crypto_copyfind, chain, risk_preset=rp,
        no_rug=profile.get("no_rug_mode", True), anti_mev=profile.get("anti_mev", True),
    )
    await update.message.reply_text(out[:4000], parse_mode="Markdown", disable_web_page_preview=True)


async def riskpreset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    profile = get_user_profile(user_id)
    if not context.args:
        await update.message.reply_text(
            format_risk_preset(profile) + "\n\nSet: /riskpreset chill | medium | degen | custom",
            parse_mode="Markdown",
        )
        return
    choice = context.args[0].lower()
    if choice in ("risky", "yolo", "high"):
        choice = "degen"
    if choice not in ("chill", "medium", "degen"):
        await update.message.reply_text("Use: /riskpreset chill | medium | degen")
        return
    update_user_profile(user_id, {"risk_preset": choice})
    profile = get_user_profile(user_id)
    await update.message.reply_text("Preset locked.\n\n" + format_risk_preset(profile), parse_mode="Markdown")


async def copyadd_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    profile = get_user_profile(user_id)
    if not context.args:
        await update.message.reply_text("Usage: /copyadd <wallet> <optional_label>")
        return
    wallet = context.args[0].strip()
    label = " ".join(context.args[1:]) if len(context.args) > 1 else wallet[:8]
    traders = list(profile.get("copy_traders") or [])
    traders = [t for t in traders if t.get("wallet") != wallet]
    traders.append({"wallet": wallet, "label": label, "source": "manual"})
    update_user_profile(user_id, {"copy_traders": traders[-25:]})
    await update.message.reply_text(
        f"Added **{label}**\n`{wallet}`\nKOLScan: https://kolscan.io/account/{wallet}\n/copylist to view",
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


async def copyrm_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    profile = get_user_profile(user_id)
    if not context.args:
        await update.message.reply_text("Usage: /copyrm <wallet_or_label>")
        return
    key = " ".join(context.args).lower()
    traders = list(profile.get("copy_traders") or [])
    new = [t for t in traders if key not in t.get("wallet", "").lower() and key not in t.get("label", "").lower()]
    update_user_profile(user_id, {"copy_traders": new})
    await update.message.reply_text(f"Removed matches for `{key}`. Left: {len(new)}", parse_mode="Markdown")


async def copylist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profile = get_user_profile(update.effective_user.id)
    traders = profile.get("copy_traders") or []
    preset = format_risk_preset(profile)
    if not traders:
        await update.message.reply_text(
            "No copy traders yet.\n/copyadd <wallet> <name>\n/kolscan or /fomo to discover\n\n" + preset,
            parse_mode="Markdown",
        )
        return
    lines = ["**Your copy book**", preset, ""]
    for t in traders:
        w = t.get("wallet", "")
        lines.append(f"• **{t.get('label', '?')}** `{w}`")
        lines.append(f"  https://kolscan.io/account/{w}")
    lines.append("\n/copyrm <wallet> to remove | size with risk preset")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True)


async def kolscan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Loading KOL board…")
    out = fetch_kolscan_leaderboard(15)
    await update.message.reply_text(out[:4000], parse_mode="Markdown", disable_web_page_preview=True)



async def deposit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):  # also /withdraw
    """Send SOL from burner to address you choose.
    /deposit
    /withdraw <to_address> <amount_sol>
    /withdraw cancel
    """
    user_id = update.effective_user.id
    profile = get_user_profile(user_id)
    args = context.args or []

    if args and args[0].lower() == "cancel":
        update_user_profile(user_id, {"awaiting_deposit": None})
        await update.message.reply_text("Withdraw cancelled.")
        return

    secret = profile.get("sol_burner_key")
    if not secret:
        await update.message.reply_text(
            "Need a burner key first.\n`/wallet key <base58_secret>`",
            parse_mode="Markdown",
        )
        return

    # one-shot: /withdraw <to> <amount>
    if len(args) >= 2:
        to_addr = args[0].strip()
        try:
            amount = float(args[1])
            if not __import__("math").isfinite(amount) or amount <= 0:
                raise ValueError("amount must be finite and greater than zero")
        except (ValueError, TypeError):
            await update.message.reply_text("Usage: /withdraw <to_address> <amount_sol> (amount must be finite and > 0)")
            return
        if len(to_addr) < 32:
            await update.message.reply_text("That destination does not look like a valid Solana address.")
            return
        await update.message.reply_text(f"Sending {amount} SOL to `{to_addr}`…", parse_mode="Markdown")
        result = await asyncio.to_thread(sol_send_sol, secret, to_addr, amount)
        if result.get("ok"):
            update_user_profile(user_id, {"deposit_address": to_addr, "awaiting_deposit": None})
            await update.message.reply_text(
                f"**Sent** {amount} SOL\n"
                f"To: `{to_addr}`\n"
                f"Sig: `{result.get('sig')}`\n"
                f"https://solscan.io/tx/{result.get('sig')}",
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
        else:
            await update.message.reply_text(f"Send failed: {result.get('error')}")
        return

    # interactive
    update_user_profile(user_id, {"awaiting_deposit": {"step": "to"}})
    default = profile.get("deposit_address")
    extra = f"\nLast used: `{default}`" if default else ""
    await update.message.reply_text(
        "**Deposit / send from burner**\n"
        "Reply with the **destination address** (your real wallet)."
        + extra
        + "\n\nOr one shot: `/withdraw <address> <amount>`\n"
        "/withdraw cancel — abort",
        parse_mode="Markdown",
    )



async def fomo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(fetch_fomo_board_help(), parse_mode="Markdown", disable_web_page_preview=True)



def sol_pubkey_from_secret(secret: str) -> Optional[str]:
    """Derive Solana public address from base58 secret (burner)."""
    secret = (secret or "").strip()
    if not secret:
        return None
    try:
        from solders.keypair import Keypair
        kp = Keypair.from_base58_string(secret)
        return str(kp.pubkey())
    except Exception:
        pass
    try:
        import base58
        from nacl.signing import SigningKey
        raw = base58.b58decode(secret)
        if len(raw) >= 32:
            sk = SigningKey(raw[:32])
            # Solana address = base58 of 32-byte verify key
            return base58.b58encode(bytes(sk.verify_key)).decode()
    except Exception:
        pass
    return None


def sol_rpc_balance(pubkey: str) -> Optional[float]:
    """SOL balance via public mainnet RPC."""
    if not pubkey:
        return None
    try:
        import requests as req
        r = req.post(
            "https://api.mainnet-beta.solana.com",
            json={"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [pubkey]},
            timeout=12,
        )
        val = (r.json().get("result") or {}).get("value")
        if val is None:
            return None
        return float(val) / 1e9
    except Exception:
        return None



def sol_send_sol(secret: str, to_address: str, amount_sol: float) -> dict:
    """Sign and send native SOL from burner. Returns {ok, sig|error}."""
    if os.getenv("SOL_WITHDRAWALS_ENABLED", "false").strip().lower() not in ("1", "true", "yes", "on"):
        return {"ok": False, "error": "SOL withdrawals are temporarily disabled for safety"}
    try:
        from solders.keypair import Keypair
        from solders.pubkey import Pubkey
        from solders.system_program import TransferParams, transfer
        from solders.transaction import Transaction
        from solders.hash import Hash
        from solders.message import Message
        import requests as req
    except Exception as e:
        return {"ok": False, "error": f"Missing solders: {e}. pip install solders"}

    try:
        import math
        if not math.isfinite(float(amount_sol)) or float(amount_sol) <= 0:
            return {"ok": False, "error": "Amount must be a finite number greater than zero"}
        if float(amount_sol) > 1_000_000:
            return {"ok": False, "error": "Amount exceeds the hard safety limit of 1,000,000 SOL"}
        kp = Keypair.from_base58_string(secret.strip())
        to_pk = Pubkey.from_string(to_address.strip())
        lamports = int(amount_sol * 1_000_000_000)
        # leave rent - basic check
        bal = sol_rpc_balance(str(kp.pubkey()))
        if bal is not None and bal < amount_sol + 0.001:
            return {"ok": False, "error": f"Insufficient SOL (have {bal:.6f}, need ~{amount_sol + 0.001:.6f} incl fees)"}

        bh = req.post(
            "https://api.mainnet-beta.solana.com",
            json={"jsonrpc": "2.0", "id": 1, "method": "getLatestBlockhash", "params": [{"commitment": "finalized"}]},
            timeout=15,
        ).json()
        blockhash = (bh.get("result") or {}).get("value", {}).get("blockhash")
        if not blockhash:
            return {"ok": False, "error": "Could not get blockhash"}

        ix = transfer(TransferParams(from_pubkey=kp.pubkey(), to_pubkey=to_pk, lamports=lamports))
        msg = Message.new_with_blockhash([ix], kp.pubkey(), Hash.from_string(blockhash))
        tx = Transaction.new_unsigned(msg)
        tx.sign([kp], Hash.from_string(blockhash))

        raw = list(bytes(tx))
        # solders Transaction has to_json / or use sendTransaction base64
        import base64 as b64
        # Prefer serialize
        try:
            wire = bytes(tx)
        except Exception:
            wire = tx.to_bytes_with_signatures() if hasattr(tx, "to_bytes_with_signatures") else bytes(tx)
        b64tx = b64.b64encode(wire).decode()

        send = req.post(
            "https://api.mainnet-beta.solana.com",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "sendTransaction",
                "params": [b64tx, {"encoding": "base64", "preflightCommitment": "confirmed"}],
            },
            timeout=30,
        ).json()
        if send.get("error"):
            return {"ok": False, "error": str(send["error"])}
        sig = send.get("result")
        return {"ok": True, "sig": sig, "from": str(kp.pubkey()), "to": to_address, "amount": amount_sol}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def wallet_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Burner wallet: public address and/or private key.

    /wallet                     show
    /wallet <pubkey>            save public only
    /wallet key <base58_secret> import BURNER private key (control)
    /wallet balance             SOL balance
    /wallet clear               wipe address + key

    Use a burner only. Fund it small, trade, send profits to your real wallet yourself.
    Never import your main wallet seed/key.
    """
    user_id = update.effective_user.id
    profile = get_user_profile(user_id)
    args = context.args or []

    if not args:
        w = profile.get("sol_wallet")
        has_key = bool(profile.get("sol_burner_key"))
        if not w and not has_key:
            await update.message.reply_text(
                "**Sol burner wallet**\n"
                "Public only:\n`/wallet <sol_address>`\n\n"
                "Burner key (control on this bot):\n`/wallet key <base58_private_key>`\n\n"
                "Flow: fund burner → bot can use it → you send bags to real wallet.\n"
                "**Never** paste your main wallet seed or key.",
                parse_mode="Markdown",
            )
            return
        bal = await asyncio.to_thread(sol_rpc_balance, w) if w else None
        bal_s = f"{bal:.6f} SOL" if bal is not None else "n/a"
        buttons = []
        if w:
            buttons.append([
                InlineKeyboardButton("🔄 Refresh Balance", callback_data="wallet_balance"),
                InlineKeyboardButton("🔎 View on Solscan", url=f"https://solscan.io/account/{w}"),
            ])
        buttons.append([
            InlineKeyboardButton("🗑️ Clear Wallet", callback_data="wallet_clear_confirm"),
            InlineKeyboardButton("💸 Withdraw", callback_data="wallet_withdraw"),
        ])
        await update.message.reply_text(
            f"**Sol wallet**\\n"
            f"Address: `{w or '—'}`\\n"
            f"Key loaded: {'YES (burner)' if has_key else 'no (watch only)'}\\n"
            f"Balance: {bal_s}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons),
            disable_web_page_preview=True,
        )
        return

    action = args[0].lower()

    if action == "clear":
        update_user_profile(user_id, {"sol_wallet": None, "sol_burner_key": None})
        await update.message.reply_text("Sol address + burner key cleared.")
        return

    if action == "balance":
        w = profile.get("sol_wallet")
        if not w:
            await update.message.reply_text("No wallet set. /wallet key <burner_secret> or /wallet <pubkey>")
            return
        bal = await asyncio.to_thread(sol_rpc_balance, w)
        await update.message.reply_text(
            f"`{w}`\nBalance: **{(bal if bal is not None else 'n/a')}** SOL",
            parse_mode="Markdown",
        )
        return

    if action == "key":
        if len(args) < 2:
            await update.message.reply_text("Usage: /wallet key <base58_private_key>\nBurner only.")
            return
        secret = args[1].strip()
        # delete user message if possible (best-effort — hide key from chat history)
        try:
            await update.message.delete()
        except Exception:
            pass
        pub = sol_pubkey_from_secret(secret)
        if not pub:
            await update.message.reply_text(
                "Could not derive pubkey from that key.\n"
                "Install on VPS: `pip install solders` (or `base58 pynacl`)\n"
                "Or send a valid Solana base58 secret for a **burner**.",
            )
            return
        update_user_profile(user_id, {
            "sol_wallet": pub,
            "sol_burner_key": secret,
            "crypto_chain": "solana",
        })
        bal = await asyncio.to_thread(sol_rpc_balance, pub)
        bal_s = f"{bal:.6f} SOL" if bal is not None else "n/a"
        await update.message.reply_text(
            "**Burner key imported** (message with key deleted if allowed)\n"
            f"Address: `{pub}`\n"
            f"Balance: {bal_s}\n\n"
            "Bot can use this burner. Keep size small.\n"
            "Send profits to your real wallet yourself.\n"
            "**Never** import your main wallet.",
            parse_mode="Markdown",
        )
        return

    # public address only
    arg = args[0].strip()
    if len(arg) < 32:
        await update.message.reply_text("That does not look like a Solana address or use /wallet key <secret>")
        return
    update_user_profile(user_id, {"sol_wallet": arg, "crypto_chain": "solana"})
    # do not clear existing key if address matches; clear key if different address
    if profile.get("sol_burner_key"):
        derived = sol_pubkey_from_secret(profile.get("sol_burner_key") or "")
        if derived and derived != arg:
            update_user_profile(user_id, {"sol_burner_key": None})
    await update.message.reply_text(
        f"Public Sol wallet saved:\n`{arg}`\n"
        "Watch-only unless you also `/wallet key <burner_secret>`.",
        parse_mode="Markdown",
    )



async def norug_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    profile = get_user_profile(user_id)
    if context.args and context.args[0].lower() in ("off", "0", "false"):
        update_user_profile(user_id, {"no_rug_mode": False})
        await update.message.reply_text("no_rug_mode OFF — filters relaxed (more danger).")
        return
    if context.args and context.args[0].lower() in ("on", "1", "true"):
        update_user_profile(user_id, {"no_rug_mode": True})
        await update.message.reply_text("no_rug_mode ON — strict Sol safety gates.")
        return
    cur = profile.get("no_rug_mode", True)
    update_user_profile(user_id, {"no_rug_mode": not cur})
    await update.message.reply_text(f"no_rug_mode now {'ON' if not cur else 'OFF'}")


async def antimev_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    profile = get_user_profile(user_id)
    if context.args and context.args[0].lower() in ("off", "0", "false"):
        update_user_profile(user_id, {"anti_mev": False})
        await update.message.reply_text("anti_mev OFF")
        return
    if context.args and context.args[0].lower() in ("on", "1", "true"):
        update_user_profile(user_id, {"anti_mev": True})
        await update.message.reply_text("anti_mev ON — no chase vertical tape; priority fee advice on.")
        return
    cur = profile.get("anti_mev", True)
    update_user_profile(user_id, {"anti_mev": not cur})
    await update.message.reply_text(f"anti_mev now {'ON' if not cur else 'OFF'}")


async def incinerator_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profile = get_user_profile(update.effective_user.id)
    w = profile.get("sol_wallet") or ""
    msg = (
        "**Sol incinerator (reclaim rent)**\n"
        "Burns empty token accounts / recovers SOL rent.\n\n"
        "• https://sol-incinerator.com\n"
        "• https://www.solincinerator.com\n"
        "• Or Phantom/backpack account tools → close empty accounts\n\n"
        "Only connect a wallet you control. Never paste seed here.\n"
    )
    if w:
        msg += f"Your imported wallet: `{w}`\nhttps://solscan.io/account/{w}"
    await update.message.reply_text(msg, parse_mode="Markdown", disable_web_page_preview=True)


async def pumpcash_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    profile = get_user_profile(user_id)
    if context.args and context.args[0].lower() != "show":
        code = context.args[0].strip()
        update_user_profile(user_id, {"pump_cashback_code": code})
        await update.message.reply_text(
            f"Pump.fun cashback/ref code saved: `{code}`\n"
            "Use it on pump.fun / supported routing UIs that honor cashback.",
            parse_mode="Markdown",
        )
        return
    code = profile.get("pump_cashback_code")
    await update.message.reply_text(
        "**Pump.fun cashback**\n"
        "• Trade on https://pump.fun (Solana)\n"
        "• Save your code: `/pumpcash YOURCODE`\n"
        f"• Saved code: `{code or 'none'}`\n"
        "• Cashback depends on pump.fun / bot partner terms — claim in their UI\n"
        "• Bot does not custody funds; it only stores your code + Sol research tools",
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


async def pos_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sol positions: list / add / refresh / close"""
    user_id = update.effective_user.id
    profile = get_user_profile(user_id)
    args = context.args or []

    if not args or args[0].lower() in ("list", "show"):
        msg = list_positions_text(profile, refresh=True)
        update_user_profile(user_id, {"sol_positions": profile.get("sol_positions") or []})
        await update.message.reply_text(
            msg[:4000],
            parse_mode="Markdown",
            reply_markup=positions_keyboard(get_user_profile(user_id)),
            disable_web_page_preview=True,
        )
        return

    action = args[0].lower()

    if action == "refresh":
        msg = list_positions_text(profile, refresh=True)
        update_user_profile(user_id, {"sol_positions": profile.get("sol_positions") or []})
        await update.message.reply_text(
            msg[:4000],
            parse_mode="Markdown",
            reply_markup=positions_keyboard(get_user_profile(user_id)),
            disable_web_page_preview=True,
        )
        return

    if action == "add":
        if len(args) < 3:
            await update.message.reply_text("Usage: /pos add <token_mint> <amount> [entry_price]")
            return
        # Enforce the selected SOL preset's cap on tracked open positions.
        current_positions = list(profile.get("sol_positions") or [])
        open_positions = [
            p for p in current_positions
            if p.get("status", "open") == "open"
        ]
        risk_rules = get_risk_preset(effective_profile_risk(profile))
        max_positions = int(risk_rules.get("max_positions") or 4)

        if len(open_positions) >= max_positions:
            await update.message.reply_text(
                f"Position limit reached: {len(open_positions)}/{max_positions} "
                "open tracked SOL positions. Close a tracked position or adjust "
                "your SOL risk settings before adding another."
            )
            return

        token = args[1].strip()
        if not token:
            await update.message.reply_text("Token mint cannot be empty.")
            return

        try:
            amount = float(args[2])
            if amount <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Amount must be a number greater than zero.")
            return
        entry = None
        if len(args) >= 4:
            try:
                entry = float(args[3])
            except ValueError:
                await update.message.reply_text(
                    "Entry price must be a positive number."
                )
                return
            if entry <= 0:
                await update.message.reply_text(
                    "Entry price must be greater than zero."
                )
                return
        info = fetch_token_price_usd(token)
        sym = (info.get("symbol") if info else None) or token[:6]
        px = info.get("price") if info else None
        if entry is None:
            entry = px or 0
        import uuid
        pos = {
            "id": str(uuid.uuid4())[:8],
            "token": (info.get("token") if info else token) or token,
            "symbol": sym,
            "amount": amount,
            "entry": entry,
            "last_price": px,
            "url": (info.get("url") if info else "") or "",
            "status": "open",
            "opened_at": datetime.utcnow().isoformat(),
        }
        positions = list(profile.get("sol_positions") or [])
        positions.append(pos)
        update_user_profile(user_id, {"sol_positions": positions})
        await update.message.reply_text(
            f"Opened **{sym}** amt {amount} entry {entry}\\n`/pos` to view",
            parse_mode="Markdown",
        )
        return

    if action == "close":
        if len(args) < 2:
            await update.message.reply_text("Usage: /pos close <symbol|all>")
            return
        target = args[1].lower()
        positions = list(profile.get("sol_positions") or [])
        if target == "all":
            for p in positions:
                if p.get("status", "open") == "open":
                    p["status"] = "closed"
                    p["closed_at"] = datetime.utcnow().isoformat()
            update_user_profile(user_id, {"sol_positions": positions})
            await update.message.reply_text(
                "Closed **all** tracked positions. Sell on-chain on Jupiter if needed: https://jup.ag",
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
            return
        closed = 0
        for p in positions:
            if p.get("status", "open") != "open":
                continue
            if target in str(p.get("symbol", "")).lower() or target in str(p.get("token", "")).lower() or target == str(p.get("id")):
                p["status"] = "closed"
                p["closed_at"] = datetime.utcnow().isoformat()
                closed += 1
        update_user_profile(user_id, {"sol_positions": positions})
        await update.message.reply_text(
            f"Closed {closed} position(s) matching `{target}`. On-chain exit: https://jup.ag",
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
        return

    await update.message.reply_text(
        "Usage:\\n"
        "/pos — list (price high→low)\\n"
        "/pos refresh\\n"
        "/pos add <mint> <amount> [entry]\\n"
        "/pos close <symbol|all>"
    )


# ---------------------------------------------------------------------------
# Market opportunity scanner (technical screening only; never places trades)
# ---------------------------------------------------------------------------
SCANNER_UNIVERSES = {
    "stocks": {
        "NVDA": "NVIDIA", "AAPL": "Apple", "MSFT": "Microsoft", "AMD": "AMD",
        "TSLA": "Tesla", "META": "Meta", "AMZN": "Amazon", "GOOGL": "Alphabet",
        "SPY": "S&P 500 ETF", "QQQ": "Nasdaq 100 ETF", "IWM": "Russell 2000 ETF",
        "PLTR": "Palantir", "NFLX": "Netflix", "COIN": "Coinbase", "SMCI": "Super Micro",
    },
    "futures": {"ES=F": "E-mini S&P 500", "NQ=F": "E-mini Nasdaq 100", "YM=F": "E-mini Dow", "RTY=F": "E-mini Russell", "GC=F": "Gold", "CL=F": "Crude Oil"},
    "crypto": {"BTC-USD": "Bitcoin", "ETH-USD": "Ethereum", "SOL-USD": "Solana", "DOGE-USD": "Dogecoin"},
}


def _scan_one_symbol(symbol: str, label: str) -> Optional[Dict[str, Any]]:
    """Return a transparent 15-minute technical screen. Yahoo data may be delayed."""
    try:
        import yfinance as yf
        import pandas as pd
        df = yf.Ticker(symbol).history(period="5d", interval="15m", auto_adjust=False)
        if df is None or len(df) < 35:
            return None
        df = df.dropna(subset=["Open", "High", "Low", "Close"])
        if len(df) < 30:
            return None
        close = df["Close"].astype(float)
        high, low = df["High"].astype(float), df["Low"].astype(float)
        vol = df["Volume"].astype(float) if "Volume" in df else pd.Series(0.0, index=df.index)
        ema9, ema21 = close.ewm(span=9, adjust=False).mean(), close.ewm(span=21, adjust=False).mean()
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean().replace(0, float("nan"))
        rsi = 100 - (100 / (1 + gain / loss))
        prev_close = close.shift(1)
        tr = pd.concat([high-low, (high-prev_close).abs(), (low-prev_close).abs()], axis=1).max(axis=1)
        atr = float(tr.rolling(14).mean().iloc[-1])
        price = float(close.iloc[-1])
        if not (price > 0 and atr > 0):
            return None
        rsi_now = float(rsi.iloc[-1]) if pd.notna(rsi.iloc[-1]) else 50.0
        e9, e21 = float(ema9.iloc[-1]), float(ema21.iloc[-1])
        # Exclude the latest candle from the reference range so the current bar can break it.
        range_high = float(high.iloc[-21:-1].max())
        range_low = float(low.iloc[-21:-1].min())
        vol_now = float(vol.iloc[-1]) if len(vol) else 0.0
        vol_base = float(vol.iloc[-21:-1].median()) if len(vol) >= 21 else 0.0
        relvol = (vol_now / vol_base) if vol_base > 0 else 1.0
        recent_change = float((close.iloc[-1] / close.iloc[-5] - 1) * 100) if len(close) >= 5 else 0.0
        up_trend, down_trend = e9 > e21 and price > e21, e9 < e21 and price < e21
        breakout_up, breakout_down = price > range_high, price < range_low
        # Score ranks confluence; it is not a probability of profit.
        bull = 0
        bear = 0
        bull += 30 if up_trend else (10 if e9 > e21 else 0)
        bear += 30 if down_trend else (10 if e9 < e21 else 0)
        bull += 25 if breakout_up else (15 if price >= range_high - 0.25*atr else 0)
        bear += 25 if breakout_down else (15 if price <= range_low + 0.25*atr else 0)
        bull += 15 if 50 <= rsi_now <= 68 else (5 if 45 <= rsi_now < 50 else 0)
        bear += 15 if 32 <= rsi_now <= 50 else (5 if 50 < rsi_now <= 55 else 0)
        bull += 15 if relvol >= 1.5 else (8 if relvol >= 1.15 else 0)
        bear += 15 if relvol >= 1.5 else (8 if relvol >= 1.15 else 0)
        bull += 15 if recent_change > 0.15 else 0
        bear += 15 if recent_change < -0.15 else 0
        side = "LONG" if bull >= bear else "SHORT"
        score = min(100, bull if side == "LONG" else bear)
        # Only list actionable-looking screens; user still must confirm HTF/context and risk.
        if score < 55:
            return None
        entry = range_high + 0.05*atr if side == "LONG" else range_low - 0.05*atr
        stop = entry - 1.2*atr if side == "LONG" else entry + 1.2*atr
        target = entry + 2.4*atr if side == "LONG" else entry - 2.4*atr
        reason = []
        reason.append("15m EMA trend aligned" if (up_trend if side == "LONG" else down_trend) else "EMA alignment is not fully confirmed")
        reason.append("range breakout" if (breakout_up if side == "LONG" else breakout_down) else "near the recent range edge; wait for confirmation")
        reason.append(f"RSI {rsi_now:.0f}")
        if relvol >= 1.15:
            reason.append(f"relative volume {relvol:.1f}x median")
        return {"symbol": symbol, "label": label, "side": side, "score": score, "price": price,
                "atr": atr, "entry": entry, "stop": stop, "target": target, "rsi": rsi_now,
                "relvol": relvol, "reasons": reason, "breakout": breakout_up if side == "LONG" else breakout_down}
    except Exception as exc:
        log.debug("Scanner skipped %s: %s", symbol, exc)
        return None


def scan_market_opportunities(category: str = "stocks") -> str:
    category = (category or "stocks").lower()
    if category == "all":
        universe = {k: v for group in SCANNER_UNIVERSES.values() for k, v in group.items()}
    elif category in SCANNER_UNIVERSES:
        universe = SCANNER_UNIVERSES[category]
    else:
        return "Use `/scan stocks`, `/scan futures`, `/scan crypto`, or `/scan all`."
    results = []
    for symbol, label in universe.items():
        result = _scan_one_symbol(symbol, label)
        if result:
            results.append(result)
    results.sort(key=lambda x: x["score"], reverse=True)
    if not results:
        return (f"Opportunity Scanner — {category.upper()}\n\nNo qualifying 15-minute setups found or market data was unavailable. "
                "This is a valid result: do not force a trade. Try again during an active session.")
    lines = [f"OPPORTUNITY SCANNER — {category.upper()}",
             "15-minute technical screen • Yahoo Finance data may be delayed • score is confluence, NOT win probability.", ""]
    shown = 0
    for r in results:
        if shown >= 5:
            break
        strength = "STRONGER SETUP TO REVIEW" if r["score"] >= 75 else "WATCHLIST — needs confirmation"
        lines += [f"{shown+1}. {r['symbol']} — {r['label']} | {r['side']} | Score {r['score']}/100",
                  f"   {strength}",
                  f"   Current/reference price: {r['price']:.4g}",
                  f"   Plan: wait for a 15m close {'above' if r['side']=='LONG' else 'below'} {r['entry']:.4g}; don't chase if price runs away.",
                  f"   Example invalidation/stop: {r['stop']:.4g} | 2R target: {r['target']:.4g}",
                  f"   Evidence: {', '.join(r['reasons'])}.",
                  f"   Skip if: higher-timeframe bias conflicts, spread/liquidity is poor, news volatility is extreme, or stop size violates your risk rules.", ""]
        shown += 1
    lines += ["HOW TO USE IT", "1. Open the symbol on your chart and confirm 1H/4H bias and key liquidity levels.",
              "2. Wait for the stated 15m trigger/close; a scanner result alone is not an entry.",
              "3. Calculate position size from your allowed dollar risk and stop distance before entering.",
              "4. If confirmation is missing or risk/reward is poor, WAIT. No order is placed by this scanner."]
    return "\n".join(lines)


async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not user_is_licensed(user_id):
        await update.message.reply_text(f"Not licensed. ID: `{user_id}`", parse_mode="Markdown")
        return
    category = context.args[0].lower() if context.args else "stocks"
    if category not in {"stocks", "futures", "crypto", "all"}:
        await update.message.reply_text("Usage: /scan stocks | futures | crypto | all")
        return
    status = await update.message.reply_text(f"Scanning {category} for 15-minute setups…")
    try:
        result = await asyncio.to_thread(scan_market_opportunities, category)
        # Keep Telegram under its message limit and avoid Markdown parsing issues.
        for i in range(0, len(result), 3500):
            chunk = result[i:i+3500]
            if i == 0:
                await status.edit_text(chunk)
            else:
                await update.message.reply_text(chunk)
    except Exception as exc:
        log.exception("Opportunity scanner failed")
        await status.edit_text(f"Scanner failed: {type(exc).__name__}. Check yfinance/network logs and try again.")


async def stocks_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not user_is_licensed(user_id):
        await update.message.reply_text(f"Not licensed. ID: `{user_id}`", parse_mode="Markdown")
        return
    update_user_profile(user_id, {"last_desk": "stocks"})
    profile = get_user_profile(user_id)
    await update.message.reply_text(
        ui_header(profile, "stocks") + "\n**Stocks / Futures desk**",
        parse_mode="Markdown",
        reply_markup=hub_stocks_keyboard(profile),
    )


async def crypto_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not user_is_licensed(user_id):
        await update.message.reply_text(f"Not licensed. ID: `{user_id}`", parse_mode="Markdown")
        return
    update_user_profile(user_id, {"last_desk": "crypto"})
    profile = get_user_profile(user_id)
    await update.message.reply_text(
        await crypto_header_with_balance(profile) + "\n**Sol crypto desk**",
        parse_mode="Markdown",
        reply_markup=hub_crypto_keyboard(profile),
    )


async def toggle_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not user_is_licensed(user_id):
        await update.message.reply_text(f"Not licensed. ID: `{user_id}`", parse_mode="Markdown")
        return
    profile = get_user_profile(user_id)
    last = profile.get("last_desk") or "stocks"
    if last == "crypto":
        update_user_profile(user_id, {"last_desk": "stocks"})
        profile = get_user_profile(user_id)
        await update.message.reply_text(
            ui_header(profile, "stocks") + "\nSwitched to STOCKS / FUTURES.",
            parse_mode="Markdown",
            reply_markup=hub_stocks_keyboard(profile),
        )
    else:
        update_user_profile(user_id, {"last_desk": "crypto"})
        profile = get_user_profile(user_id)
        await update.message.reply_text(
            await crypto_header_with_balance(profile) + "\nSwitched to SOL CRYPTO.",
            parse_mode="Markdown",
            reply_markup=hub_crypto_keyboard(profile),
        )


async def account_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Change / list Tradeify accounts (same as Change account button)."""
    user_id = update.effective_user.id
    if not user_is_licensed(user_id):
        await update.message.reply_text(f"Not licensed. ID: `{user_id}`", parse_mode="Markdown")
        return
    profile = get_user_profile(user_id)
    accounts = profile.get("accounts") or {}
    rows = []
    for key in sorted(accounts.keys()):
        mark = " (active)" if key == profile.get("active_account") else ""
        rows.append([
            InlineKeyboardButton("👤 " + key + mark, callback_data="acct_pick_" + key),
            InlineKeyboardButton("🗑️ Delete", callback_data="acct_del_pick_" + key),
        ])
    rows.append([InlineKeyboardButton("Add new size/plan", callback_data="acct_new")])
    rows.append([InlineKeyboardButton("🏠 Home", callback_data="menu")])
    await update.message.reply_text(
        ui_header(profile, "home") + "\n**Change Tradeify account**\nTap an account to review a switch confirmation, use 🗑️ Delete to remove its saved profile, or add a new size/plan.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )

async def _start_scheduler(application: Application):
    scheduler = AsyncIOScheduler(timezone=NY_TZ)
    scheduler.add_job(
        send_daily_open_report,
        CronTrigger(day_of_week="mon-fri", hour=9, minute=25, timezone=NY_TZ),
        args=[application], id="daily_open_report", replace_existing=True,
    )
    scheduler.add_job(
        send_hourly_opportunities,
        CronTrigger(day_of_week="mon-fri", hour="10-15", minute=0, timezone=NY_TZ),
        args=[application], id="hourly_opportunities", replace_existing=True,
    )
    for h, m, jid in [(3, 0, "london_open"), (9, 25, "ny_prep"), (9, 30, "ny_open"), (15, 0, "power_hour"), (16, 45, "flatten_warning")]:
        scheduler.add_job(
            send_session_transition_alert,
            CronTrigger(day_of_week="mon-fri", hour=h, minute=m, timezone=NY_TZ),
            args=[application], id=jid, replace_existing=True,
        )
    scheduler.start()
    application.bot_data["brazy_scheduler"] = scheduler
    log.info("Brazy scheduler started (%d jobs)", len(scheduler.get_jobs()))


async def _stop_scheduler(application: Application):
    scheduler = application.bot_data.get("brazy_scheduler")
    if scheduler and scheduler.running:
        scheduler.shutdown(wait=False)
        log.info("Brazy scheduler stopped")



async def telegram_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log unexpected update errors and give the affected user a visible failure notice."""
    error = context.error
    log.error("Unhandled Telegram update error: %s", error, exc_info=(type(error), error, error.__traceback__) if error else None)
    try:
        if isinstance(update, Update) and update.effective_message is not None:
            await update.effective_message.reply_text(
                "That action failed unexpectedly. The error was logged; please try again. "
                + (str(error)[:300] if error else "")
            )
        elif isinstance(update, Update) and update.callback_query is not None:
            await update.callback_query.answer("Action failed. Please try again.", show_alert=True)
    except Exception:
        log.exception("Could not deliver the update-error notice")


def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing. Set it in /opt/brazy/.env before starting.")
    if not OWNER_IDS and not LICENSE_LIST_CONFIGURED:
        raise RuntimeError("Access is locked by default. Set TELEGRAM_OWNER_IDS in .env to your numeric Telegram ID.")

    app = (Application.builder()
           .token(TELEGRAM_TOKEN)
           .post_init(_start_scheduler)
           .post_shutdown(_stop_scheduler)
           .build())

    app.add_handler(TypeHandler(Update, authorization_gate), group=-1)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stocks", stocks_command))
    app.add_handler(CommandHandler("scan", scan_command))
    app.add_handler(CommandHandler("opportunity", scan_command))
    app.add_handler(CommandHandler("crypto", crypto_command))
    app.add_handler(CommandHandler("toggle", toggle_command))
    app.add_handler(CommandHandler("account", account_command))
    app.add_handler(CommandHandler("bias", bias_command))
    app.add_handler(CommandHandler("log", log_command))
    app.add_handler(CommandHandler("alert", alert_command))
    app.add_handler(CommandHandler("risk", risk_command))
    app.add_handler(CommandHandler("rules", rules_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("notes", notes_command))
    app.add_handler(CommandHandler("checklist", checklist_command))
    app.add_handler(CommandHandler("session", session_command))
    app.add_handler(CommandHandler("target", target_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("performance", performance_command))
    app.add_handler(CommandHandler("sentiment", sentiment_command))
    app.add_handler(CommandHandler("asset", asset_command))
    app.add_handler(CommandHandler("phase", phase_command))
    app.add_handler(CommandHandler("progress", progress_command))
    app.add_handler(CommandHandler("debriefs", debriefs_command))
    app.add_handler(CommandHandler("paper", paper_command))
    app.add_handler(CommandHandler("cryptoscan", cryptoscan_command))
    app.add_handler(CommandHandler("snipe", snipe_command))
    app.add_handler(CommandHandler("copyfind", copyfind_command))
    app.add_handler(CommandHandler("fomo", fomo_command))
    app.add_handler(CommandHandler("pumpcash", pumpcash_command))
    app.add_handler(CommandHandler("pos", pos_command))
    app.add_handler(CommandHandler("deposit", deposit_command))
    app.add_handler(CommandHandler("withdraw", deposit_command))
    app.add_handler(CommandHandler("incinerator", incinerator_command))
    app.add_handler(CommandHandler("antimev", antimev_command))
    app.add_handler(CommandHandler("norug", norug_command))
    app.add_handler(CommandHandler("wallet", wallet_command))
    app.add_handler(CommandHandler("kolscan", kolscan_command))
    app.add_handler(CommandHandler("copylist", copylist_command))
    app.add_handler(CommandHandler("copyrm", copyrm_command))
    app.add_handler(CommandHandler("copyadd", copyadd_command))
    app.add_handler(CommandHandler("riskpreset", riskpreset_command))
    app.add_handler(CommandHandler("mode", mode_command))
    app.add_handler(CommandHandler("customrule", customrule_command))
    app.add_handler(CommandHandler("customrules", customrules_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(telegram_error_handler)

    log.info("Brazy Bot starting with daily + hourly reports...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
