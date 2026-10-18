import unittest
from crypto_copy.rr import reward_to_risk, meets_min_rr
from crypto_copy.order_state import Order, OrderState
from crypto_copy.position_manager import evaluate_exit, attach_risk_levels


class TestRR(unittest.TestCase):
    def test_exact_1_5(self):
        # entry 100, stop 90, tp 115 => reward 15 risk 10 = 1.5 gross
        rr = reward_to_risk(100, 90, 115, fee_frac=0)
        self.assertAlmostEqual(rr, 1.5, places=5)
        ok, val, reason = meets_min_rr(100, 90, 115, min_rr=1.5, fee_frac=0)
        self.assertTrue(ok)

    def test_below_min(self):
        ok, val, reason = meets_min_rr(100, 90, 110, min_rr=1.5, fee_frac=0)
        self.assertFalse(ok)

    def test_invalid_stop(self):
        ok, val, reason = meets_min_rr(100, 105, 120, min_rr=1.5)
        self.assertFalse(ok)


class TestOrderState(unittest.TestCase):
    def test_transitions(self):
        o = Order("buy", "Mint", 1000)
        self.assertEqual(o.state, OrderState.CREATED)
        o.advance(OrderState.QUOTED)
        o.advance(OrderState.SUBMITTED, sig="abc")
        self.assertEqual(o.sig, "abc")
        d = o.to_dict()
        self.assertEqual(d["state"], "SUBMITTED")


class TestPosMgr(unittest.TestCase):
    def test_stop(self):
        pos = {"status": "open", "stop": 90, "take_profit": 120, "entry": 100}
        act, reason = evaluate_exit(pos, 89)
        self.assertEqual(act, "sell")
        self.assertIn("stop", reason)

    def test_tp(self):
        pos = {"status": "open", "stop": 90, "take_profit": 120, "entry": 100}
        act, reason = evaluate_exit(pos, 121)
        self.assertEqual(act, "sell")

    def test_hold(self):
        pos = {"status": "open", "stop": 90, "take_profit": 120, "entry": 100}
        act, reason = evaluate_exit(pos, 105)
        self.assertIsNone(act)

    def test_attach_rr(self):
        pos = {"avg_entry_usd": 100, "status": "open"}
        pos, reason = attach_risk_levels(pos, 90, 115, min_rr=1.5, fee_frac=0)
        self.assertEqual(reason, "ok")
        self.assertAlmostEqual(pos["planned_rr"], 1.5)
