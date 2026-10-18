import base64
import unittest

from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from solana_instruction_inspector import inspect_transaction


class InstructionPrivilegeTests(unittest.TestCase):
    def test_reports_signer_and_writable_accounts(self):
        payer = Pubkey.new_unique()
        recipient = Pubkey.new_unique()
        program = Pubkey.new_unique()

        instruction = Instruction(
            program,
            b"\x01",
            [
                AccountMeta(payer, is_signer=True, is_writable=True),
                AccountMeta(recipient, is_signer=False, is_writable=True),
            ],
        )
        message = MessageV0.try_compile(
            payer,
            [instruction],
            [],
            Hash.default(),
        )

        # Placeholder signature only; no signing, network call, or submission.
        tx = VersionedTransaction.populate(
            message,
            [Signature.default()],
        )
        encoded = base64.b64encode(bytes(tx)).decode("ascii")

        result = inspect_transaction(encoded)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["authorization"], "NOT_AUTHORIZED")

        accounts = result["instructions"][0]["accounts"]
        by_address = {item["address"]: item for item in accounts}

        self.assertTrue(by_address[str(payer)]["signer"])
        self.assertTrue(by_address[str(payer)]["writable"])
        self.assertFalse(by_address[str(recipient)]["signer"])
        self.assertTrue(by_address[str(recipient)]["writable"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
