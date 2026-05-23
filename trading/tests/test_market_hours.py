"""
Tests for the US market-hours checker.

Stable assertions across DST boundary: every test passes a frozen UTC
datetime, so daylight saving is exercised explicitly rather than relying
on the test runner's clock.

Run:
    python3 -m unittest trading.tests.test_market_hours -v
"""
import os
import sys
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'trading', 'execution'))

from market_hours import is_us_market_open, minutes_until_open

UTC = ZoneInfo("UTC")


def at_utc(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=UTC)


class TestMarketOpenCases(unittest.TestCase):

    def test_winter_thursday_market_hours(self):
        # Thu 2026-01-22, 15:00 UTC = 10:00 ET (EST) — open
        ok, _ = is_us_market_open(at_utc(2026, 1, 22, 15, 0))
        self.assertTrue(ok)

    def test_summer_wednesday_market_hours(self):
        # Wed 2026-07-15, 17:00 UTC = 13:00 EDT — open
        ok, _ = is_us_market_open(at_utc(2026, 7, 15, 17, 0))
        self.assertTrue(ok)

    def test_just_after_open(self):
        # Tue 2026-04-21 13:35 UTC = 09:35 EDT — open
        ok, _ = is_us_market_open(at_utc(2026, 4, 21, 13, 35))
        self.assertTrue(ok)

    def test_one_minute_before_open(self):
        # Tue 2026-04-21 13:29 UTC = 09:29 EDT — closed
        ok, reason = is_us_market_open(at_utc(2026, 4, 21, 13, 29))
        self.assertFalse(ok)
        self.assertIn("outside RTH", reason)

    def test_one_minute_after_close(self):
        # Tue 2026-04-21 20:01 UTC = 16:01 EDT — closed
        ok, reason = is_us_market_open(at_utc(2026, 4, 21, 20, 1))
        self.assertFalse(ok)


class TestWeekend(unittest.TestCase):

    def test_saturday_closed(self):
        ok, reason = is_us_market_open(at_utc(2026, 5, 23, 15, 0))
        self.assertFalse(ok)
        self.assertIn("weekend", reason)

    def test_sunday_closed(self):
        ok, reason = is_us_market_open(at_utc(2026, 5, 24, 15, 0))
        self.assertFalse(ok)


class TestHolidays(unittest.TestCase):

    def test_christmas_closed(self):
        ok, reason = is_us_market_open(at_utc(2026, 12, 25, 15, 0))
        self.assertFalse(ok)
        self.assertIn("holiday", reason)

    def test_thanksgiving_closed(self):
        ok, reason = is_us_market_open(at_utc(2026, 11, 26, 15, 0))
        self.assertFalse(ok)

    def test_new_years_closed(self):
        ok, reason = is_us_market_open(at_utc(2026, 1, 1, 15, 0))
        self.assertFalse(ok)


class TestEarlyClose(unittest.TestCase):

    def test_day_before_july4_open_at_noon(self):
        # Thu 2026-07-02, 16:00 UTC = 12:00 EDT (during early-close window) — still open
        ok, _ = is_us_market_open(at_utc(2026, 7, 2, 16, 0))
        self.assertTrue(ok)

    def test_day_before_july4_closed_after_1pm(self):
        # Thu 2026-07-02, 17:30 UTC = 13:30 EDT (after early close) — closed
        ok, reason = is_us_market_open(at_utc(2026, 7, 2, 17, 30))
        self.assertFalse(ok)
        self.assertIn("early close", reason)


class TestDSTTransition(unittest.TestCase):
    """The SGT-scheduled cron job hits ET 1 hour earlier when US is on EDT."""

    def test_21_40_sgt_in_winter_is_before_market_open(self):
        # 21:40 SGT = 13:40 UTC. In winter (EST), that's 08:40 ET — before 09:30 open.
        ok, reason = is_us_market_open(at_utc(2026, 1, 22, 13, 40))
        self.assertFalse(ok)
        self.assertIn("08:40", reason)

    def test_21_40_sgt_in_summer_is_after_market_open(self):
        # 21:40 SGT = 13:40 UTC. In summer (EDT), that's 09:40 ET — 10 min after open.
        ok, _ = is_us_market_open(at_utc(2026, 7, 15, 13, 40))
        self.assertTrue(ok)

    def test_22_40_sgt_in_winter_is_after_market_open(self):
        # User can fix the cron to 22:40 SGT to cover both seasons:
        # 22:40 SGT = 14:40 UTC. Winter EST: 09:40 ET ✓. Summer EDT: 10:40 ET ✓.
        ok, _ = is_us_market_open(at_utc(2026, 1, 22, 14, 40))
        self.assertTrue(ok)
        ok, _ = is_us_market_open(at_utc(2026, 7, 15, 14, 40))
        self.assertTrue(ok)


class TestMinutesUntilOpen(unittest.TestCase):

    def test_50_minutes_before_open_in_winter(self):
        # 21:40 SGT winter = 08:40 ET, market opens 09:30 ET → 50 minutes
        self.assertEqual(minutes_until_open(at_utc(2026, 1, 22, 13, 40)), 50)

    def test_returns_none_when_already_open(self):
        self.assertIsNone(minutes_until_open(at_utc(2026, 4, 21, 15, 0)))

    def test_returns_none_on_weekend(self):
        self.assertIsNone(minutes_until_open(at_utc(2026, 5, 23, 13, 0)))

    def test_returns_none_on_holiday(self):
        self.assertIsNone(minutes_until_open(at_utc(2026, 12, 25, 13, 0)))


if __name__ == '__main__':
    unittest.main()
