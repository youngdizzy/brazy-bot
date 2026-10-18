"""Simulate a Jupiter transaction; never sign or broadcast it."""

import base64
import requests
from solders.transaction import VersionedTransaction

ORDER_URL = "https://api.jup.ag/swap/v2/order"
RPC_URL = "https://api.mainnet-beta.solana.com"
SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
PUBLIC_EXAMPLE_TAKER = "BQ72nSv9f3PRyRKCBnHLVrerrv37CYTHm5h3s9VSGQDV"


def main():
    order_response = requests.get(
        ORDER_URL,
        params={
            "inputMint": SOL_MINT,
            "outputMint": USDC_MINT,
            "amount": "1000000",
            "taker": PUBLIC_EXAMPLE_TAKER,
        },
        timeout=20,
    )
    order_response.raise_for_status()
    order = order_response.json()

    raw_tx = base64.b64decode(order["transaction"])
    tx = VersionedTransaction.from_bytes(raw_tx)

    # Simulate only. This does not broadcast the transaction.
    rpc_response = requests.post(
        RPC_URL,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "simulateTransaction",
            "params": [
                base64.b64encode(bytes(tx)).decode("ascii"),
                {
                    "encoding": "base64",
                    "sigVerify": False,
                    "replaceRecentBlockhash": True,
                    "commitment": "processed",
                },
            ],
        },
        timeout=30,
    )
    rpc_response.raise_for_status()
    payload = rpc_response.json()

    if "error" in payload:
        print("RPC error:", payload["error"].get("message", "unknown"))
        return

    result = payload.get("result", {}).get("value", {})
    print("Simulation returned:", bool(result))
    print("Simulation error:", result.get("err"))
    print("Compute units:", result.get("unitsConsumed"))
    print("Transaction signed or submitted: NO")

    logs = result.get("logs") or []
    for line in logs[-5:]:
        print("Log:", line[:240])


if __name__ == "__main__":
    main()
