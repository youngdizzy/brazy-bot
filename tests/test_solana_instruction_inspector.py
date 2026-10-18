import unittest

from solana_instruction_inspector import inspect_transaction


class InstructionInspectorTests(unittest.TestCase):
    def test_rejects_invalid_base64(self):
        result = inspect_transaction("not-base64")
        self.assertFalse(result["ok"])

    def test_rejects_empty_input(self):
        result = inspect_transaction("")
        self.assertFalse(result["ok"])

    def test_invalid_input_never_authorizes(self):
        result = inspect_transaction("invalid")
        self.assertNotEqual(result.get("authorization"), "AUTHORIZED")


if __name__ == "__main__":
    unittest.main(verbosity=2)
