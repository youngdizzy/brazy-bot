import unittest

from solana_transaction_validator import validate_unsigned_order


WALLET = "11111111111111111111111111111111"
SOL_MINT = "So11111111111111111111111111111111111111112"
TOKEN_MINT = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
AMOUNT = 100_000


def make_order(transaction="not-base64"):
    return {
        "inputMint": SOL_MINT,
        "outputMint": TOKEN_MINT,
        "inAmount": str(AMOUNT),
        "outAmount": "1000",
        "requestId": "test-request",
        "transaction": transaction,
    }


def validate(order=None, transaction="not-base64", **overrides):
    args = {
        "transaction_b64": transaction,
        "expected_taker": WALLET,
        "expected_input_mint": SOL_MINT,
        "expected_output_mint": TOKEN_MINT,
        "expected_input_amount": AMOUNT,
        "order": order if order is not None else make_order(transaction),
    }
    args.update(overrides)
    return validate_unsigned_order(**args)


class ValidatorRejectionTests(unittest.TestCase):
    def test_rejects_input_mint_mismatch(self):
        order = make_order()
        order["inputMint"] = TOKEN_MINT
        result = validate(order=order)
        self.assertFalse(result.accepted)
        self.assertTrue(any("input mint" in r.lower() for r in result.reasons))

    def test_rejects_output_mint_mismatch(self):
        order = make_order()
        order["outputMint"] = SOL_MINT
        result = validate(order=order)
        self.assertFalse(result.accepted)
        self.assertTrue(any("output mint" in r.lower() for r in result.reasons))

    def test_rejects_input_amount_mismatch(self):
        order = make_order()
        order["inAmount"] = str(AMOUNT + 1)
        result = validate(order=order)
        self.assertFalse(result.accepted)
        self.assertTrue(any("input amount" in r.lower() for r in result.reasons))

    def test_rejects_missing_request_id(self):
        order = make_order()
        order["requestId"] = ""
        result = validate(order=order)
        self.assertFalse(result.accepted)
        self.assertTrue(any("request id" in r.lower() for r in result.reasons))

    def test_rejects_missing_output_amount(self):
        order = make_order()
        order["outAmount"] = ""
        result = validate(order=order)
        self.assertFalse(result.accepted)
        self.assertTrue(any("output amount" in r.lower() for r in result.reasons))

    def test_rejects_missing_transaction(self):
        order = make_order()
        order["transaction"] = ""
        result = validate(order=order, transaction="")
        self.assertFalse(result.accepted)
        self.assertTrue(any("transaction" in r.lower() for r in result.reasons))

    def test_rejects_transaction_that_does_not_match_order(self):
        order = make_order("different-value")
        result = validate(order=order, transaction="not-base64")
        self.assertFalse(result.accepted)
        self.assertTrue(any("does not match" in r.lower() for r in result.reasons))

    def test_rejects_malformed_serialized_transaction(self):
        result = validate()
        self.assertFalse(result.accepted)
        self.assertTrue(any("serialized versioned transaction" in r.lower()
                            for r in result.reasons))


if __name__ == "__main__":
    unittest.main(verbosity=2)
