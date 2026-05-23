"""
Unit tests for report.stats.

Run from repo root:
    python3 -m unittest report.tests.test_stats -v

Each test pins down a bug the legacy daily/weekly pipeline had, or the
contract for the standalone report package. If a test fails, the Telegram
numbers will be wrong.
"""
import unittest
from datetime import datetime, date, timezone
from zoneinfo import ZoneInfo

import sys
import os
# Repo root = three levels up: tests/ → report/ → repo
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from report import stats as R


SGT = ZoneInfo("Asia/Singapore")


def at(y, m, d, hh=12, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=SGT)


# ────────────────────────────────────────────────────────────────────
# Week boundary tests — verifies "fire Sat morning SGT, report Mon→Sun"
# ────────────────────────────────────────────────────────────────────

class TestWeekBounds(unittest.TestCase):

    def test_saturday_returns_current_week(self):
        # Sat fires after US Friday close. The Mon-Fri trading week of THIS
        # calendar week just ended → window is Mon-Sun of this week.
        start, end = R.week_bounds_sgt(at(2026, 5, 23))   # Sat
        self.assertEqual(start, date(2026, 5, 18))         # this Mon
        self.assertEqual(end,   date(2026, 5, 24))         # this Sun

    def test_sunday_returns_same_current_week(self):
        start, end = R.week_bounds_sgt(at(2026, 5, 24))   # Sun
        self.assertEqual(start, date(2026, 5, 18))
        self.assertEqual(end,   date(2026, 5, 24))

    def test_friday_returns_prior_completed_week(self):
        # Fri is mid-week (US Fri close hasn't happened yet in SGT until ~5am Sat)
        # → most recently completed week is last Mon-Sun.
        start, end = R.week_bounds_sgt(at(2026, 5, 22))
        self.assertEqual(start, date(2026, 5, 11))
        self.assertEqual(end,   date(2026, 5, 17))

    def test_monday_returns_prior_completed_week(self):
        start, end = R.week_bounds_sgt(at(2026, 5, 25))   # Mon
        self.assertEqual(start, date(2026, 5, 18))
        self.assertEqual(end,   date(2026, 5, 24))

    def test_window_is_seven_days_inclusive(self):
        start, end = R.week_bounds_sgt(at(2026, 5, 25))
        self.assertEqual((end - start).days, 6)

    def test_start_is_always_monday(self):
        for d in range(1, 28):
            start, _ = R.week_bounds_sgt(at(2026, 5, d))
            self.assertEqual(start.weekday(), 0, f"failed on 2026-05-{d}")

    def test_end_is_always_sunday(self):
        for d in range(1, 28):
            _, end = R.week_bounds_sgt(at(2026, 5, d))
            self.assertEqual(end.weekday(), 6, f"failed on 2026-05-{d}")


class TestDateParsing(unittest.TestCase):

    def test_parse_date_yyyy_mm_dd(self):
        self.assertEqual(R.parse_date("2026-05-23"), date(2026, 5, 23))

    def test_parse_date_iso_with_time(self):
        self.assertEqual(R.parse_date("2026-05-23T10:30:00"), date(2026, 5, 23))

    def test_parse_date_empty_returns_none(self):
        self.assertIsNone(R.parse_date(""))
        self.assertIsNone(R.parse_date(None))

    def test_parse_date_garbage_returns_none(self):
        self.assertIsNone(R.parse_date("not a date"))


class TestSafeFloat(unittest.TestCase):

    def test_handles_nan(self):
        self.assertEqual(R.safe_float(float("nan")), 0.0)

    def test_handles_none(self):
        self.assertEqual(R.safe_float(None), 0.0)

    def test_handles_string_number(self):
        self.assertEqual(R.safe_float("3.5"), 3.5)


# ────────────────────────────────────────────────────────────────────
# Filter
# ────────────────────────────────────────────────────────────────────

class TestFilterByDateRange(unittest.TestCase):

    def test_inclusive_bounds(self):
        rows = [
            {"exit_date": "2026-05-17"},   # in
            {"exit_date": "2026-05-11"},   # in (start)
            {"exit_date": "2026-05-18"},   # out
            {"exit_date": "2026-05-10"},   # out
        ]
        out = R.filter_by_date_range(rows, "exit_date", date(2026, 5, 11), date(2026, 5, 17))
        self.assertEqual(len(out), 2)

    def test_missing_date_field_ignored(self):
        rows = [{"exit_date": ""}, {"exit_date": None}, {"exit_date": "2026-05-15"}]
        out = R.filter_by_date_range(rows, "exit_date", date(2026, 5, 11), date(2026, 5, 17))
        self.assertEqual(len(out), 1)


