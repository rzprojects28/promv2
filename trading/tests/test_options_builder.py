"""
Unit tests for options_builder.

Tests the parser, structural validators, DTE bounds, and sizing math.
Does NOT test the IBKR contract builders (those require ib_insync + a live
gateway). The parser + sizing math is the high-value testable layer.

Run:
    python3 -m unittest trading.tests.test_options_builder -v
"""
import os
import sys
import unittest
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'trading', 'execution'))

from options_builder import (
    parse_options_structure,
    check_dte_bounds,
    max_loss_per_contract_usd,
    position_size_contracts,
    ALLOWED_TYPES,
    BLOCKED_TYPES,
)


# Helper: today is Sat 2026-05-23 for stable DTE math in tests
TODAY = date(2026, 5, 23)


def _leg(action, right, strike, expiry, ratio=1):
    return {"action": action, "right": right, "strike": strike,
            "expiry": expiry, "ratio": ratio}


# ────────────────────────────────────────────────────────────────────
# Parse + structural validation
# ────────────────────────────────────────────────────────────────────

class TestParseLongCall(unittest.TestCase):

    def test_valid(self):
        s = parse_options_structure({
            "type": "long_call",
            "underlying": "AAPL",
            "legs": [_leg("BUY", "C", 150, "2026-07-18")],
        })
        self.assertEqual(s.type, "long_call")
        self.assertEqual(s.underlying, "AAPL")
        self.assertEqual(len(s.legs), 1)
        self.assertEqual(s.legs[0].to_ibkr_expiry(), "20260718")

    def test_wrong_right_rejected(self):
        with self.assertRaisesRegex(ValueError, "must be BUY C"):
            parse_options_structure({
                "type": "long_call",
                "underlying": "AAPL",
                "legs": [_leg("BUY", "P", 150, "2026-07-18")],
            })

    def test_sell_rejected(self):
        with self.assertRaisesRegex(ValueError, "must be BUY C"):
            parse_options_structure({
                "type": "long_call",
                "underlying": "AAPL",
                "legs": [_leg("SELL", "C", 150, "2026-07-18")],
            })


class TestParseBullCallSpread(unittest.TestCase):

    def test_valid(self):
        s = parse_options_structure({
            "type": "bull_call_spread",
            "underlying": "AAPL",
            "legs": [
                _leg("BUY",  "C", 150, "2026-07-18"),
                _leg("SELL", "C", 160, "2026-07-18"),
            ],
        })
        self.assertEqual(s.net_action(), "debit")

    def test_inverted_strikes_rejected(self):
        # BUY higher strike + SELL lower = backwards (would be bearish credit)
        with self.assertRaisesRegex(ValueError, "BUY strike must be lower"):
            parse_options_structure({
                "type": "bull_call_spread",
                "underlying": "AAPL",
                "legs": [
                    _leg("BUY",  "C", 160, "2026-07-18"),
                    _leg("SELL", "C", 150, "2026-07-18"),
                ],
            })

    def test_different_expiries_rejected(self):
        with self.assertRaisesRegex(ValueError, "same expiry"):
            parse_options_structure({
                "type": "bull_call_spread",
                "underlying": "AAPL",
                "legs": [
                    _leg("BUY",  "C", 150, "2026-07-18"),
                    _leg("SELL", "C", 160, "2026-08-15"),
                ],
            })


class TestParseBullPutSpread(unittest.TestCase):
    """Credit spread — SELL higher put, BUY lower put."""

    def test_valid(self):
        s = parse_options_structure({
            "type": "bull_put_spread",
            "underlying": "AAPL",
            "legs": [
                _leg("SELL", "P", 145, "2026-07-18"),
                _leg("BUY",  "P", 140, "2026-07-18"),
            ],
        })
        self.assertEqual(s.net_action(), "credit")


class TestParseCalendarSpread(unittest.TestCase):

    def test_valid(self):
        s = parse_options_structure({
            "type": "calendar_call_spread",
            "underlying": "AAPL",
            "legs": [
                _leg("SELL", "C", 155, "2026-06-20"),
                _leg("BUY",  "C", 155, "2026-08-15"),
            ],
        })
        self.assertEqual(s.type, "calendar_call_spread")

    def test_strikes_must_match(self):
        with self.assertRaisesRegex(ValueError, "same strike"):
            parse_options_structure({
                "type": "calendar_call_spread",
                "underlying": "AAPL",
                "legs": [
                    _leg("SELL", "C", 155, "2026-06-20"),
                    _leg("BUY",  "C", 160, "2026-08-15"),
                ],
            })

    def test_short_leg_must_be_nearer(self):
        with self.assertRaisesRegex(ValueError, "SELL must be the near-dated"):
            parse_options_structure({
                "type": "calendar_call_spread",
                "underlying": "AAPL",
                "legs": [
                    _leg("SELL", "C", 155, "2026-08-15"),
                    _leg("BUY",  "C", 155, "2026-06-20"),
                ],
            })


