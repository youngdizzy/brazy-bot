import unittest
from unittest.mock import patch

import solana_executor
from solana_executor import OrderCheck


class ExecutorInspectionTests(unittest.TestCase):
    def test_missing_order_stays_unauthorized(self):
        check = OrderCheck(False, ("Order request failed.",))

        with patch.object(
            solana_executor, "inspect_buy_order", return_value=(check, None)
        ):
            result = solana_executor.inspect_buy_order_details(
                secret="test-only",
                output_mint="output-mint",
                amount_lamports=1000,
            )

        self.assertIsNone(result["inspection"])
        self.assertEqual(result["authorization"], "NOT_AUTHORIZED")

    def test_order_transaction_is_sent_to_read_only_inspector(self):
        check = OrderCheck(
            False,
            ("Not authorized to sign.",),
            wallet="public-test-address",
            input_amount=1000,
            quoted_output=2000,
            transaction_version="V0",
            instruction_count=1,
        )
        order = {"transaction": "synthetic-transaction"}

        with (
            patch.object(
                solana_executor, "inspect_buy_order", return_value=(check, order)
            ),
            patch(
                "solana_instruction_inspector.inspect_transaction_from_rpc",
                return_value={
                    "ok": True,
                    "authorization": "NOT_AUTHORIZED",
                    "instructions": [],
                },
            ) as inspector,
        ):
            result = solana_executor.inspect_buy_order_details(
                secret="test-only",
                output_mint="output-mint",
                amount_lamports=1000,
            )

        inspector.assert_called_once_with("synthetic-transaction")
        self.assertEqual(result["authorization"], "NOT_AUTHORIZED")
        self.assertTrue(result["inspection"]["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