# ────────────────────────────────────────────────────────────────────
# PnL
# ────────────────────────────────────────────────────────────────────

class TestPositionPnlUSD(unittest.TestCase):
    """
    Pins down THE biggest legacy bug: sum-of-percents != portfolio return.
    Trades must be PnL-weighted by entry_size_usd.
    """

    def test_basic(self):
        p = {"pnl_pct": 10.0, "entry_size_usd": 5000.0}
        self.assertEqual(R.position_pnl_usd(p), 500.0)

    def test_loss(self):
        p = {"pnl_pct": -4.0, "entry_size_usd": 2500.0}
        self.assertEqual(R.position_pnl_usd(p), -100.0)

    def test_unequal_sizes_dont_average_to_simple_pct_sum(self):
        a = {"pnl_pct": 10.0, "entry_size_usd": 1000.0}   # +$100
        b = {"pnl_pct": -5.0, "entry_size_usd": 10000.0}  # -$500
        total = R.position_pnl_usd(a) + R.position_pnl_usd(b)
        self.assertEqual(total, -400.0)
        # The legacy "sum of pnl_pct" would give +5% which is misleading
        legacy_misleading = 10.0 + (-5.0)
        self.assertEqual(legacy_misleading, 5.0)


# ────────────────────────────────────────────────────────────────────
# Risk
# ────────────────────────────────────────────────────────────────────

class TestPositionBudgetedRisk(unittest.TestCase):

    def test_with_stop(self):
        p = {"risk_per_share": 2.0, "entry_qty": 50, "entry_size_usd": 7500}
        usd, has = R.position_budgeted_risk_usd(p)
        self.assertEqual(usd, 100.0)
        self.assertTrue(has)

    def test_without_stop_uses_full_size(self):
        # Per user spec: "treat unstopped positions as 100% at risk"
        p = {"risk_per_share": None, "entry_qty": 50, "entry_size_usd": 7500}
        usd, has = R.position_budgeted_risk_usd(p)
        self.assertEqual(usd, 7500.0)
        self.assertFalse(has)

    def test_zero_qty(self):
        p = {"risk_per_share": 2.0, "entry_qty": 0, "entry_size_usd": 0}
        usd, has = R.position_budgeted_risk_usd(p)
        self.assertEqual(usd, 0.0)


class TestPositionLiveRisk(unittest.TestCase):

    def test_long_above_stop(self):
        # entry $100, stop $95, current $110, qty 50 → live risk = (110-95)*50 = 750
        p = {"direction": "LONG", "calculated_stop": 95, "entry_qty": 50,
             "entry_size_usd": 5000, "entry_price": 100}
        usd, has = R.position_live_risk_usd(p, 110.0)
        self.assertEqual(usd, 750.0)
        self.assertTrue(has)

    def test_long_below_stop_returns_zero(self):
        # Should have triggered exit, but defensively return 0 not negative
        p = {"direction": "LONG", "calculated_stop": 95, "entry_qty": 50,
             "entry_size_usd": 5000, "entry_price": 100}
        usd, _ = R.position_live_risk_usd(p, 90.0)
        self.assertEqual(usd, 0.0)

    def test_short_below_stop(self):
        p = {"direction": "SHORT", "calculated_stop": 105, "entry_qty": 50,
             "entry_size_usd": 5000, "entry_price": 100}
        usd, has = R.position_live_risk_usd(p, 95.0)
        self.assertEqual(usd, (105 - 95) * 50)
        self.assertTrue(has)

    def test_no_price_falls_back_to_full_size(self):
        p = {"direction": "LONG", "calculated_stop": 95, "entry_qty": 50,
             "entry_size_usd": 5000}
        usd, has = R.position_live_risk_usd(p, None)
        self.assertEqual(usd, 5000.0)
        self.assertFalse(has)

    def test_no_stop_falls_back_to_full_size(self):
        p = {"direction": "LONG", "calculated_stop": None, "entry_qty": 50,
             "entry_size_usd": 5000}
        usd, has = R.position_live_risk_usd(p, 110.0)
        self.assertEqual(usd, 5000.0)
        self.assertFalse(has)


# ────────────────────────────────────────────────────────────────────
# Unrealized
# ────────────────────────────────────────────────────────────────────

