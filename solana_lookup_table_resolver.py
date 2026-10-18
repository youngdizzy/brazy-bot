"""Read-only Solana address lookup-table resolver.

Fetches public account data and decodes lookup-table addresses.
Never signs, broadcasts, or authorizes transactions.
"""
from __future__ import annotations

import base64
from typing import Any

import requests
from solders.address_lookup_table_account import AddressLookupTable
from solders.pubkey import Pubkey

RPC_URL = "https://api.mainnet-beta.solana.com"
ALT_PROGRAM_ID = "AddressLookupTab1e1111111111111111111111111"


def resolve_lookup_table(
    table_address: str,
    *,
    rpc_url: str = RPC_URL,
    timeout: int = 15,
) -> dict[str, Any]:
    """Fetch and decode one lookup table using Solana JSON-RPC."""
    try:
        table_key = Pubkey.from_string(table_address)
    except Exception:
        return {"ok": False, "error": "Invalid lookup-table address."}

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getAccountInfo",
        "params": [
            str(table_key),
            {"encoding": "base64", "commitment": "finalized"},
        ],
    }

    try:
        response = requests.post(rpc_url, json=payload, timeout=timeout)
        response.raise_for_status()
        result = response.json()
    except Exception:
        return {"ok": False, "error": "Lookup-table RPC request failed."}

    if not isinstance(result, dict) or result.get("error"):
        return {"ok": False, "error": "Lookup-table RPC returned an error."}

    value = (result.get("result") or {}).get("value")
    if not isinstance(value, dict):
        return {"ok": False, "error": "Lookup-table account does not exist."}

    if value.get("owner") != ALT_PROGRAM_ID:
        return {"ok": False, "error": "Account is not owned by the lookup-table program."}

    data = value.get("data")
    if not isinstance(data, list) or len(data) < 1 or not isinstance(data[0], str):
        return {"ok": False, "error": "Lookup-table account data is malformed."}

    try:
        raw = base64.b64decode(data[0], validate=True)
        table = AddressLookupTable.deserialize(raw)
        addresses = [str(address) for address in table.addresses]
    except Exception:
        return {"ok": False, "error": "Lookup-table data could not be decoded."}

    return {
        "ok": True,
        "table_address": str(table_key),
        "addresses": addresses,
        "address_count": len(addresses),
        "authorization": "NOT_AUTHORIZED",
    }
