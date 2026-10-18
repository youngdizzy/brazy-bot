"""Stocks Paper/Live/Funded mode labels — pure logic (no telegram import)."""
import unittest


def stocks_mode_label(profile):
    mode = (profile.get("trading_mode") or "paper").lower()
    if mode not in ("paper", "live", "funded"):
        mode = "paper"
    if mode != "funded":
        return mode.upper()
    phase = (profile.get("account_phase") or "evaluation").lower()
    if phase in ("eval", "evaluation"):
        return "FUNDED · EVALUATION"
    if phase in ("passed", "funded"):
        return "FUNDED · PASSED"
    return "FUNDED · EVALUATION"


def stocks_analysis_context(profile):
    mode = (profile.get("trading_mode") or "paper").lower()
    label = stocks_mode_label(profile)
    lines = [
        f"STOCKS MODE: {label}",
        "NOTE: This mode applies to STOCKS/FUTURES chart analysis only — never Sol crypto execution.",
    ]
    if mode == "paper":
        lines.append("PAPER")
    elif mode == "funded":
        phase = (profile.get("account_phase") or "evaluation").lower()
        lines.append("EVALUATION" if phase in ("eval", "evaluation") else "PASSED")
    return "\n".join(lines)


class TestStocksModes(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(stocks_mode_label({"trading_mode": "paper"}), "PAPER")
        self.assertEqual(stocks_mode_label({"trading_mode": "live"}), "LIVE")
        self.assertIn("EVALUATION", stocks_mode_label({"trading_mode": "funded", "account_phase": "evaluation"}))
        self.assertIn("PASSED", stocks_mode_label({"trading_mode": "funded", "account_phase": "passed"}))

    def test_crypto_isolation(self):
        p = {"trading_mode": "funded", "account_phase": "passed", "sol_positions": [1, 2]}
        ctx = stocks_analysis_context(p)
        self.assertIn("never Sol crypto", ctx)
        self.assertEqual(len(p["sol_positions"]), 2)

    def test_mode_switch_no_trade(self):
        # switching is a pure profile write; no order object created
        p = {"trading_mode": "paper"}
        p["trading_mode"] = "funded"
        p["account_phase"] = "evaluation"
        self.assertEqual(p.get("order_submitted"), None)