class TestPositionUnrealized(unittest.TestCase):

    def test_long_gain(self):
        p = {"ticker": "X", "direction": "LONG", "entry_price": 100,
             "entry_qty": 50, "entry_size_usd": 5000}
        u = R.position_unrealized(p, 110.0)
        self.assertEqual(u["unrealized_pct"], 10.0)
        self.assertEqual(u["unrealized_usd"], 500.0)
        self.assertTrue(u["available"])

    def test_short_gain(self):
        p = {"ticker": "X", "direction": "SHORT", "entry_price": 100,
             "entry_qty": 50, "entry_size_usd": 5000}
        u = R.position_unrealized(p, 90.0)
        self.assertEqual(u["unrealized_pct"], 10.0)
        self.assertEqual(u["unrealized_usd"], 500.0)

    def test_no_price(self):
        p = {"ticker": "X", "entry_price": 100}
        u = R.position_unrealized(p, None)
        self.assertFalse(u["available"])
        self.assertIsNone(u["unrealized_pct"])


# ────────────────────────────────────────────────────────────────────
# Daily aggregate
# ────────────────────────────────────────────────────────────────────

class TestComputeDailyStats(unittest.TestCase):

    def setUp(self):
        # Account value 100k. Three positions:
        #  AAPL long: entry $150 qty 50 ($7500), stop $147, current $155 → +$250 (3.33%)
        #  TSLA long: entry $200 qty 25 ($5000), no stop,    current $210 → +$250 (5%)
        #  NVDA short: entry $400 qty 10 ($4000), stop $410, current $390 → +$100 (2.5%)
        self.opens = [
            {"ticker": "AAPL", "direction": "LONG",  "entry_price": 150, "entry_qty": 50,
             "entry_size_usd": 7500, "calculated_stop": 147, "risk_per_share": 3.0},
            {"ticker": "TSLA", "direction": "LONG",  "entry_price": 200, "entry_qty": 25,
             "entry_size_usd": 5000, "calculated_stop": None, "risk_per_share": None},
            {"ticker": "NVDA", "direction": "SHORT", "entry_price": 400, "entry_qty": 10,
             "entry_size_usd": 4000, "calculated_stop": 410, "risk_per_share": 10.0},
        ]
        self.prices = {"AAPL": 155.0, "TSLA": 210.0, "NVDA": 390.0}

    def test_open_count(self):
        s = R.compute_daily_stats(self.opens, self.prices, 100_000, "A — BASELINE")
        self.assertEqual(s["open_trades"], 3)
        self.assertEqual(s["priced_count"], 3)

    def test_unrealized_usd_total(self):
        s = R.compute_daily_stats(self.opens, self.prices, 100_000, "A — BASELINE")
        # +250 + 250 + 100 = +600
        self.assertEqual(s["total_unrealized_usd"], 600.0)

    def test_unrealized_pct_of_account(self):
        s = R.compute_daily_stats(self.opens, self.prices, 100_000, "A — BASELINE")
        self.assertAlmostEqual(s["unrealized_pct_of_account"], 0.6, places=2)

    def test_avg_position_pct_is_size_weighted(self):
        # Size-weighted average of (3.33%, 5%, 2.5%) by sizes (7500, 5000, 4000)
        # = (3.33*7500 + 5*5000 + 2.5*4000) / 16500
        # = (24975 + 25000 + 10000) / 16500 = 59975/16500 ≈ 3.63%
        # Note: pct values are rounded to 2 dp in position_unrealized first
        s = R.compute_daily_stats(self.opens, self.prices, 100_000, "A")
        self.assertAlmostEqual(s["unrealized_pct_avg_position"], 3.63, places=1)

    def test_budgeted_risk_unstopped_uses_full_size(self):
        # AAPL: 3 * 50 = 150
        # TSLA: 5000 (no stop)
        # NVDA: 10 * 10 = 100
        s = R.compute_daily_stats(self.opens, self.prices, 100_000, "A")
        self.assertEqual(s["budgeted_risk_usd"], 150 + 5000 + 100)
        self.assertEqual(s["positions_missing_stop"], 1)

    def test_live_risk_uses_current_price_to_stop(self):
        # AAPL: (155-147)*50 = 400
        # TSLA: 5000 (no stop)
        # NVDA short: (410-390)*10 = 200
        s = R.compute_daily_stats(self.opens, self.prices, 100_000, "A")
        self.assertEqual(s["live_risk_usd"], 400 + 5000 + 200)

    def test_zero_account_value_doesnt_crash(self):
        s = R.compute_daily_stats(self.opens, self.prices, 0, "A")
        self.assertEqual(s["unrealized_pct_of_account"], 0.0)

    def test_missing_price_excludes_from_unrealized(self):
        prices = {"AAPL": 155.0, "TSLA": None, "NVDA": 390.0}
        s = R.compute_daily_stats(self.opens, prices, 100_000, "A")
        self.assertEqual(s["priced_count"], 2)
        # TSLA's unrealized excluded from total: 250 + 100 = 350
        self.assertEqual(s["total_unrealized_usd"], 350.0)

    def test_empty_positions(self):
        s = R.compute_daily_stats([], {}, 100_000, "A")
        self.assertEqual(s["open_trades"], 0)
        self.assertEqual(s["total_unrealized_usd"], 0)
        self.assertEqual(s["budgeted_risk_usd"], 0)