class TestParseCalendarRatioSpread(unittest.TestCase):

    def test_valid(self):
        s = parse_options_structure({
            "type": "calendar_ratio_call_spread",
            "underlying": "AAPL",
            "legs": [
                _leg("SELL", "C", 160, "2026-06-20", ratio=2),
                _leg("BUY",  "C", 155, "2026-08-15", ratio=1),
            ],
        })
        self.assertEqual(s.legs[0].ratio, 2)

    def test_equal_ratios_rejected(self):
        # equal ratios = ordinary calendar, not ratio
        with self.assertRaisesRegex(ValueError, "unequal leg ratios"):
            parse_options_structure({
                "type": "calendar_ratio_call_spread",
                "underlying": "AAPL",
                "legs": [
                    _leg("SELL", "C", 160, "2026-06-20", ratio=1),
                    _leg("BUY",  "C", 155, "2026-08-15", ratio=1),
                ],
            })


class TestBlockedTypes(unittest.TestCase):
    """All naked shorts / iron condors / butterflies / straddles must be rejected."""

    def test_naked_short_call_blocked(self):
        with self.assertRaisesRegex(ValueError, "blocked"):
            parse_options_structure({"type": "naked_short_call", "underlying": "AAPL",
                                     "legs": [_leg("SELL", "C", 160, "2026-07-18")]})

    def test_naked_short_put_blocked(self):
        with self.assertRaisesRegex(ValueError, "blocked"):
            parse_options_structure({"type": "naked_short_put", "underlying": "AAPL",
                                     "legs": [_leg("SELL", "P", 140, "2026-07-18")]})

    def test_iron_condor_blocked(self):
        with self.assertRaisesRegex(ValueError, "blocked"):
            parse_options_structure({"type": "iron_condor", "underlying": "AAPL", "legs": []})

    def test_short_strangle_blocked(self):
        with self.assertRaisesRegex(ValueError, "blocked"):
            parse_options_structure({"type": "short_strangle", "underlying": "AAPL", "legs": []})

    def test_unknown_type_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown"):
            parse_options_structure({"type": "magic_unicorn_spread", "underlying": "AAPL",
                                     "legs": [_leg("BUY", "C", 150, "2026-07-18")]})


class TestBasicValidation(unittest.TestCase):

    def test_missing_type(self):
        with self.assertRaisesRegex(ValueError, "type missing"):
            parse_options_structure({"underlying": "AAPL", "legs": []})

    def test_missing_underlying(self):
        with self.assertRaisesRegex(ValueError, "underlying missing"):
            parse_options_structure({"type": "long_call",
                                     "legs": [_leg("BUY", "C", 150, "2026-07-18")]})

    def test_empty_legs(self):
        with self.assertRaisesRegex(ValueError, "non-empty list"):
            parse_options_structure({"type": "long_call", "underlying": "AAPL", "legs": []})

    def test_bad_expiry_format(self):
        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            parse_options_structure({"type": "long_call", "underlying": "AAPL",
                                     "legs": [_leg("BUY", "C", 150, "07/18/2026")]})

    def test_zero_strike(self):
        with self.assertRaisesRegex(ValueError, "strike must be > 0"):
            parse_options_structure({"type": "long_call", "underlying": "AAPL",
                                     "legs": [_leg("BUY", "C", 0, "2026-07-18")]})


# ────────────────────────────────────────────────────────────────────
# DTE bounds
# ────────────────────────────────────────────────────────────────────

class TestDTEBounds(unittest.TestCase):

    def test_vertical_inside_window(self):
        # 2026-07-18 from 2026-05-23 = 56 days → in [30, 60]
        s = parse_options_structure({
            "type": "bull_call_spread", "underlying": "AAPL",
            "legs": [
                _leg("BUY",  "C", 150, "2026-07-18"),
                _leg("SELL", "C", 160, "2026-07-18"),
            ],
        })
        ok, reason = check_dte_bounds(s, TODAY)
        self.assertTrue(ok, reason)

    def test_vertical_too_short_dte(self):
        # 2026-06-05 from 2026-05-23 = 13 days → outside [30, 60]
        s = parse_options_structure({
            "type": "bull_call_spread", "underlying": "AAPL",
            "legs": [
                _leg("BUY",  "C", 150, "2026-06-05"),
                _leg("SELL", "C", 160, "2026-06-05"),
            ],
        })
        ok, reason = check_dte_bounds(s, TODAY)
        self.assertFalse(ok)
        self.assertIn("outside 30-60", reason)

    def test_vertical_too_long_dte(self):
        # 2026-09-18 from 2026-05-23 = 118 days → outside [30, 60]
        s = parse_options_structure({
            "type": "bull_call_spread", "underlying": "AAPL",
            "legs": [
                _leg("BUY",  "C", 150, "2026-09-18"),
                _leg("SELL", "C", 160, "2026-09-18"),
            ],
        })
        ok, _ = check_dte_bounds(s, TODAY)
        self.assertFalse(ok)

    def test_calendar_short_leg_window(self):
        # short 2026-06-20 → 28 days (in 7-45). long 2026-08-15 → 84 days (in 45-120). OK.
        s = parse_options_structure({
            "type": "calendar_call_spread", "underlying": "AAPL",
            "legs": [
                _leg("SELL", "C", 155, "2026-06-20"),
                _leg("BUY",  "C", 155, "2026-08-15"),
            ],
        })
        ok, _ = check_dte_bounds(s, TODAY)
        self.assertTrue(ok)

    def test_calendar_short_leg_too_far(self):
        # short 2026-08-15 → 84 days > 45 → reject
        s = parse_options_structure({
            "type": "calendar_call_spread", "underlying": "AAPL",
            "legs": [
                _leg("SELL", "C", 155, "2026-08-15"),
                _leg("BUY",  "C", 155, "2026-11-20"),
            ],
        })
        ok, reason = check_dte_bounds(s, TODAY)
        self.assertFalse(ok)
        self.assertIn("short leg", reason)


