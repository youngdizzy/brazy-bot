import base64
import unittest

from solders.address_lookup_table_account import AddressLookupTableAccount
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from solana_instruction_inspector import inspect_transaction


class LookupPrivilegeTests(unittest.TestCase):
    def test_reports_writable_lookup_account(self):
        payer = Pubkey.new_unique()
        lookup_program = Pubkey.new_unique()
        lookup_table_key = Pubkey.new_unique()
        loaded_account = Pubkey.new_unique()

        table = AddressLookupTableAccount(
            key=lookup_table_key,
            addresses=[loaded_account],
        )

        instruction = Instruction(
            lookup_program,
            b"\x01",
            [
                AccountMeta(payer, is_signer=True, is_writable=True),
                AccountMeta(loaded_account, is_signer=False, is_writable=True),
            ],
        )

        message = MessageV0.try_compile(
            payer,
            [instruction],
            [table],
            Hash.default(),
        )

        self.assertEqual(len(message.address_table_lookups), 1)

        tx = VersionedTransaction.populate(
            message,
            [Signature.default()],
        )
        encoded = base64.b64encode(bytes(tx)).decode("ascii")

        lookup_map = {str(lookup_table_key): [str(loaded_account)]}
        result = inspect_transaction(encoded, lookup_map)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["authorization"], "NOT_AUTHORIZED")

        accounts = result["instructions"][0]["accounts"]
        loaded = next(
            account for account in accounts
            if account["address"] == str(loaded_account)
        )
        self.assertFalse(loaded["signer"])
        self.assertTrue(loaded["writable"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