# ────────────────────────────────────────────────────────────────────
# Weekly aggregate
# ────────────────────────────────────────────────────────────────────

class TestComputeWeeklyStats(unittest.TestCase):
    """
    Week under test: Mon 2026-05-18 → Sun 2026-05-24 (the week reported
    when fired on Sat 2026-05-23). All assertions use 'now=Sat 2026-05-23'.
    """

    def setUp(self):
        self.now = at(2026, 5, 23)        # Saturday
        # Closed trades — mix of in-week and out-of-week
        self.closed = [
            # IN WEEK: closed 2026-05-20
            {"ticker": "AAPL", "direction": "LONG", "entry_date": "2026-05-18",
             "exit_date": "2026-05-20", "entry_price": 150, "exit_price": 156,
             "entry_size_usd": 7500, "pnl_pct": 4.0, "exit_reason": "catalyst"},
            # IN WEEK: closed 2026-05-22 (Friday — last US trading day)
            {"ticker": "TSLA", "direction": "LONG", "entry_date": "2026-05-19",
             "exit_date": "2026-05-22", "entry_price": 200, "exit_price": 190,
             "entry_size_usd": 5000, "pnl_pct": -5.0, "exit_reason": "stop"},
            # OUT OF WEEK: closed BEFORE the week
            {"ticker": "MSFT", "direction": "LONG", "entry_date": "2026-04-30",
             "exit_date": "2026-05-08", "entry_price": 300, "exit_price": 320,
             "entry_size_usd": 6000, "pnl_pct": 6.67, "exit_reason": "deadline"},
            # OUT OF WEEK: closed AFTER (future, shouldn't happen but defensively)
            {"ticker": "META", "direction": "LONG", "entry_date": "2026-05-25",
             "exit_date": "2026-05-27", "entry_price": 400, "exit_price": 412,
             "entry_size_usd": 8000, "pnl_pct": 3.0, "exit_reason": "catalyst"},
        ]
        self.opens = [
            {"ticker": "NVDA", "direction": "LONG", "entry_date": "2026-05-20",
             "entry_price": 400, "entry_qty": 10, "entry_size_usd": 4000,
             "calculated_stop": 390, "risk_per_share": 10.0},
        ]
        self.prices = {"NVDA": 420.0}

    def test_only_in_week_closed_counted(self):
        s = R.compute_weekly_stats(self.opens, self.closed, self.prices,
                                   100_000, "A", now=self.now)
        self.assertEqual(s["closed_count"], 2)
        tickers = [r["ticker"] for r in s["closed_this_week"]]
        self.assertIn("AAPL", tickers)
        self.assertIn("TSLA", tickers)
        self.assertNotIn("MSFT", tickers)
        self.assertNotIn("META", tickers)

    def test_realized_pnl_is_usd_weighted_not_pct_sum(self):
        # AAPL: 4% * 7500 = +300
        # TSLA: -5% * 5000 = -250
        # Net = +50
        # Legacy bug summed pcts: 4 + (-5) = -1, misleading
        s = R.compute_weekly_stats(self.opens, self.closed, self.prices,
                                   100_000, "A", now=self.now)
        self.assertEqual(s["realized_pnl_usd"], 50.0)
        self.assertAlmostEqual(s["realized_pnl_pct_of_account"], 0.05, places=3)

    def test_win_rate(self):
        s = R.compute_weekly_stats(self.opens, self.closed, self.prices,
                                   100_000, "A", now=self.now)
        self.assertEqual(s["win_count"], 1)
        self.assertEqual(s["loss_count"], 1)
        self.assertEqual(s["win_rate_pct"], 50.0)

    def test_best_worst_trade(self):
        s = R.compute_weekly_stats(self.opens, self.closed, self.prices,
                                   100_000, "A", now=self.now)
        self.assertEqual(s["best_trade"]["ticker"], "AAPL")
        self.assertEqual(s["worst_trade"]["ticker"], "TSLA")

    def test_end_of_week_open_with_unrealized(self):
        s = R.compute_weekly_stats(self.opens, self.closed, self.prices,
                                   100_000, "A", now=self.now)
        self.assertEqual(s["open_count_eow"], 1)
        eow = s["end_of_week_open"][0]
        self.assertEqual(eow["ticker"], "NVDA")
        self.assertEqual(eow["unrealized_pct"], 5.0)   # (420-400)/400 = +5%

    def test_opened_this_week_includes_closed_same_week(self):
        s = R.compute_weekly_stats(self.opens, self.closed, self.prices,
                                   100_000, "A", now=self.now)
        # AAPL entered 2026-05-12 (in week), TSLA entered 2026-05-13 (in week)
        # — both should count even though they're already closed
        opened_tickers = sorted(p["ticker"] for p in s["opened_this_week"])
        self.assertIn("AAPL", opened_tickers)
        self.assertIn("TSLA", opened_tickers)

    def test_no_closed_trades_in_week(self):
        s = R.compute_weekly_stats([], [], {}, 100_000, "A", now=self.now)
        self.assertEqual(s["closed_count"], 0)
        self.assertEqual(s["realized_pnl_usd"], 0.0)
        self.assertEqual(s["win_rate_pct"], 0.0)
        self.assertIsNone(s["best_trade"])

    def test_empty_inputs_dont_crash(self):
        s = R.compute_weekly_stats([], [], {}, 100_000, "A", now=self.now)
        self.assertEqual(s["open_count_eow"], 0)


