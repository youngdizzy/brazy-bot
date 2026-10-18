import unittest
from unittest.mock import Mock, patch

from solana_lookup_table_resolver import resolve_lookup_table, ALT_PROGRAM_ID


class LookupTableResolverTests(unittest.TestCase):
    def test_rejects_invalid_address_without_rpc(self):
        with patch("solana_lookup_table_resolver.requests.post") as post:
            result = resolve_lookup_table("not-a-public-key")
        self.assertFalse(result["ok"])
        post.assert_not_called()

    @patch("solana_lookup_table_resolver.requests.post")
    def test_rejects_rpc_error(self, post):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"error": {"message": "unavailable"}}
        post.return_value = response

        result = resolve_lookup_table("11111111111111111111111111111111")
        self.assertFalse(result["ok"])

    @patch("solana_lookup_table_resolver.requests.post")
    def test_rejects_wrong_owner(self, post):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "result": {
                "value": {
                    "owner": "11111111111111111111111111111111",
                    "data": ["AA==", "base64"],
                }
            }
        }
        post.return_value = response

        result = resolve_lookup_table("11111111111111111111111111111111")
        self.assertFalse(result["ok"])
        self.assertIn("not owned", result["error"])

    def test_owner_constant_is_present(self):
        self.assertTrue(ALT_PROGRAM_ID.startswith("AddressLookupTab1e"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
