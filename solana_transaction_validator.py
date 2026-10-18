"""Fail-closed structural checks for unsigned Jupiter transactions.

This module never signs, broadcasts, or executes transactions.
It is a preliminary validation gate, not a complete swap authorization engine.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from solana_lookup_indexing import resolve_lookup_keys


SYSTEM_PROGRAM = "11111111111111111111111111111111"
COMPUTE_BUDGET_PROGRAM = "ComputeBudget111111111111111111111111111111"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ASSOCIATED_TOKEN_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
JUPITER_V6_PROGRAM = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"

# Explicitly limited starter set. Unknown programs are rejected.
# Do not treat inclusion here as proof that instruction data is safe.
ALLOWED_PROGRAMS = {
    SYSTEM_PROGRAM,
    COMPUTE_BUDGET_PROGRAM,
    TOKEN_PROGRAM,
    TOKEN_2022_PROGRAM,
    ASSOCIATED_TOKEN_PROGRAM,
    JUPITER_V6_PROGRAM,
}

SOL_MINT = "So11111111111111111111111111111111111111112"


@dataclass(frozen=True)
class ValidationResult:
    accepted: bool
    reasons: tuple[str, ...]
    transaction_version: str | None = None
    instruction_count: int = 0
    static_account_count: int = 0


def _fail(*reasons: str) -> ValidationResult:
    return ValidationResult(False, tuple(reasons))


def validate_unsigned_order(
    *,
    transaction_b64: str,
    expected_taker: str,
    expected_input_mint: str,
    expected_output_mint: str,
    expected_input_amount: int,
    order: dict[str, Any],
    resolved_lookup_addresses: dict[str, list[str]] | None = None,
) -> ValidationResult:
    """Check an unsigned Jupiter transaction against its requested order.

    `resolved_lookup_addresses` must map each lookup-table address to its
    complete ordered list of addresses fetched from a trusted Solana RPC.
    This function verifies index resolution, not the RPC's authenticity.
    """
    errors: list[str] = []

    try:
        taker = str(Pubkey.from_string(expected_taker))
        input_mint = str(Pubkey.from_string(expected_input_mint))
        output_mint = str(Pubkey.from_string(expected_output_mint))
        if expected_input_amount <= 0:
            return _fail("Expected input amount must be positive.")
        if input_mint == output_mint:
            return _fail("Input and output mints must differ.")
    except Exception:
        return _fail("Invalid expected wallet, mint, or amount.")

    if not isinstance(order, dict):
        return _fail("Order response is not an object.")

    # Require the order response to match caller expectations exactly.
    if order.get("inputMint") != input_mint:
        errors.append("Order input mint does not match expectation.")
    if order.get("outputMint") != output_mint:
        errors.append("Order output mint does not match expectation.")
    try:
        if int(order.get("inAmount", -1)) != expected_input_amount:
            errors.append("Order input amount does not match expectation.")
    except (TypeError, ValueError):
        errors.append("Order input amount is missing or invalid.")

    if not order.get("requestId"):
        errors.append("Jupiter request ID is missing.")
    if not order.get("outAmount"):
        errors.append("Quoted output amount is missing.")
    if not order.get("transaction"):
        errors.append("Jupiter transaction is missing.")
    elif order.get("transaction") != transaction_b64:
        errors.append("Supplied transaction does not match the Jupiter order transaction.")

    if errors:
        return _fail(*errors)

    try:
        raw = base64.b64decode(transaction_b64, validate=True)
        tx = VersionedTransaction.from_bytes(raw)
        tx.sanitize()
    except Exception:
        return _fail("Transaction is not valid serialized versioned transaction data.")

    message = tx.message
    static_keys = [str(key) for key in message.account_keys]
    required_signers = message.header.num_required_signatures

    if required_signers < 1 or required_signers > len(static_keys):
        errors.append("Invalid required signer count.")
    elif static_keys[0] != taker:
        errors.append("Expected taker is not the first required signer.")

    lookup_map = resolved_lookup_addresses or {}
    lookups = list(getattr(message, "address_table_lookups", []))
    writable_lookup_keys, readonly_lookup_keys, lookup_errors = resolve_lookup_keys(
        lookups, lookup_map
    )
    errors.extend(lookup_errors)
    all_keys = static_keys + writable_lookup_keys + readonly_lookup_keys

    for ix_number, ix in enumerate(message.instructions, start=1):
        if ix.program_id_index >= len(all_keys):
            errors.append(f"Instruction {ix_number}: unresolved program index.")
            continue
        program_id = all_keys[ix.program_id_index]
        if program_id not in ALLOWED_PROGRAMS:
            errors.append(
                f"Instruction {ix_number}: program is not in starter allowlist: "
                f"{program_id}"
            )
        if any(index >= len(all_keys) for index in ix.accounts):
            errors.append(f"Instruction {ix_number}: unresolved account index.")

    # This preliminary module does not decode Jupiter swap instruction data,
    # verify the actual minimum-output semantics, authenticate RPC lookup-table
    # data, or establish that the quote is profitable. Therefore it never
    # grants live-execution authorization.
    errors.append(
        "Structural checks only: complete instruction decoding and simulation "
        "are still required before any signing authorization."
    )

    return ValidationResult(
        accepted=False,
        reasons=tuple(errors),
        transaction_version=str(tx.version()),
        instruction_count=len(message.instructions),
        static_account_count=len(static_keys),
    )
