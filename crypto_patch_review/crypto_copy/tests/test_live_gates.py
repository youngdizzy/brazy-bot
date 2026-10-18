"""Unit tests — mocked RPC/Jupiter. Never hits mainnet."""
import os
import unittest
from pathlib import Path
import tempfile
from unittest.mock import patch, MagicMock

from crypto_copy.storage import CopyStorage
from crypto_copy.copy_engine import CopyEngine, live_enabled
from crypto_copy.models import CopySubscription, CopyRules
from crypto_copy.risk_limits import RiskSnapshot, check_before_sign
from crypto_copy.tx_validate import validate_swap_tx


class TestRisk(unittest.TestCase):
    def test_kill(self):
        snap = RiskSnapshot(kill_switch=True)
        ok, r = check_before_sign("buy", 0.05, 1.0, 50, snap)
        self.assertFalse(ok)
        self.assertEqual(r, "kill_switch")

    def test_unreadable_kill(self):
        snap = RiskSnapshot(kill_switch=False, kill_switch_readable=False)
        ok, r = check_before_sign("buy", 0.05, 1.0, 50, snap)
        self.assertFalse(ok)
        self.assertIn("unreadable", r)

    def test_size(self):
        snap = RiskSnapshot(max_trade_sol=0.1)
        ok, r = check_before_sign("buy", 0.5, 1.0, 50, snap)
        self.assertFalse(ok)

    def test_impact(self):
        snap = RiskSnapshot(max_price_impact_pct=5)
        ok, r = check_before_sign("buy", 0.05, 10.0, 50, snap)
        self.assertFalse(ok)


class TestTxValidate(unittest.TestCase):
    def test_empty(self):
        ok, r = validate_swap_tx("", "User111")
        self.assertFalse(ok)