# ────────────────────────────────────────────────────────────────────
# Sizing math
# ────────────────────────────────────────────────────────────────────

class TestMaxLossPerContract(unittest.TestCase):

    def test_long_call_max_loss_is_premium(self):
        # premium = $3.50/share → $350 per contract
        s = parse_options_structure({
            "type": "long_call", "underlying": "AAPL",
            "legs": [_leg("BUY", "C", 150, "2026-07-18")],
        })
        prices = {("BUY", "C", 150.0, "2026-07-18"): 3.50}
        self.assertEqual(max_loss_per_contract_usd(s, prices), 350.0)

    def test_bull_call_spread_max_loss_is_net_debit(self):
        # BUY 150C @ 5.00 SELL 160C @ 1.50 → net debit 3.50 → $350 max loss
        s = parse_options_structure({
            "type": "bull_call_spread", "underlying": "AAPL",
            "legs": [
                _leg("BUY",  "C", 150, "2026-07-18"),
                _leg("SELL", "C", 160, "2026-07-18"),
            ],
        })
        prices = {
            ("BUY",  "C", 150.0, "2026-07-18"): 5.00,
            ("SELL", "C", 160.0, "2026-07-18"): 1.50,
        }
        self.assertEqual(max_loss_per_contract_usd(s, prices), 350.0)

    def test_bull_put_spread_max_loss_is_width_minus_credit(self):
        # SELL 145P @ 2.00 BUY 140P @ 0.80 → credit 1.20, width 5 → max loss 3.80 → $380
        s = parse_options_structure({
            "type": "bull_put_spread", "underlying": "AAPL",
            "legs": [
                _leg("SELL", "P", 145, "2026-07-18"),
                _leg("BUY",  "P", 140, "2026-07-18"),
            ],
        })
        prices = {
            ("SELL", "P", 145.0, "2026-07-18"): 2.00,
            ("BUY",  "P", 140.0, "2026-07-18"): 0.80,
        }
        # (5.0 - 1.20) * 100 = 380
        self.assertAlmostEqual(max_loss_per_contract_usd(s, prices), 380.0)

    def test_calendar_max_loss_is_net_debit(self):
        # SELL 155C near @ 2.00, BUY 155C far @ 5.00 → debit 3.00 → $300
        s = parse_options_structure({
            "type": "calendar_call_spread", "underlying": "AAPL",
            "legs": [
                _leg("SELL", "C", 155, "2026-06-20"),
                _leg("BUY",  "C", 155, "2026-08-15"),
            ],
        })
        prices = {
            ("SELL", "C", 155.0, "2026-06-20"): 2.00,
            ("BUY",  "C", 155.0, "2026-08-15"): 5.00,
        }
        self.assertEqual(max_loss_per_contract_usd(s, prices), 300.0)


class TestPositionSizeContracts(unittest.TestCase):

    def test_basic_sizing(self):
        # 0.5% of $100k = $500 budget. Max loss $350/contract → 1 contract
        self.assertEqual(position_size_contracts(350.0, 100_000, 0.5), 1)

    def test_round_down(self):
        # Budget $500, max_loss $300 → 1 contract (not 1.66)
        self.assertEqual(position_size_contracts(300.0, 100_000, 0.5), 1)

    def test_multiple_contracts(self):
        # Budget $500, max_loss $100 → 5 contracts
        self.assertEqual(position_size_contracts(100.0, 100_000, 0.5), 5)

    def test_skip_if_too_expensive(self):
        # Budget $500, max_loss $1000 → 0 (skip the trade)
        self.assertEqual(position_size_contracts(1000.0, 100_000, 0.5), 0)

    def test_zero_max_loss_returns_zero(self):
        self.assertEqual(position_size_contracts(0, 100_000, 0.5), 0)

    def test_custom_risk_pct(self):
        # 1.0% of $100k = $1000 budget, $200/contract → 5
        self.assertEqual(position_size_contracts(200.0, 100_000, 1.0), 5)


if __name__ == '__main__':
    unittest.main()
