"""Brazy Bot: inspect Jupiter orders without signing or broadcasting."""

import requests
from solders.transaction import VersionedTransaction
from solders.pubkey import Pubkey

JUPITER_ORDER_URL = "https://api.jup.ag/swap/v2/order"
SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

# Public example address only. Never use this as a trading wallet.
EXAMPLE_TAKER = "BQ72nSv9f3PRyRKCBnHLVrerrv37CYTHm5h3s9VSGQDV"


def inspect_order(amount_lamports: int = 1_000_000) -> None:
    if not 1 <= amount_lamports <= 1_000_000:
        raise ValueError("Test amount must be between 1 and 1,000,000 lamports.")

    for address in (SOL_MINT, USDC_MINT, EXAMPLE_TAKER):
        Pubkey.from_string(address)

    response = requests.get(
        JUPITER_ORDER_URL,
        params={
            "inputMint": SOL_MINT,
            "outputMint": USDC_MINT,
            "amount": str(amount_lamports),
            "taker": EXAMPLE_TAKER,
        },
        timeout=20,
    )
    response.raise_for_status()
    order = response.json()

    if not order.get("transaction"):
        raise RuntimeError("Jupiter returned no transaction.")

    tx = VersionedTransaction.from_bytes(
        __import__("base64").b64decode(order["transaction"])
    )

    print("Jupiter order received.")
    print("Transaction version:", tx.version())
    print("Message account keys:", len(tx.message.account_keys))
    print("Instructions:", len(tx.message.instructions))
    print("Output amount present:", bool(order.get("outAmount")))
    print("Transaction signed or submitted: NO")


if __name__ == "__main__":
    inspect_order()
