#!/usr/bin/env python3
"""Read-only Jupiter quote preview. Never signs or submits transactions."""

import sys
import requests
from solders.pubkey import Pubkey

QUOTE_URL = "https://api.jup.ag/swap/v1/quote"
RPC_URL = "https://api.mainnet-beta.solana.com"
SOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL = 1_000_000_000


def rpc_token_decimals(mint):
    response = requests.post(
        RPC_URL,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTokenSupply",
            "params": [mint],
        },
        timeout=15,
    )
    response.raise_for_status()
    body = response.json()

    if body.get("error"):
        raise ValueError("Solana RPC could not retrieve token supply.")

    result = (body.get("result") or {}).get("value") or {}
    decimals = result.get("decimals")

    if not isinstance(decimals, int) or not 0 <= decimals <= 18:
        raise ValueError("Invalid token decimals returned by RPC.")

    return decimals


def quote_preview(mint, amount_sol, slippage_bps=50):
    mint = mint.strip()

    try:
        Pubkey.from_string(mint)
    except Exception:
        raise ValueError("Invalid Solana token mint address.")

    if not 0 < amount_sol <= 0.01:
        raise ValueError("Preview amount must be greater than 0 and at most 0.01 SOL.")

    if not 1 <= slippage_bps <= 100:
        raise ValueError("Slippage must be between 1 and 100 basis points.")

    if mint == SOL_MINT:
        raise ValueError("Choose a token mint other than wrapped SOL.")

    amount = int(amount_sol * LAMPORTS_PER_SOL)

    response = requests.get(
        QUOTE_URL,
        params={
            "inputMint": SOL_MINT,
            "outputMint": mint,
            "amount": str(amount),
            "slippageBps": str(slippage_bps),
        },
        timeout=15,
    )
    response.raise_for_status()
    data = response.json()

    if data.get("inputMint") != SOL_MINT or data.get("outputMint") != mint:
        raise ValueError("Quote mint addresses did not match the request.")
    if data.get("swapMode") != "ExactIn":
        raise ValueError("Unexpected swap mode.")
    if int(data.get("inAmount", -1)) != amount:
        raise ValueError("Quote input amount did not match.")
    if int(data.get("slippageBps", -1)) != slippage_bps:
        raise ValueError("Quote slippage did not match.")

    output_raw = int(data.get("outAmount", 0))
    minimum_raw = int(data.get("otherAmountThreshold", 0))

    if output_raw <= 0 or not 0 < minimum_raw <= output_raw:
        raise ValueError("Invalid quoted output amounts.")

    impact_fraction = float(data.get("priceImpactPct", 0))
    if not 0 <= impact_fraction <= 1:
        raise ValueError("Invalid price impact value.")

    decimals = rpc_token_decimals(mint)

    return {
        "mint": mint,
        "input_sol": amount_sol,
        "decimals": decimals,
        "output": output_raw / (10 ** decimals),
        "minimum": minimum_raw / (10 ** decimals),
        "slippage_bps": slippage_bps,
        "impact_pct": impact_fraction * 100,
        "routes": len(data.get("routePlan", [])),
    }


def main():
    if len(sys.argv) != 3:
        print("Usage: python jupiter_preview.py TOKEN_MINT AMOUNT_SOL")
        raise SystemExit(2)

    try:
        result = quote_preview(sys.argv[1], float(sys.argv[2]))
    except Exception as exc:
        print(f"Preview failed: {type(exc).__name__}: {exc}")
        raise SystemExit(1)

    print("\nJUPITER QUOTE PREVIEW — NO TRADE SUBMITTED")
    print("-----------------------------------------")
    print(f"SOL input: {result['input_sol']:.6f}")
    print(f"Token mint: {result['mint']}")
    print(f"Token decimals: {result['decimals']}")
    print(f"Estimated output: {result['output']:,.{min(result['decimals'], 10)}f}")
    print(f"Minimum output at quoted slippage: {result['minimum']:,.{min(result['decimals'], 10)}f}")
    print(f"Slippage limit: {result['slippage_bps']} bps ({result['slippage_bps'] / 100:.2f}%)")
    print(f"Price impact: {result['impact_pct']:.6f}%")
    print(f"Route steps: {result['routes']}")
    if result["impact_pct"] > 3:
        print("WARNING: Price impact exceeds 3%; do not treat this quote as safe.")
    print("\nQuote only. No wallet key loaded; no transaction signed or sent.")


if __name__ == "__main__":
    main()
