"""Fail-closed Solana transaction instruction decoder for Brazy Bot.

This module is for inspection only. It does NOT authorize, sign, or broadcast
transactions. Unknown program instructions remain unknown and require rejection
by any future authorization layer.
"""
from __future__ import annotations

import base64
import struct
from typing import Any, Sequence

from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

SYSTEM_PROGRAM = "11111111111111111111111111111111"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ASSOCIATED_TOKEN_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsYV1j3"
COMPUTE_BUDGET_PROGRAM = "ComputeBudget111111111111111111111111111111"
MEMO_PROGRAMS = {
    "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",
    "Memo1UhkJRfHyvLMcVucJwxXeuD728EqVDDwQDxFMNo",
}

# Solana SystemInstruction enum variant indices.
SYSTEM_TAGS = {
    0: "CreateAccount", 1: "Assign", 2: "Transfer",
    3: "CreateAccountWithSeed", 4: "AdvanceNonceAccount",
    5: "WithdrawNonceAccount", 6: "InitializeNonceAccount",
    7: "AuthorizeNonceAccount", 8: "Allocate",
    9: "AllocateWithSeed", 10: "AssignWithSeed",
    11: "TransferWithSeed", 12: "UpgradeNonceAccount",
}
# SPL Token instruction opcodes (also broadly shared by Token-2022).
TOKEN_TAGS = {
    0: "InitializeMint", 1: "InitializeAccount", 2: "InitializeMultisig",
    3: "Transfer", 4: "Approve", 5: "Revoke", 6: "SetAuthority",
    7: "MintTo", 8: "Burn", 9: "CloseAccount",
    10: "FreezeAccount", 11: "ThawAccount", 12: "TransferChecked",
    13: "ApproveChecked", 14: "MintToChecked", 15: "BurnChecked",
    16: "InitializeAccount2", 17: "SyncNative", 18: "InitializeAccount3",
    19: "InitializeMultisig2", 20: "InitializeMint2",
    21: "GetAccountDataSize", 22: "InitializeImmutableOwner",
    23: "AmountToUiAmount", 24: "UiAmountToAmount",
}
TOKEN_RISKY = {
    "Transfer", "TransferChecked", "Approve", "ApproveChecked", "Revoke",
    "SetAuthority", "MintTo", "MintToChecked", "Burn", "BurnChecked",
    "CloseAccount", "FreezeAccount", "ThawAccount",
}
COMPUTE_TAGS = {
    0: "RequestHeapFrame", 1: "SetComputeUnitLimit",
    2: "SetComputeUnitPrice", 3: "SetLoadedAccountsDataSizeLimit",
}


def _b58_pubkey(value: Any) -> str:
    try:
        return str(Pubkey.from_string(str(value)))
    except Exception:
        return str(value)


def _decode_system(data: bytes) -> dict[str, Any]:
    if len(data) < 4:
        return {"kind": "MALFORMED", "risk": "REJECT"}
    tag = struct.unpack("<I", data[:4])[0]
    name = SYSTEM_TAGS.get(tag, "UNKNOWN_SYSTEM_INSTRUCTION")
    result: dict[str, Any] = {"kind": name}
    if tag == 2:
        if len(data) != 12:
            return {"kind": "MALFORMED_TRANSFER", "risk": "REJECT"}
        result["lamports"] = struct.unpack("<Q", data[4:12])[0]
        result["risk"] = "VALUE_TRANSFER"
    elif tag in (0, 3, 5, 11):
        # Account creation, nonce withdrawal, and seeded transfer require
        # full variant-specific parsing; never infer safe semantics here.
        result["risk"] = "REVIEW_REQUIRED"
    elif tag in (1, 4, 6, 7, 8, 9, 10, 12):
        result["risk"] = "STATE_CHANGE_OR_NONTRANSFER"
    else:
        result["risk"] = "UNKNOWN"
    return result


def _decode_token(data: bytes) -> dict[str, Any]:
    if not data:
        return {"kind": "MALFORMED", "risk": "REJECT"}
    tag = data[0]
    name = TOKEN_TAGS.get(tag, "UNKNOWN_TOKEN_INSTRUCTION")
    result: dict[str, Any] = {"kind": name}
    if tag in (3, 4, 7, 8):
        if len(data) != 9:
            return {"kind": "MALFORMED_TOKEN_INSTRUCTION", "risk": "REJECT"}
        result["amount_base_units"] = struct.unpack("<Q", data[1:9])[0]
    elif tag in (12, 13, 14, 15):
        if len(data) < 10:
            return {"kind": "MALFORMED_TOKEN_INSTRUCTION", "risk": "REJECT"}
        result["amount_base_units"] = struct.unpack("<Q", data[1:9])[0]
        result["decimals"] = data[9]
    elif tag == 6:
        # SetAuthority has a variable authority option and authority type.
        if len(data) < 3:
            return {"kind": "MALFORMED_TOKEN_INSTRUCTION", "risk": "REJECT"}
    if name == "UNKNOWN_TOKEN_INSTRUCTION":
        result["risk"] = "UNKNOWN"
    else:
        result["risk"] = "VALUE_OR_AUTHORITY_CHANGE" if name in TOKEN_RISKY else (
            "INITIALIZATION_OR_ACCOUNT_OPERATION"
        )
    return result


