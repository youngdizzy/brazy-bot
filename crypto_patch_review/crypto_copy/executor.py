"""Solana live swap executor — Jupiter Swap API (api.jup.ag).

Fail-closed: never signs/broadcasts unless COPY_LIVE_ENABLED=true.
Secret is never logged.
"""
from __future__ import annotations

import base64
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple
from .tx_validate import validate_swap_tx
from .order_state import Order, OrderState

log = logging.getLogger("brazy-executor")

SOL_MINT = "So11111111111111111111111111111111111111112"
# Current hostnames (portal key optional; lite host for testing)
JUP_BASE = os.getenv("JUPITER_API_BASE", "https://api.jup.ag/swap/v1")
JUP_LITE = os.getenv("JUPITER_LITE_BASE", "https://lite-api.jup.ag/swap/v1")
RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
JUP_API_KEY = os.getenv("JUPITER_API_KEY", "")


def live_enabled() -> bool:
    return os.getenv("COPY_LIVE_ENABLED", "").lower() in ("1", "true", "yes")


def _jup_headers() -> Dict[str, str]:
    h = {"Accept": "application/json", "Content-Type": "application/json"}
    if JUP_API_KEY:
        h["x-api-key"] = JUP_API_KEY
    return h


def rpc(method: str, params: list, timeout: int = 30) -> Any:
    import requests
    r = requests.post(
        RPC_URL,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=timeout,
    )
    data = r.json()
    if data.get("error"):
        raise RuntimeError(str(data["error"]))
    return data.get("result")


def get_balance_sol(pubkey: str) -> Optional[float]:
    try:
        lamports = rpc("getBalance", [pubkey])
        if isinstance(lamports, dict):
            lamports = lamports.get("value", 0)
        return float(lamports) / 1e9
    except Exception as e:
        log.warning("balance: %s", e)
        return None


def get_token_balance_raw(owner: str, mint: str) -> int:
    try:
        res = rpc(
            "getTokenAccountsByOwner",
            [owner, {"mint": mint}, {"encoding": "jsonParsed"}],
        )
        total = 0
        for acc in (res or {}).get("value") or []:
            amt = (
                acc.get("account", {})
                .get("data", {})
                .get("parsed", {})
                .get("info", {})
                .get("tokenAmount", {})
                .get("amount", "0")
            )
            total += int(amt)
        return total
    except Exception as e:
        log.warning("token balance: %s", e)
        return 0


def jupiter_quote(
    input_mint: str,
    output_mint: str,
    amount_raw: int,
    slippage_bps: int = 150,
) -> Tuple[Optional[Dict[str, Any]], str]:
    import requests
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount_raw),
        "slippageBps": str(slippage_bps),
    }
    errors = []
    for base in (JUP_BASE, JUP_LITE):
        try:
            r = requests.get(
                f"{base}/quote",
                params=params,
                headers=_jup_headers(),
                timeout=20,
            )
            if not r.ok:
                errors.append(f"{base}:{r.status_code}")
                continue
            data = r.json()
            if data.get("error"):
                errors.append(str(data.get("error")))
                continue
            if not data.get("outAmount") and not data.get("routePlan"):
                errors.append(f"{base}:empty")
                continue
            return data, "ok"
        except Exception as e:
            errors.append(f"{base}:{e}")
    return None, "quote_fail:" + ";".join(errors)[:300]


def jupiter_swap_tx(
    quote: Dict[str, Any],
    user_public_key: str,
    prioritization_fee_lamports: Optional[int] = None,
) -> Tuple[Optional[str], str]:
    import requests
    body: Dict[str, Any] = {
        "quoteResponse": quote,
        "userPublicKey": user_public_key,
        "wrapAndUnwrapSol": True,
        "dynamicComputeUnitLimit": True,
    }
    if prioritization_fee_lamports is not None:
        body["prioritizationFeeLamports"] = prioritization_fee_lamports
    errors = []
    for base in (JUP_BASE, JUP_LITE):
        try:
            r = requests.post(
                f"{base}/swap",
                json=body,
                headers=_jup_headers(),
                timeout=30,
            )
            if not r.ok:
                errors.append(f"{base}:{r.status_code}:{r.text[:120]}")
                continue
            data = r.json()
            tx = data.get("swapTransaction")
            if not tx:
                errors.append(f"{base}:no_swapTransaction")
                continue
            return tx, "ok"
        except Exception as e:
            errors.append(f"{base}:{e}")
    return None, "swap_fail:" + ";".join(errors)[:300]


def preflight_tx_b64(tx_b64: str) -> Tuple[bool, str]:
    try:
        res = rpc(
            "simulateTransaction",
            [
                tx_b64,
                {
                    "encoding": "base64",
                    "sigVerify": False,
                    "replaceRecentBlockhash": True,
                },
            ],
        )
        err = (res or {}).get("value", {}).get("err")
        if err:
            return False, f"sim_fail:{err}"
        return True, "ok"
    except Exception as e:
        return False, f"sim_err:{e}"


