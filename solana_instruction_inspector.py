"""Read-only Solana versioned-transaction instruction inspector.

This module never signs, broadcasts, or authorizes transactions.
"""
from __future__ import annotations

import base64
from typing import Any

from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from solana_lookup_indexing import resolve_lookup_keys


def inspect_transaction(
    transaction_b64: str,
    resolved_lookup_addresses: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Decode a serialized transaction and describe its compiled instructions.

    Lookup-table addresses must come from a trusted RPC. This function does
    not fetch lookup tables or decide whether an instruction is safe.
    """
    try:
        raw = base64.b64decode(transaction_b64, validate=True)
        tx = VersionedTransaction.from_bytes(raw)
        tx.sanitize()
    except Exception:
        return {"ok": False, "error": "Invalid serialized transaction."}

    message = tx.message
    static_keys = [str(key) for key in message.account_keys]
    lookup_map = resolved_lookup_addresses or {}
    lookups = list(getattr(message, "address_table_lookups", []))
    writable_lookup, readonly_lookup, lookup_errors = resolve_lookup_keys(
        lookups, lookup_map
    )
    if lookup_errors:
        return {
            "ok": False,
            "error": "Lookup-table resolution failed.",
            "details": lookup_errors,
            "authorization": "NOT_AUTHORIZED",
        }
    all_keys = static_keys + writable_lookup + readonly_lookup
    instructions = []

    for position, ix in enumerate(message.instructions, start=1):
        if ix.program_id_index >= len(all_keys):
            return {"ok": False, "error": "Instruction program index out of range."}
        if any(index >= len(all_keys) for index in ix.accounts):
            return {"ok": False, "error": "Instruction account index out of range."}

        account_details = []
        for account_index in ix.accounts:
            key = all_keys[account_index]
            try:
                signer = bool(message.is_signer(account_index))
                writable = bool(message.is_maybe_writable(account_index))
            except Exception:
                return {
                    "ok": False,
                    "error": "Could not safely determine account privileges.",
                    "authorization": "NOT_AUTHORIZED",
                }

            account_details.append({
                "index": account_index,
                "address": key,
                "signer": signer,
                "writable": writable,
            })

        instructions.append({
            "index": position,
            "program_id": all_keys[ix.program_id_index],
            "account_count": len(ix.accounts),
            "account_indexes": list(ix.accounts),
            "accounts": account_details,
            "data_length": len(bytes(ix.data)),
        })

    return {
        "ok": True,
        "transaction_version": str(tx.version()),
        "required_signatures": message.header.num_required_signatures,
        "static_account_count": len(static_keys),
        "instruction_count": len(instructions),
        "lookup_table_count": len(lookups),
        "instructions": instructions,
        "authorization": "NOT_AUTHORIZED",
    }


def inspect_transaction_from_rpc(transaction_b64: str) -> dict[str, Any]:
    """Resolve lookup tables via read-only RPC, then inspect the transaction.

    This is diagnostic only. RPC data is not signing authorization.
    """
    from solana_lookup_table_resolver import resolve_lookup_table

    try:
        raw = base64.b64decode(transaction_b64, validate=True)
        tx = VersionedTransaction.from_bytes(raw)
        tx.sanitize()
    except Exception:
        return {
            "ok": False,
            "error": "Invalid serialized transaction.",
            "authorization": "NOT_AUTHORIZED",
        }

    resolved: dict[str, list[str]] = {}
    for lookup in getattr(tx.message, "address_table_lookups", []):
        table_address = str(lookup.account_key)
        if table_address in resolved:
            continue

        result = resolve_lookup_table(table_address)
        if not result.get("ok"):
            return {
                "ok": False,
                "error": "Could not resolve every lookup table.",
                "failed_table": table_address,
                "authorization": "NOT_AUTHORIZED",
            }

        addresses = result.get("addresses")
        if not isinstance(addresses, list):
            return {
                "ok": False,
                "error": "Lookup-table resolver returned invalid address data.",
                "authorization": "NOT_AUTHORIZED",
            }
        resolved[table_address] = addresses

    result = inspect_transaction(transaction_b64, resolved)
    result["authorization"] = "NOT_AUTHORIZED"
    result["lookup_tables_resolved"] = len(resolved)
    return result