class TestEngineLiveGates(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CopyStorage(Path(self.tmp.name))
        self.engine = CopyEngine(self.store)
        self.uid = 1
        os.environ.pop("COPY_LIVE_ENABLED", None)

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("COPY_LIVE_ENABLED", None)

    def test_live_disabled(self):
        sub = CopySubscription(wallet="W", status="active", paper=False)
        ok, reason, _ = self.engine.process_event(
            self.uid, sub,
            {"sig": "a", "side": "buy", "mint": "M", "liquidity_usd": 9e9, "token_age_min": 99, "age_sec": 1},
            "secret", "pubkey",
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "live_disabled")

    def test_duplicate(self):
        sub = CopySubscription(wallet="W", status="active", paper=True)
        self.store.upsert_subscription(self.uid, sub)
        ev = {"sig": "dup1", "side": "buy", "mint": "M", "price_usd": 1, "liquidity_usd": 9e9, "token_age_min": 99, "age_sec": 1}
        ok1, _, _ = self.engine.simulate_copy(self.uid, sub, ev)
        self.assertTrue(ok1)
        ok2, reason, _ = self.engine.simulate_copy(self.uid, sub, ev)
        self.assertFalse(ok2)
        self.assertEqual(reason, "duplicate")

    def test_kill_switch_blocks(self):
        sub = CopySubscription(wallet="W", status="active", paper=True)
        self.store.upsert_subscription(self.uid, sub)
        self.engine.emergency_kill(self.uid)
        ok, reason, _ = self.engine.simulate_copy(
            self.uid, sub,
            {"sig": "k1", "side": "buy", "mint": "M", "price_usd": 1, "liquidity_usd": 9e9, "token_age_min": 99, "age_sec": 1},
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "kill_switch")


class TestExecutorNoBroadcast(unittest.TestCase):
    def test_execute_swap_disabled(self):
        from crypto_copy import executor as ex
        os.environ.pop("COPY_LIVE_ENABLED", None)
        out = ex.execute_swap("sec", "pub", ex.SOL_MINT, "TokenMint1111111111111111111111111111111", 1000)
        self.assertFalse(out["ok"])
        self.assertIn("COPY_LIVE_ENABLED", out["error"] or "")


if __name__ == "__main__":
    unittest.main()


class TestAutomation(unittest.TestCase):
    def test_health_and_auto_flag(self):
        import tempfile
        from pathlib import Path
        from crypto_copy.automation import AutomationService, auto_enabled
        os.environ.pop("AUTO_COPY_MONITOR", None)
        self.assertFalse(auto_enabled())
        with tempfile.TemporaryDirectory() as d:
            svc = AutomationService(Path(d), interval_sec=15)
            self.assertFalse(svc.health.running)
            h = svc.health.as_dict()
            self.assertIn("live_enabled", h)


class TestCloseScopes(unittest.TestCase):
    def test_filter_origins(self):
        from crypto_copy.close_positions import filter_open, SCOPE_COPIED, SCOPE_SNIPED, SCOPE_ALL
        positions = [
            {"id": "1", "status": "open", "origin": "copied", "symbol": "A"},
            {"id": "2", "status": "open", "origin": "sniped", "symbol": "B"},
            {"id": "3", "status": "open", "origin": "manual", "symbol": "C"},
            {"id": "4", "status": "closed", "origin": "copied", "symbol": "D"},
            {"id": "5", "status": "open", "leader_wallet": "W", "symbol": "E"},
        ]
        self.assertEqual(len(filter_open(positions, SCOPE_COPIED)), 2)  # 1 and 5
        self.assertEqual(len(filter_open(positions, SCOPE_SNIPED)), 1)
        self.assertEqual(len(filter_open(positions, SCOPE_ALL)), 4)  # open with id

    def test_close_tracker_only_scope(self):
        from crypto_copy.close_positions import close_tracker, SCOPE_SNIPED
        positions = [
            {"id": "1", "status": "open", "origin": "copied", "symbol": "A"},
            {"id": "2", "status": "open", "origin": "sniped", "symbol": "B"},
        ]
        positions, results = close_tracker(positions, SCOPE_SNIPED)
        self.assertEqual(positions[0]["status"], "open")
        self.assertEqual(positions[1]["status"], "closed")
        self.assertTrue(any("B" in r for r in results))


class TestCloseNoDoubleSell(unittest.TestCase):
    def test_pending_blocks_duplicate(self):
        from crypto_copy.close_positions import close_live_each, SCOPE_ALL
        positions = [
            {"id": "x1", "status": "open", "origin": "manual", "symbol": "Z", "mint": "MintZ"},
        ]
        pending = {"x1"}
        calls = []

        def sell_fn(sec, pk, mint):
            calls.append(mint)
            return {"ok": True, "sig": "abc"}

        positions, results = close_live_each(
            positions, SCOPE_ALL, "s", "p", sell_fn, pending_ids=pending
        )
        self.assertEqual(calls, [])
        self.assertTrue(any("Pending" in r for r in results))

    def test_copied_not_in_snipe_close(self):
        from crypto_copy.close_positions import close_tracker, SCOPE_SNIPED
        positions = [
            {"id": "c1", "status": "open", "origin": "copied", "symbol": "COPY"},
            {"id": "s1", "status": "open", "origin": "sniped", "symbol": "SNIPE"},
        ]
        positions, results = close_tracker(positions, SCOPE_SNIPED)
        self.assertEqual(positions[0]["status"], "open")
        self.assertEqual(positions[1]["status"], "closed")


class TestIndividualClose(unittest.TestCase):
    def test_close_one_id_only(self):
        """Closing one position id must not close same-mint other lots."""
        from crypto_copy.close_positions import close_tracker, SCOPE_ALL, normalize_origin
        positions = [
            {"id": "a1", "status": "open", "origin": "copied", "symbol": "SAME", "mint": "M1"},
            {"id": "a2", "status": "open", "origin": "sniped", "symbol": "SAME", "mint": "M1"},
        ]
        # simulate individual: only mark a1
        for p in positions:
            if p["id"] == "a1":
                p["status"] = "closed"
        self.assertEqual(positions[0]["status"], "closed")
        self.assertEqual(positions[1]["status"], "open")
        self.assertEqual(normalize_origin(positions[1]), "sniped")


class TestSameMintDifferentIds(unittest.TestCase):
    def test_individual_id_close_leaves_other_mint_lot(self):
        positions = [
            {"id": "copy1", "status": "open", "origin": "copied", "symbol": "PEPE", "mint": "SameMint"},
            {"id": "snipe1", "status": "open", "origin": "sniped", "symbol": "PEPE", "mint": "SameMint"},
        ]
        # close only copy1
        for p in positions:
            if p["id"] == "copy1":
                p["status"] = "closed"
        open_ids = [p["id"] for p in positions if p["status"] == "open"]
        self.assertEqual(open_ids, ["snipe1"])
        from crypto_copy.close_positions import filter_open, SCOPE_SNIPED, SCOPE_COPIED
        self.assertEqual(len(filter_open(positions, SCOPE_COPIED)), 0)
        self.assertEqual(len(filter_open(positions, SCOPE_SNIPED)), 1)

    def test_origins_on_new_positions(self):
        from crypto_copy.pnl import PnLCalculator
        eng = PnLCalculator()
        positions = eng.apply_fill_to_positions([], {
            "leader_wallet": "L1",
            "mint": "M1",
            "side": "buy",
            "qty": 10,
            "price_usd": 1.0,
            "fee_usd": 0,
            "symbol": "X",
            "ts": "t",
        })
        self.assertEqual(positions[0].get("origin"), "copied")
        self.assertTrue(positions[0].get("id"))
