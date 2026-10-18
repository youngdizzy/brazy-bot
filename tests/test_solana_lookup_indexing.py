import unittest
from types import SimpleNamespace

from solana_lookup_indexing import resolve_lookup_keys

TABLE_A = "11111111111111111111111111111111"
TABLE_B = "SysvarRent111111111111111111111111111111111"
A0 = "ComputeBudget111111111111111111111111111111"
A1 = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
B0 = "AddressLookupTab1e1111111111111111111111111"
B1 = "Vote111111111111111111111111111111111111111"


def lookup(key, writable=(), readonly=()):
    return SimpleNamespace(
        account_key=key,
        writable_indexes=bytes(writable),
        readonly_indexes=bytes(readonly),
    )


class LookupIndexingTests(unittest.TestCase):
    def test_writable_keys_from_all_tables_precede_readonly_keys(self):
        lookups = [
            lookup(TABLE_A, writable=(0,), readonly=(1,)),
            lookup(TABLE_B, writable=(0,), readonly=(1,)),
        ]
        mapping = {
            TABLE_A: [A0, A1],
            TABLE_B: [B0, B1],
        }

        writable, readonly, errors = resolve_lookup_keys(lookups, mapping)

        self.assertEqual(writable, [A0, B0])
        self.assertEqual(readonly, [A1, B1])
        self.assertEqual(errors, [])

    def test_unresolved_table_fails(self):
        writable, readonly, errors = resolve_lookup_keys(
            [lookup(TABLE_A, writable=(0,))], {}
        )
        self.assertEqual(writable, [])
        self.assertEqual(readonly, [])
        self.assertTrue(any("not resolved" in error for error in errors))

    def test_out_of_range_index_fails(self):
        writable, readonly, errors = resolve_lookup_keys(
            [lookup(TABLE_A, writable=(2,))],
            {TABLE_A: [A0]},
        )
        self.assertEqual(writable, [])
        self.assertTrue(any("out of range" in error for error in errors))


if __name__ == "__main__":
    unittest.main(verbosity=2)
