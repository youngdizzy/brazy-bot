import base64
import unittest

from solana_instruction_decoder import (
    SYSTEM_PROGRAM, TOKEN_PROGRAM, decode_transaction_instructions,
)


class InstructionDecoderTests(unittest.TestCase):
    def test_invalid_base64_rejected(self):
        result = decode_transaction_instructions("not base64!!!")
        self.assertFalse(result["ok"])
        self.assertEqual(result["authorization"], "NOT_AUTHORIZED")

    def test_system_transfer_decoder(self):
        import struct
        decoded = __import__("solana_instruction_decoder")._decode_system(
            struct.pack("<IQ", 2, 1234)
        )
        self.assertEqual(decoded["kind"], "Transfer")
        self.assertEqual(decoded["lamports"], 1234)
        self.assertEqual(decoded["risk"], "VALUE_TRANSFER")

    def test_malformed_system_transfer_rejected(self):
        import struct
        decoded = __import__("solana_instruction_decoder")._decode_system(
            struct.pack("<I", 2) + b"\x01"
        )
        self.assertEqual(decoded["risk"], "REJECT")

    def test_unknown_token_opcode_is_not_assumed_safe(self):
        decoded = __import__("solana_instruction_decoder")._decode_token(b"\xff")
        self.assertEqual(decoded["kind"], "UNKNOWN_TOKEN_INSTRUCTION")
        self.assertEqual(decoded["risk"], "UNKNOWN")

    def test_always_not_authorized(self):
        result = decode_transaction_instructions("bad")
        self.assertEqual(result["authorization"], "NOT_AUTHORIZED")


if __name__ == "__main__":
    unittest.main()
