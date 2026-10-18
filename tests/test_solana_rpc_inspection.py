import unittest
from unittest.mock import patch

from solana_instruction_inspector import inspect_transaction_from_rpc


class RpcInspectionTests(unittest.TestCase):
    def test_invalid_transaction_fails_before_rpc(self):
        with patch(
            "solana_lookup_table_resolver.resolve_lookup_table"
        ) as resolver:
            result = inspect_transaction_from_rpc("not-base64")

        self.assertFalse(result["ok"])
        self.assertEqual(result["authorization"], "NOT_AUTHORIZED")
        resolver.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