# ────────────────────────────────────────────────────────────────────
# Regression: behaviors the legacy code got wrong
# ────────────────────────────────────────────────────────────────────

class TestLegacyBugsAreFixed(unittest.TestCase):

    def test_weekly_does_not_include_lifetime_trades(self):
        """
        Legacy send_weekly_report.py + send_ab_weekly_summary used LIFETIME stats
        (calc_stats over all closed_positions, no date filter). New code MUST
        only count this week's closed trades.
        """
        closed = [
            {"ticker": "OLD", "entry_date": "2025-01-01", "exit_date": "2025-01-05",
             "entry_size_usd": 10000, "pnl_pct": 20.0},     # ancient win
            {"ticker": "NEW", "entry_date": "2026-05-19", "exit_date": "2026-05-21",
             "entry_size_usd": 5000,  "pnl_pct": -1.0},      # this week, small loss
        ]
        s = R.compute_weekly_stats([], closed, {}, 100_000, "A", now=at(2026, 5, 23))
        self.assertEqual(s["closed_count"], 1)
        self.assertEqual(s["win_rate_pct"], 0.0)
        self.assertEqual(s["realized_pnl_usd"], -50.0)

    def test_daily_unrealized_pct_is_account_relative_not_per_position_sum(self):
        """
        Legacy dashboard summed per-position pnl_pct to get total_pnl_pct,
        which is mathematically wrong. Our daily report exposes both:
          unrealized_pct_of_account (correct portfolio %)
          unrealized_pct_avg_position (size-weighted avg per-position %)
        and they are different when sizes differ.
        """
        opens = [
            {"ticker": "A", "direction": "LONG", "entry_price": 100, "entry_qty": 10,
             "entry_size_usd": 1000, "calculated_stop": None, "risk_per_share": None},
            {"ticker": "B", "direction": "LONG", "entry_price": 100, "entry_qty": 100,
             "entry_size_usd": 10000, "calculated_stop": None, "risk_per_share": None},
        ]
        prices = {"A": 110.0, "B": 101.0}
        s = R.compute_daily_stats(opens, prices, 100_000, "X")
        # Unrealized: A = +$100 (10%), B = +$100 (1%) → total +$200
        self.assertEqual(s["total_unrealized_usd"], 200.0)
        # Portfolio %: 200 / 100_000 = 0.2%
        self.assertAlmostEqual(s["unrealized_pct_of_account"], 0.2, places=3)
        # Avg position % (size-weighted): (10*1000 + 1*10000) / 11000 ≈ 1.82%
        self.assertAlmostEqual(s["unrealized_pct_avg_position"], 1.82, places=1)
        # The two are different — that's the whole point
        self.assertNotEqual(round(s["unrealized_pct_of_account"], 2),
                            round(s["unrealized_pct_avg_position"], 2))

    def test_filter_with_timestamp_dates_works(self):
        """
        Some legacy code stored entry_date as ISO datetime ('2026-05-15T10:30:00').
        Filter must still match on the date portion.
        """
        rows = [{"exit_date": "2026-05-20T10:30:00"}]
        out = R.filter_by_date_range(rows, "exit_date", date(2026, 5, 18), date(2026, 5, 24))
        self.assertEqual(len(out), 1)


if __name__ == "__main__":
    unittest.main()