def sign_and_send_versioned(tx_b64: str, secret_base58: str) -> Tuple[Optional[str], str]:
    if not live_enabled():
        return None, "live_disabled"
    if not secret_base58:
        return None, "no_secret"
    try:
        from solders.keypair import Keypair
        from solders.transaction import VersionedTransaction
        from solders.null_signer import NullSigner
        from solders.pubkey import Pubkey
    except Exception as e:
        return None, f"deps:{e}"

    try:
        kp = Keypair.from_base58_string(secret_base58.strip())
        raw = base64.b64decode(tx_b64)
        unsigned = VersionedTransaction.from_bytes(raw)
        # Sign: replace empty signatures with keypair signature
        # solders VersionedTransaction(message, signers)
        signed = VersionedTransaction(unsigned.message, [kp])
        wire = bytes(signed)
        b64 = base64.b64encode(wire).decode()
        res = rpc(
            "sendTransaction",
            [
                b64,
                {
                    "encoding": "base64",
                    "skipPreflight": False,
                    "maxRetries": 3,
                    "preflightCommitment": "confirmed",
                },
            ],
            timeout=45,
        )
        if not isinstance(res, str) or not res:
            return None, f"no_signature:{res}"
        return res, "ok"
    except Exception as e:
        return None, f"sign_send:{type(e).__name__}:{e}"


def confirm_signature(sig: str, timeout_sec: float = 60.0) -> Tuple[bool, str]:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            res = rpc(
                "getSignatureStatuses",
                [[sig], {"searchTransactionHistory": True}],
            )
            st = ((res or {}).get("value") or [None])[0]
            if st is None:
                time.sleep(1.5)
                continue
            if st.get("err"):
                return False, f"tx_err:{st.get('err')}"
            conf = st.get("confirmationStatus")
            if conf in ("confirmed", "finalized"):
                return True, conf
        except Exception as e:
            log.warning("confirm: %s", e)
        time.sleep(1.5)
    return False, "confirm_timeout"


def execute_swap(
    secret_base58: str,
    user_pubkey: str,
    input_mint: str,
    output_mint: str,
    amount_raw: int,
    slippage_bps: int = 150,
    max_priority_fee_lamports: int = 500_000,
    require_live: bool = True,
) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "ok": False,
        "sig": None,
        "error": None,
        "live": False,
        "quote": None,
    }
    if require_live and not live_enabled():
        out["error"] = "COPY_LIVE_ENABLED is not true — refusing to sign/broadcast"
        return out
    if amount_raw <= 0:
        out["error"] = "amount_raw must be > 0"
        return out

    order = Order(
        side="buy" if input_mint == SOL_MINT else "sell",
        mint=output_mint if input_mint == SOL_MINT else input_mint,
        amount_raw=amount_raw,
        meta={"user": user_pubkey[:8] if user_pubkey else ""},
    )
    order.advance(OrderState.RISK_CHECKED)
    out["order_id"] = order.id

    quote, qerr = jupiter_quote(input_mint, output_mint, amount_raw, slippage_bps)
    if not quote:
        out["error"] = qerr
        return out
    out["quote"] = {
        "inAmount": quote.get("inAmount"),
        "outAmount": quote.get("outAmount"),
        "priceImpactPct": quote.get("priceImpactPct"),
    }
    order.advance(OrderState.QUOTED, quote=out["quote"])
    try:
        impact = float(quote.get("priceImpactPct") or 0)
        if impact > 15.0:
            out["error"] = f"price_impact_too_high:{impact}"
            return out
    except Exception:
        pass

    tx_b64, serr = jupiter_swap_tx(quote, user_pubkey, max_priority_fee_lamports)
    if not tx_b64:
        out["error"] = serr
        return out

    ok_val, val_msg = validate_swap_tx(tx_b64, user_pubkey)
    if not ok_val:
        out["error"] = f"tx_validate:{val_msg}"
        return out
    out["validate"] = val_msg
    order.advance(OrderState.VALIDATED)

    ok_sim, sim_msg = preflight_tx_b64(tx_b64)
    if not ok_sim:
        order.advance(OrderState.FAILED, error=sim_msg)
        out["error"] = sim_msg
        out["order"] = order.to_dict()
        return out
    order.advance(OrderState.SIMULATED)

    sig, send_err = sign_and_send_versioned(tx_b64, secret_base58)
    if not sig:
        order.advance(OrderState.FAILED, error=send_err)
        out["error"] = send_err
        out["order"] = order.to_dict()
        return out
    order.advance(OrderState.SUBMITTED, sig=sig)

    ok_conf, conf_msg = confirm_signature(sig)
    out["sig"] = sig
    out["live"] = True
    if not ok_conf:
        order.advance(OrderState.UNKNOWN_REQUIRES_RECONCILIATION, error=conf_msg)
        out["error"] = f"submitted_but:{conf_msg}"
        out["ok"] = False
        out["order"] = order.to_dict()
        return out
    order.advance(OrderState.CONFIRMED)
    out["ok"] = True
    out["confirmation"] = conf_msg
    out["order"] = order.to_dict()
    return out


def buy_token_sol(
    secret_base58: str,
    user_pubkey: str,
    token_mint: str,
    sol_amount: float,
    slippage_bps: int = 150,
) -> Dict[str, Any]:
    lamports = int(sol_amount * 1_000_000_000)
    return execute_swap(
        secret_base58,
        user_pubkey,
        SOL_MINT,
        token_mint,
        lamports,
        slippage_bps=slippage_bps,
    )


def sell_token_pct(
    secret_base58: str,
    user_pubkey: str,
    token_mint: str,
    pct: float = 100.0,
    slippage_bps: int = 150,
) -> Dict[str, Any]:
    raw = get_token_balance_raw(user_pubkey, token_mint)
    if raw <= 0:
        return {"ok": False, "error": "no_token_balance", "sig": None, "live": False}
    sell_raw = int(raw * min(max(pct, 0), 100) / 100.0)
    if sell_raw <= 0:
        return {"ok": False, "error": "sell_amount_zero", "sig": None, "live": False}
    return execute_swap(
        secret_base58,
        user_pubkey,
        token_mint,
        SOL_MINT,
        sell_raw,
        slippage_bps=slippage_bps,
    )