def _decode_compute(data: bytes) -> dict[str, Any]:
    if not data:
        return {"kind": "MALFORMED", "risk": "REJECT"}
    tag = data[0]
    name = COMPUTE_TAGS.get(tag, "UNKNOWN_COMPUTE_BUDGET_INSTRUCTION")
    result: dict[str, Any] = {"kind": name}
    if tag == 1 and len(data) == 5:
        result["compute_unit_limit"] = struct.unpack("<I", data[1:5])[0]
    elif tag == 2 and len(data) == 9:
        result["micro_lamports_per_compute_unit"] = struct.unpack("<Q", data[1:9])[0]
    elif tag in (1, 2) and len(data) not in (5, 9):
        return {"kind": "MALFORMED_COMPUTE_BUDGET_INSTRUCTION", "risk": "REJECT"}
    result["risk"] = "COMPUTE_LIMIT_OR_FEE"
    return result


def decode_transaction_instructions(
    transaction_b64: str,
    resolved_account_keys: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Decode known outer instructions. Unknowns and unresolved ALTs fail closed.

    resolved_account_keys, when supplied for a v0 transaction, must contain the
    full canonical key order: static keys, all loaded writable keys, then all
    loaded readonly keys. This function does not fetch or trust RPC data itself.
    """
    try:
        if not isinstance(transaction_b64, str) or not transaction_b64:
            raise ValueError("missing transaction")
        raw = base64.b64decode(transaction_b64, validate=True)
        tx = VersionedTransaction.from_bytes(raw)
        tx.sanitize()
    except Exception:
        return {
            "ok": False, "error": "Invalid serialized transaction.",
            "instructions": [], "authorization": "NOT_AUTHORIZED",
        }

    message = tx.message
    static_keys = [str(k) for k in message.account_keys]
    has_lookups = bool(getattr(message, "address_table_lookups", ()))
    if resolved_account_keys is not None:
        keys = [str(k) for k in resolved_account_keys]
        if len(keys) < len(static_keys) or keys[:len(static_keys)] != static_keys:
            return {
                "ok": False, "error": "Resolved account keys do not match static message keys.",
                "instructions": [], "authorization": "NOT_AUTHORIZED",
            }
        if has_lookups and len(keys) == len(static_keys):
            return {
                "ok": False, "error": "Lookup-table addresses were not resolved.",
                "instructions": [], "authorization": "NOT_AUTHORIZED",
            }
    else:
        keys = static_keys

    if has_lookups and resolved_account_keys is None:
        lookup_warning = "Transaction uses address lookup tables; account indices may be unresolved."
    else:
        lookup_warning = None

    decoded: list[dict[str, Any]] = []
    overall_ok = True
    for index, instruction in enumerate(message.instructions):
        try:
            program_index = int(instruction.program_id_index)
            data = bytes(instruction.data)
            account_indices = list(bytes(instruction.accounts))
            if program_index >= len(keys):
                raise IndexError("program index out of range")
            if any(i >= len(keys) for i in account_indices):
                raise IndexError("account index out of range")
            program_id = keys[program_index]
            accounts = [keys[i] for i in account_indices]
        except Exception:
            decoded.append({
                "index": index, "kind": "MALFORMED_COMPILED_INSTRUCTION",
                "risk": "REJECT",
            })
            overall_ok = False
            continue

        if program_id == SYSTEM_PROGRAM:
            details = _decode_system(data)
        elif program_id in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
            details = _decode_token(data)
        elif program_id == COMPUTE_BUDGET_PROGRAM:
            details = _decode_compute(data)
        elif program_id == ASSOCIATED_TOKEN_PROGRAM:
            details = {
                "kind": "CREATE_ASSOCIATED_TOKEN_ACCOUNT",
                "risk": "REVIEW_REQUIRED",
            }
        elif program_id in MEMO_PROGRAMS:
            details = {"kind": "MEMO", "risk": "NON_EXECUTABLE_DATA"}
        else:
            details = {
                "kind": "UNRECOGNIZED_PROGRAM_INSTRUCTION",
                "risk": "UNKNOWN_PROGRAM_REQUIRES_REJECTION",
            }
            overall_ok = False

        if details.get("risk") in ("REJECT", "UNKNOWN", "UNKNOWN_PROGRAM_REQUIRES_REJECTION"):
            overall_ok = False

        decoded.append({
            "index": index,
            "program_id": program_id,
            "accounts": accounts,
            "data_length": len(data),
            **details,
        })

    return {
        "ok": overall_ok and not has_lookups or (
            overall_ok and has_lookups and resolved_account_keys is not None
        ),
        "static_account_count": len(static_keys),
        "uses_address_lookup_tables": has_lookups,
        "lookup_warning": lookup_warning,
        "instruction_count": len(decoded),
        "instructions": decoded,
        "authorization": "NOT_AUTHORIZED",
        "note": (
            "Inspection only. Known outer instruction decoding is not proof of "
            "safe swap semantics; CPI behavior, token metadata, destination "
            "ownership, economic limits, and Jupiter route semantics still need "
            "independent validation."
        ),
    }
