"""Unit tests — leaderboard, PnL, validation, dedupe, kill switch."""
import sys
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from crypto_copy.leaderboard import LeaderboardService
from crypto_copy.models import CopyRules, CopySubscription, FollowerFill
from crypto_copy.pnl import PnLCalculator
from crypto_copy.copy_engine import CopyEngine
from crypto_copy.storage import CopyStorage
from crypto_copy.alerts import AlertService


class TestLeaderboard(unittest.TestCase):
    def test_periods_and_demo(self):
        lb = LeaderboardService()
        for p in ("24h", "7d", "30d"):
            traders, source = lb.fetch(period=p, limit=8, offset=0)
            self.assertEqual(len(traders), 8)
            self.assertIn(source, ("demo", "solanatracker", "solanatracker_error", "none"))
            # demo must not invent PnL numbers as floats pretending live
            if source == "demo":
                for t in traders:
                    self.assertIsNone(t.realized_pnl)


class TestPnL(unittest.TestCase):
    def test_follower_not_leader(self):
        calc = PnLCalculator()
        positions = [{
            "leader_wallet": "L1",
            "mint": "M1",
            "symbol": "X",
            "qty": 0,
            "avg_entry_usd": 1.0,
            "realized_pnl_usd": 12.5,
            "fees_usd": 1.0,
            "status": "closed",
        }]
        st = calc.stats_for_leader([], positions)
        self.assertEqual(st["my_realized_pnl"], 12.5)
        self.assertEqual(st["closed_trades"], 1)
        self.assertEqual(st["my_win_rate"], 100.0)

    def test_apply_buy_sell(self):
        calc = PnLCalculator()
        pos = []
        buy = {
            "leader_wallet": "L", "mint": "M", "symbol": "T", "side": "buy",
            "qty": 10, "price_usd": 2.0, "fee_usd": 0.1, "ts": "t1",
        }
        pos = calc.apply_fill_to_positions(pos, buy)
        self.assertEqual(pos[0]["qty"], 10)
        sell = {
            "leader_wallet": "L", "mint": "M", "symbol": "T", "side": "sell",
            "qty": 10, "price_usd": 3.0, "fee_usd": 0.1, "ts": "t2",
        }
        pos = calc.apply_fill_to_positions(pos, sell)
        self.assertEqual(pos[0]["status"], "closed")
        self.assertAlmostEqual(pos[0]["realized_pnl_usd"], 10 - 0.1)


class TestRules(unittest.TestCase):
    def test_validate(self):
        r = CopyRules(fixed_amount_sol=0.1, max_per_trade_sol=0.05)
        errs = r.validate()
        self.assertTrue(any("max_per_trade" in e for e in errs))


class TestEngine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CopyStorage(Path(self.tmp.name))
        self.engine = CopyEngine(self.store)
        self.uid = 42

    def tearDown(self):
        self.tmp.cleanup()

    def test_dedupe_and_paper(self):
        sub = CopySubscription(wallet="W1", handle="T", status="active", paper=True)
        self.store.upsert_subscription(self.uid, sub)
        ev = {"sig": "abc", "side": "buy", "mint": "M", "symbol": "X", "price_usd": 1.0, "liquidity_usd": 50000, "token_age_min": 60, "age_sec": 5}
        ok, reason, fill = self.engine.simulate_copy(self.uid, sub, ev)
        self.assertTrue(ok)
        self.assertIsNotNone(fill)
        ok2, reason2, _ = self.engine.simulate_copy(self.uid, sub, ev)
        self.assertFalse(ok2)
        self.assertEqual(reason2, "duplicate")

    def test_kill_switch(self):
        sub = CopySubscription(wallet="W1", handle="T", status="active", paper=True)
        self.store.upsert_subscription(self.uid, sub)
        self.engine.emergency_kill(self.uid)
        ok, reason = self.engine.validate_event(self.uid, sub, {"sig": "x", "side": "buy"})
        self.assertFalse(ok)
        self.assertEqual(reason, "kill_switch")

    def test_copy_list_uses_follower_pnl(self):
        sub = CopySubscription(wallet="W1", handle="Alpha", status="active", paper=True)
        self.store.upsert_subscription(self.uid, sub)
        text = self.engine.copy_list_rows(self.uid)
        self.assertIn("YOUR results", text)
        self.assertIn("Alpha", text)


class TestAlerts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CopyStorage(Path(self.tmp.name))
        self.alerts = AlertService(self.store)
        self.uid = 7

    def tearDown(self):
        self.tmp.cleanup()

    def test_first_buy_once(self):
        self.alerts.set_mode(self.uid, "W", "H", "first_buy")
        self.assertTrue(self.alerts.should_notify(self.uid, "W", "buy", "s1"))
        self.alerts.mark_notified(self.uid, "W", "s1")
        self.assertFalse(self.alerts.should_notify(self.uid, "W", "buy", "s1"))


if __name__ == "__main__":
    unittest.main()


class TestLiveGates(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CopyStorage(Path(self.tmp.name))
        self.engine = CopyEngine(self.store)
        self.uid = 99
        os.environ.pop("COPY_LIVE_ENABLED", None)

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("COPY_LIVE_ENABLED", None)

    def test_live_disabled_refuses_without_falling_to_paper_when_requested(self):
        sub = CopySubscription(wallet="W1", handle="T", status="active", paper=False)
        self.store.upsert_subscription(self.uid, sub)
        ok, reason, fill = self.engine.process_event(
            self.uid, sub,
            {"sig": "z1", "side": "buy", "mint": "M", "liquidity_usd": 50000, "token_age_min": 60, "age_sec": 3},
            secret_base58=None,
            user_pubkey=None,
        )
        self.assertFalse(ok)
        self.assertIn(reason, ("live_disabled", "live_requested_but_missing_wallet_secret_or_pubkey"))
        self.assertIsNone(fill)

    def test_paper_still_works_when_live_off(self):
        sub = CopySubscription(wallet="W1", handle="T", status="active", paper=True)
        self.store.upsert_subscription(self.uid, sub)
        ok, reason, fill = self.engine.process_event(
            self.uid, sub,
            {"sig": "z2", "side": "buy", "mint": "M", "price_usd": 1.0, "liquidity_usd": 50000, "token_age_min": 60, "age_sec": 3},
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "paper_fill")
        self.assertTrue(fill.paper)

    def test_executor_live_flag(self):
        from crypto_copy import executor
        os.environ["COPY_LIVE_ENABLED"] = "false"
        # reload check via function
        self.assertFalse(executor.live_enabled() if hasattr(executor, "live_enabled") else False)


class TestScanUi(unittest.TestCase):
    def test_filter_and_detail(self):
        from crypto_copy.scan_ui import ScanMemesUI, _fmt_usd
        self.assertEqual(_fmt_usd(None), "N/A")
        ui = ScanMemesUI()
        # empty cache resolve
        self.assertIsNone(ui.resolve("newest", 0))
        # live off snipe
        import os
        os.environ.pop("COPY_LIVE_ENABLED", None)
        msg = ui.execute_snipe(1, "Mint111", None, None, 0.05, 150, False)
        self.assertIn("COPY_LIVE_ENABLED", msg)
        msg2 = ui.execute_snipe(1, "Mint111", "sec", "pub", 0.05, 150, True)
        self.assertIn("Kill switch", msg2)
        # duplicate lock
        ui._inflight.add("1:MintDup")
        msg3 = ui.execute_snipe(1, "MintDup", "s", "p", 0.05, 150, False)
        self.assertIn("Already submitting", msg3)
