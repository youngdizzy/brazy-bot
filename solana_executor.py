"""Brazy Bot Solana order preflight.

This module requests and inspects Jupiter orders and simulates unsigned
transactions. It does not sign, broadcast, or authorize real trades.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from typing import Any

import requests
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction


JUPITER_ORDER_URL = "https://api.jup.ag/swap/v2/order"
RPC_URL = "https://api.mainnet-beta.solana.com"

SOL_MINT = "So11111111111111111111111111111111111111112"
MAX_BUY_LAMPORTS = 1_000_000  # 0.001 SOL initial test cap
REQUEST_TIMEOUT = 20
SIMULATION_TIMEOUT = 30


@dataclass(frozen=True)
class OrderCheck:
    ok: bool
    reasons: tuple[str, ...]
    wallet: str | None = None
    input_amount: int | None = None
    quoted_output: int | None = None
    transaction_version: str | None = None
    instruction_count: int = 0


def _jupiter_headers() -> dict[str, str]:
    """Read the API key without exposing it."""
    key = os.getenv("JUPITER_API_KEY", "").strip()
    if not key:
        raise RuntimeError("JUPITER_API_KEY is missing.")
    return {"x-api-key": key}


def _load_keypair(secret: str) -> Keypair:
    if not isinstance(secret, str) or not secret.strip():
        raise ValueError("Wallet key is missing.")
    try:
        return Keypair.from_base58_string(secret.strip())
    except Exception:
        raise ValueError("Wallet key is invalid.") from None


def _decode_transaction(transaction_b64: str) -> VersionedTransaction:
    if not isinstance(transaction_b64, str) or not transaction_b64:
        raise ValueError("Serialized transaction is missing.")
    raw = base64.b64decode(transaction_b64, validate=True)
    tx = VersionedTransaction.from_bytes(raw)
    tx.sanitize()
    return tx


def inspect_buy_order(
    *,
    secret: str,
    output_mint: str,
    amount_lamports: int,
    slippage_bps: int = 100,
) -> tuple[OrderCheck, dict[str, Any] | None]:
    """Request and structurally inspect a Jupiter SOL-to-token order.

    Passing these checks never authorizes signing or trading.
    """
    reasons: list[str] = []
    wallet_address: str | None = None
    output_amount = 0
    tx: VersionedTransaction | None = None

    if isinstance(amount_lamports, bool) or not isinstance(amount_lamports, int):
        return OrderCheck(False, ("Input amount must be an integer.",)), None

    if not 1 <= amount_lamports <= MAX_BUY_LAMPORTS:
        return OrderCheck(
            False, ("Input amount exceeds the initial test cap.",)
        ), None

    if isinstance(slippage_bps, bool) or not isinstance(slippage_bps, int):
        return OrderCheck(False, ("Slippage must be an integer.",)), None

    if not 1 <= slippage_bps <= 100:
        return OrderCheck(
            False, ("Slippage must be between 1 and 100 bps.",)
        ), None

    try:
        wallet = _load_keypair(secret)
        wallet_address = str(wallet.pubkey())
        output_address = str(Pubkey.from_string(output_mint))
    except Exception as exc:
        return OrderCheck(False, (str(exc),)), None

    if output_address == SOL_MINT:
        return OrderCheck(
            False, ("Output mint must differ from SOL.",)
        ), None

    try:
        response = requests.get(
            JUPITER_ORDER_URL,
            params={
                "inputMint": SOL_MINT,
                "outputMint": output_address,
                "amount": str(amount_lamports),
                "taker": wallet_address,
                "slippageBps": str(slippage_bps),
            },
            headers=_jupiter_headers(),
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        order = response.json()
    except Exception:
        return OrderCheck(
            False, ("Jupiter order request failed.",)
        ), None

    if not isinstance(order, dict):
        return OrderCheck(
            False, ("Jupiter response was not an object.",)
        ), None

    if order.get("inputMint") != SOL_MINT:
        reasons.append("Order input mint mismatch.")

    if order.get("outputMint") != output_address:
        reasons.append("Order output mint mismatch.")

    try:
        if int(order.get("inAmount", -1)) != amount_lamports:
            reasons.append("Order input amount mismatch.")

        output_amount = int(order.get("outAmount", 0))
        if output_amount <= 0:
            reasons.append("Quoted output amount is missing or invalid.")
    except (TypeError, ValueError, OverflowError):
        output_amount = 0
        reasons.append("Order amounts are invalid.")

    if not isinstance(order.get("requestId"), str) or not order["requestId"]:
        reasons.append("Jupiter request ID is missing.")

    if not isinstance(order.get("transaction"), str) or not order["transaction"]:
        reasons.append("Serialized transaction is missing.")

    if not reasons:
        try:
            tx = _decode_transaction(order["transaction"])
        except Exception:
            reasons.append("Transaction serialization is invalid.")

    if tx is not None:
        try:
            required = tx.message.header.num_required_signatures
            static_keys = [str(k) for k in tx.message.account_keys]
            if (
                required < 1
                or not static_keys
                or static_keys[0] != wallet_address
            ):
                reasons.append(
                    "Transaction does not require the supplied wallet "
                    "as its first signer."
                )
        except Exception:
            reasons.append("Transaction signer inspection failed.")

    reasons.append(
        "NOT AUTHORIZED: full instruction, token-account, lookup-table, "
        "economic-limit, and execution checks are not complete."
    )

    check = OrderCheck(
        ok=False,
        reasons=tuple(reasons),
        wallet=wallet_address,
        input_amount=amount_lamports,
        quoted_output=output_amount,
        transaction_version=str(tx.version()) if tx is not None else None,
        instruction_count=(
            len(tx.message.instructions) if tx is not None else 0
        ),
    )
    return check, order


def simulate_unsigned_transaction(transaction_b64: str) -> dict[str, Any]:
    """Simulate an unsigned transaction; never signs or broadcasts it."""
    try:
        tx = _decode_transaction(transaction_b64)
        encoded = base64.b64encode(bytes(tx)).decode("ascii")
    except Exception:
        return {
            "ok": False,
            "error": "Invalid serialized transaction.",
            "authorization": "NOT_AUTHORIZED",
        }

    try:
        response = requests.post(
            RPC_URL,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "simulateTransaction",
                "params": [
                    encoded,
                    {
                        "encoding": "base64",
                        "sigVerify": False,
                        "replaceRecentBlockhash": True,
                        "commitment": "processed",
                    },
                ],
            },
            timeout=SIMULATION_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return {
            "ok": False,
            "error": "Simulation RPC request failed.",
            "authorization": "NOT_AUTHORIZED",
        }

    if not isinstance(payload, dict) or payload.get("error"):
        return {
            "ok": False,
            "error": "Simulation RPC returned an error.",
            "authorization": "NOT_AUTHORIZED",
        }

    value = (payload.get("result") or {}).get("value") or {}
    if not isinstance(value, dict):
        return {
            "ok": False,
            "error": "Simulation response was malformed.",
            "authorization": "NOT_AUTHORIZED",
        }

    simulation_error = value.get("err")
    return {
        "ok": simulation_error is None,
        "simulation_error": simulation_error,
        "compute_units": value.get("unitsConsumed"),
        "authorization": "NOT_AUTHORIZED",
    }


def inspect_buy_order_details(
    *,
    secret: str,
    output_mint: str,
    amount_lamports: int,
    slippage_bps: int = 100,
) -> dict[str, Any]:
    """Run read-only order inspection, instruction inspection, and simulation."""
    check, order = inspect_buy_order(
        secret=secret,
        output_mint=output_mint,
        amount_lamports=amount_lamports,
        slippage_bps=slippage_bps,
    )

    result: dict[str, Any] = {
        "order_ok": check.ok,
        "order_reasons": list(check.reasons),
        "wallet": check.wallet,
        "input_amount": check.input_amount,
        "quoted_output": check.quoted_output,
        "transaction_version": check.transaction_version,
        "instruction_count": check.instruction_count,
        "inspection": None,
        "simulation": None,
        "authorization": "NOT_AUTHORIZED",
    }

    if not isinstance(order, dict):
        result["inspection_error"] = "No usable Jupiter order was returned."
        return result

    transaction_b64 = order.get("transaction")
    if not isinstance(transaction_b64, str) or not transaction_b64:
        result["inspection_error"] = "Order contains no serialized transaction."
        return result

    try:
        from solana_instruction_inspector import inspect_transaction_from_rpc

        result["inspection"] = inspect_transaction_from_rpc(transaction_b64)
    except Exception:
        result["inspection_error"] = "Read-only instruction inspection failed."

    result["simulation"] = simulate_unsigned_transaction(transaction_b64)
    result["authorization"] = "NOT_AUTHORIZED"
    return result


def live_execution_enabled() -> bool:
    """Read the feature flag; execution is not implemented in this module."""
    return os.getenv("SOL_LIVE_TRADING", "false").strip().lower() in {
        "1", "true", "yes", "on"
    }


if __name__ == "__main__":
    print("Brazy executor module loaded.")
    print("Live execution enabled:", live_execution_enabled())
    print("Signing implemented: NO")
    print("Broadcast implemented: NO")
    print("Trade authorization: NOT_AUTHORIZED")
