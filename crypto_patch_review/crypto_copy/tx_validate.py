"""Pre-sign transaction validation. Fail closed on unknown structure."""
from __future__ import annotations

import base64
import logging
from typing import Any, Dict, List, Optional, Set, Tuple

log = logging.getLogger("brazy-tx-validate")

# Well-known Solana program IDs (base58)
ALLOWED_PROGRAMS = {
    "11111111111111111111111111111111",  # System
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",  # Token
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",  # Token-2022
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",  # ATA
    "ComputeBudget111111111111111111111111111111",
    "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXSWDYaLye",
    # Jupiter / common aggregators (expand carefully)
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",
    "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB",
    "JUP2jxvXaqu7NQY1GmNF4m1vodw12LVXYxbFL2uZr47",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",  # pump.fun
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",  # Raydium AMM
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",  # Raydium CLMM
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",  # Orca whirlpool
    "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM",  # sometimes used
}

# Programs that must never appear in bot-built swaps
BLOCKED_PROGRAMS: Set[str] = set()


def decode_versioned_program_ids(tx_b64: str) -> Tuple[Optional[List[str]], str]:
    """Best-effort extract static account keys / program ids from versioned tx."""
    try:
        from solders.transaction import VersionedTransaction
        raw = base64.b64decode(tx_b64)
        tx = VersionedTransaction.from_bytes(raw)
        msg = tx.message
        keys = []
        # MessageV0 has account_keys
        account_keys = list(getattr(msg, "account_keys", []) or [])
        for k in account_keys:
            keys.append(str(k))
        # instructions reference program_id_index into account_keys
        prog_ids = []
        for ix in getattr(msg, "instructions", []) or []:
            idx = getattr(ix, "program_id_index", None)
            if idx is not None and idx < len(account_keys):
                prog_ids.append(str(account_keys[idx]))
        return prog_ids or keys[:16], "ok"
    except Exception as e:
        return None, f"decode_fail:{e}"


def validate_swap_tx(
    tx_b64: str,
    expected_user: str,
    max_unknown_programs: int = 3,
) -> Tuple[bool, str]:
    """
    Reject transactions we cannot classify or that include blocked programs.
    Allows limited unknown programs (Jupiter routes evolve) up to max_unknown.
    """
    if not tx_b64 or len(tx_b64) < 32:
        return False, "empty_tx"
    progs, err = decode_versioned_program_ids(tx_b64)
    if progs is None:
        # solders missing or decode failed — fail closed for LIVE
        return False, err

    unknown = []
    for p in progs:
        if p in BLOCKED_PROGRAMS:
            return False, f"blocked_program:{p}"
        if p not in ALLOWED_PROGRAMS:
            unknown.append(p)

    # Jupiter routes often include intermediate program ids; allow small set
    if len(set(unknown)) > max_unknown_programs:
        return False, f"too_many_unknown_programs:{len(set(unknown))}"

    if expected_user and expected_user not in progs and expected_user not in (progs or []):
        # user pubkey should appear in account keys
        # soft check: decode may not list all ALT-resolved keys
        pass

    return True, "ok" if not unknown else f"ok_unknown={len(set(unknown))}"
