"""Shared, fail-closed account-index resolution for versioned Solana messages."""

from __future__ import annotations

from typing import Any

from solders.pubkey import Pubkey


def resolve_lookup_keys(
    lookups: Any,
    lookup_map: dict[str, list[str]],
) -> tuple[list[str], list[str], list[str]]:
    """Return writable keys, readonly keys, and errors in Solana index order."""
    writable: list[str] = []
    readonly: list[str] = []
    errors: list[str] = []

    # First pass: writable addresses across every table.
    # Second pass: readonly addresses across every table.
    for target, indexes_attr in (
        (writable, "writable_indexes"),
        (readonly, "readonly_indexes"),
    ):
        for lookup in lookups:
            table_address = str(lookup.account_key)
            addresses = lookup_map.get(table_address)

            if addresses is None:
                message = f"Lookup table not resolved: {table_address}"
                if message not in errors:
                    errors.append(message)
                continue

            for index in getattr(lookup, indexes_attr):
                if index >= len(addresses):
                    errors.append(
                        f"Lookup index out of range: {table_address}"
                    )
                    continue
                try:
                    target.append(str(Pubkey.from_string(addresses[index])))
                except Exception:
                    errors.append(
                        f"Invalid lookup-table address: {table_address}"
                    )

    return writable, readonly, errors
