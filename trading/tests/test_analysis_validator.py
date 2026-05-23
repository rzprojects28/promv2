"""
Tests for the analysis_agent post-generation validator.

Pins down anti-hallucination behaviour: any thesis whose entry_price doesn't
match the live fetch, or which has no parseable stop, must be rejected.

Run from repo root:
    python3 -m unittest trading.tests.test_analysis_validator -v
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'trading', 'research'))

from analysis_validator import validate_thesis as _validate_thesis, extract_stop_price as _extract_stop_price


PRICES_LIVE = {
    'AAPL': {'price': 150.00, 'change_pct': 1.2},
    'TSLA': {'price': 200.00, 'change_pct': -0.5},
    'NVDA': {'price': 400.00, 'change_pct': 2.1},
}


def _t(**overrides):
    """Construct a baseline-valid thesis dict, then apply overrides."""
    base = {
        'ticker': 'AAPL',
        'direction': 'LONG',
        'entry_price': 150.00,
        'invalidation_conditions': 'Stock closes below $147 (20d swing low)',
    }
    base.update(overrides)
    return base


class TestExtractStopPrice(unittest.TestCase):

    def test_dollar_format(self):
        self.assertEqual(_extract_stop_price("closes below $147", 150.0), 147.0)

    def test_below_format(self):
        self.assertEqual(_extract_stop_price("below 147.5", 150.0), 147.5)

    def test_under_format(self):
        self.assertEqual(_extract_stop_price("under 148", 150.0), 148.0)

    def test_support_format(self):
        self.assertEqual(_extract_stop_price("147 support", 150.0), 147.0)

    def test_breaks_format(self):
        self.assertEqual(_extract_stop_price("breaks 147.25", 150.0), 147.25)

    def test_returns_none_when_no_match(self):
        self.assertIsNone(_extract_stop_price("if sector momentum reverses", 150.0))

    def test_filters_implausible_stop(self):
        # 30% away from entry — should be rejected as "not plausibly a stop"
        self.assertIsNone(_extract_stop_price("closes below $50", 150.0))
        self.assertIsNone(_extract_stop_price("closes below $250", 150.0))

    def test_picks_closest_to_entry(self):
        # Two candidates, picks the closer one
        text = "closes below $147 or below $145"
        self.assertEqual(_extract_stop_price(text, 150.0), 147.0)


class TestValidator(unittest.TestCase):

    def test_valid_thesis_passes(self):
        ok, reason = _validate_thesis(_t(), PRICES_LIVE)
        self.assertTrue(ok, f"expected pass, got: {reason}")

    def test_missing_ticker_rejected(self):
        ok, reason = _validate_thesis(_t(ticker=''), PRICES_LIVE)
        self.assertFalse(ok)
        self.assertIn('ticker', reason)

    def test_ticker_not_in_live_prices_rejected(self):
        # MSFT wasn't fetched
        ok, reason = _validate_thesis(_t(ticker='MSFT', entry_price=400.0,
                                        invalidation_conditions='below $395'),
                                      PRICES_LIVE)
        self.assertFalse(ok)
        self.assertIn('LIVE PRICES', reason)

    def test_entry_price_drift_rejected(self):
        # AAPL live is 150, thesis claims 200 — way off
        ok, reason = _validate_thesis(_t(entry_price=200.0,
                                        invalidation_conditions='below $195'),
                                      PRICES_LIVE)
        self.assertFalse(ok)
        self.assertIn('drifts', reason)

    def test_entry_price_within_tolerance_passes(self):
        # 0.5% off — within 1% tolerance
        ok, _ = _validate_thesis(_t(entry_price=150.75), PRICES_LIVE)
        self.assertTrue(ok)

    def test_unparseable_stop_rejected(self):
        ok, reason = _validate_thesis(
            _t(invalidation_conditions='if catalyst momentum reverses'),
            PRICES_LIVE,
        )
        self.assertFalse(ok)
        self.assertIn('parseable stop', reason)

    def test_implausible_stop_rejected(self):
        # Stop 60% away — fails the 30% sanity gate inside _extract_stop_price
        ok, reason = _validate_thesis(
            _t(invalidation_conditions='closes below $50'),
            PRICES_LIVE,
        )
        self.assertFalse(ok)

    def test_sector_etf_rejected(self):
        ok, reason = _validate_thesis(
            _t(ticker='XLK', entry_price=200.0,
               invalidation_conditions='below $195'),
            PRICES_LIVE,
        )
        self.assertFalse(ok)
        self.assertIn('ETF', reason)

    def test_broad_market_etf_rejected(self):
        ok, reason = _validate_thesis(
            _t(ticker='SPY', entry_price=500.0,
               invalidation_conditions='below $495'),
            PRICES_LIVE,
        )
        self.assertFalse(ok)

    def test_non_dict_rejected(self):
        ok, reason = _validate_thesis("not a dict", PRICES_LIVE)
        self.assertFalse(ok)

    def test_zero_entry_price_rejected(self):
        ok, reason = _validate_thesis(_t(entry_price=0), PRICES_LIVE)
        self.assertFalse(ok)

    def test_non_numeric_entry_price_rejected(self):
        ok, reason = _validate_thesis(_t(entry_price='approximately 150'), PRICES_LIVE)
        self.assertFalse(ok)

    def test_ticker_case_insensitive(self):
        ok, _ = _validate_thesis(_t(ticker='aapl'), PRICES_LIVE)
        self.assertTrue(ok)


if __name__ == '__main__':
    unittest.main()
