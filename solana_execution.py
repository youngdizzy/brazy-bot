"""Brazy Bot crypto risk engine. No live trading or wallet access."""

import json
import os
import time
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path
from dotenv import load_dotenv

# Read the same configuration file as the main bot.
load_dotenv(Path("/opt/brazy/.env"), override=False)

DATA_DIR = Path("/opt/brazy/data")
STATE_FILE = DATA_DIR / "solana_execution_state.json"

# Fail-closed configuration: invalid values stop startup.
def _env_bool(name, default="false"):
    raw = os.getenv(name, default).strip().lower()
    if raw not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false")
    return raw == "true"


def _env_decimal(name, default):
    try:
        value = Decimal(str(os.getenv(name, default)))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"Invalid {name}.") from exc
    if not value.is_finite():
        raise ValueError(f"Invalid {name}.")
    return value


def _env_int(name, default):
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {name}.") from exc


# These flags are configuration only; execution remains disabled.
LIVE_TRADING_ENABLED = _env_bool("SOL_LIVE_TRADING", "false")
REQUIRE_ORDER_VALIDATION = _env_bool("SOL_REQUIRE_ORDER_VALIDATION", "true")
REQUIRE_SIMULATION = _env_bool("SOL_REQUIRE_SIMULATION", "true")

# Conservative limits; SOL amounts are denominated in SOL.
MAX_BUY_SOL = _env_decimal("SOL_MAX_BUY", "0.001")
MAX_DAILY_LOSS_SOL = _env_decimal("SOL_MAX_DAILY_LOSS", "0.003")
MAX_OPEN_POSITIONS = _env_int("SOL_MAX_OPEN_POSITIONS", "1")
MAX_SLIPPAGE_BPS = _env_int("SOL_MAX_SLIPPAGE_BPS", "50")
MAX_PRICE_IMPACT_PCT = _env_decimal("SOL_MAX_PRICE_IMPACT_PCT", "1.0")
MIN_REWARD_RISK = _env_decimal("SOL_MIN_REWARD_RISK", "1.5")

if MAX_BUY_SOL <= 0 or MAX_DAILY_LOSS_SOL <= 0:
    raise ValueError("SOL buy and daily-loss limits must be positive")
if MAX_OPEN_POSITIONS < 1:
    raise ValueError("SOL_MAX_OPEN_POSITIONS must be at least 1")
if not 1 <= MAX_SLIPPAGE_BPS <= 100:
    raise ValueError("SOL_MAX_SLIPPAGE_BPS must be between 1 and 100")
if MAX_PRICE_IMPACT_PCT <= 0 or MIN_REWARD_RISK < Decimal("1.5"):
    raise ValueError("Invalid Solana impact or reward/risk limit")


def _decimal(value, name):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"Invalid {name}.")
    if not result.is_finite():
        raise ValueError(f"Invalid {name}.")
    return result


def _today():
    # UTC day boundary for consistent daily risk accounting.
    return time.strftime("%Y-%m-%d", time.gmtime())


def _load():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not STATE_FILE.exists():
        return {"version": 1, "day": _today(), "daily_realized_pnl_sol": "0", "positions": {}}

    try:
        state = json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Cannot read risk state safely: {exc}. Do not delete the state file."
        ) from exc

    if state.get("version") != 1 or not isinstance(state.get("positions"), dict):
        raise RuntimeError("Risk state schema is invalid; refusing to continue.")

    if state.get("day") != _today():
        state["day"] = _today()
        state["daily_realized_pnl_sol"] = "0"

    return state


def _save(state):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATE_FILE)
    os.chmod(STATE_FILE, 0o600)


def check_order(amount_sol, slippage_bps, price_impact_pct, planned_reward_risk):
    amount = _decimal(amount_sol, "SOL amount")
    impact = _decimal(price_impact_pct, "price impact")
    rr = _decimal(planned_reward_risk, "reward-to-risk")

    if amount <= 0 or amount > MAX_BUY_SOL:
        return False, "Buy amount is outside the configured limit."
    if not isinstance(slippage_bps, int) or isinstance(slippage_bps, bool):
        return False, "Slippage must be an integer."
    if not 1 <= slippage_bps <= MAX_SLIPPAGE_BPS:
        return False, "Slippage exceeds the configured limit."
    if impact < 0 or impact > MAX_PRICE_IMPACT_PCT:
        return False, "Price impact is outside the configured limit."
    if rr < MIN_REWARD_RISK:
        return False, "Planned reward-to-risk is below 1.5:1."

    state = _load()
    pnl = _decimal(state.get("daily_realized_pnl_sol", "0"), "daily PnL")
    if pnl <= -MAX_DAILY_LOSS_SOL:
        return False, "Daily realized-loss limit reached."

    if len(state["positions"]) >= MAX_OPEN_POSITIONS:
        return False, "Maximum open-position limit reached."

    return True, "Preliminary risk checks passed; no order was placed."


def open_position(mint, entry_sol, token_amount, stop_price, target_price):
    """Record a proposed/open position only after a caller verifies a real fill."""
    if not isinstance(mint, str) or not mint.strip():
        raise ValueError("Token mint is required.")
    if mint in ("SOL", "USDC"):
        raise ValueError("Use a valid token mint address.")

    entry = _decimal(entry_sol, "entry SOL")
    tokens = _decimal(token_amount, "token amount")
    stop = _decimal(stop_price, "stop price")
    target = _decimal(target_price, "target price")

    if entry <= 0 or entry > MAX_BUY_SOL or tokens <= 0:
        raise ValueError("Entry or token amount is outside the permitted range.")
    if stop <= 0 or target <= stop:
        raise ValueError("Invalid stop/target relationship.")

    state = _load()
    if mint in state["positions"]:
        raise RuntimeError("Position already exists for this mint.")
    if len(state["positions"]) >= MAX_OPEN_POSITIONS:
        raise RuntimeError("Maximum open-position limit reached.")

    position_id = str(uuid.uuid4())
    state["positions"][mint] = {
        "position_id": position_id,
        "mint": mint,
        "entry_sol": str(entry),
        "token_amount": str(tokens),
        "stop_price": str(stop),
        "target_price": str(target),
        "status": "recorded",
        "created_at_utc": int(time.time()),
        "note": "Record only; on-chain fill must be verified by the caller.",
    }
    _save(state)
    return position_id


def close_position(mint, realized_pnl_sol, verified_exit_signature=None):
    """Record a verified exit and realized PnL; does not submit a sell."""
    if not verified_exit_signature:
        raise ValueError("Verified on-chain exit signature is required.")
    pnl = _decimal(realized_pnl_sol, "realized PnL")

    state = _load()
    if mint not in state["positions"]:
        raise KeyError("No tracked position for this mint.")

    del state["positions"][mint]
    state["daily_realized_pnl_sol"] = str(
        _decimal(state.get("daily_realized_pnl_sol", "0"), "daily PnL") + pnl
    )
    _save(state)


def status():
    state = _load()
    return {
        "day_utc": state["day"],
        "daily_realized_pnl_sol": state["daily_realized_pnl_sol"],
        "open_positions": len(state["positions"]),
        "positions": state["positions"],
    }


if __name__ == "__main__":
    print("Risk engine status:", json.dumps(status(), indent=2))
    print("Live trading: DISABLED")
    print("Wallet key access: DISABLED")
    print("Transaction signing/submission: DISABLED")
