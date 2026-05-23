"""
Snapshot-style tests for the expanded daily message. Confirms the new
"Today's decisions" and "Today's activity" sections render when the daily
stats dict carries the corresponding keys.

Run from repo root:
    python3 -m unittest report.tests.test_messages -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from report import stats, messages


class TestDailyMessageDecisionsAndActivity(unittest.TestCase):

    def setUp(self):
        opens = [
            {'ticker': 'AAPL', 'direction': 'LONG', 'entry_price': 150, 'entry_qty': 50,
             'entry_size_usd': 7500, 'calculated_stop': 147, 'risk_per_share': 3.0},
        ]
        self.s = stats.compute_daily_stats(
            open_positions=opens, current_prices={'AAPL': 155.0},
            account_value=100_000, account_label='A — BASELINE', currency='USD',
        )

    def test_no_decisions_or_activity_section_when_empty(self):
        msg = messages.build_daily_message(self.s)
        self.assertNotIn("Today's decisions", msg)
        self.assertNotIn("Today's activity",  msg)

    def test_decisions_section_renders_approved(self):
        self.s['approved_today'] = [{
            'ticker': 'NVDA', 'direction': 'LONG', 'conviction': 'HIGH',
            'position_size_pct': 3.5,
        }]
        msg = messages.build_daily_message(self.s)
        self.assertIn("Today's decisions", msg)
        self.assertIn("NVDA",  msg)
        self.assertIn("3.5%", msg)

    def test_decisions_section_renders_rejected_with_reason(self):
        self.s['rejected_today'] = [{
            'ticker': 'AMD', 'direction': 'LONG',
            'risk_checks': ['✓ size OK', '✗ Sector XLK would be 22% — exceeds max 20%'],
        }]
        msg = messages.build_daily_message(self.s)
        self.assertIn("AMD", msg)
        self.assertIn("Sector XLK would be 22%", msg)

    def test_activity_section_renders_opened_and_closed(self):
        self.s['opened_today'] = [{
            'ticker': 'TSLA', 'direction': 'LONG', 'entry_price': 200,
            'entry_size_usd': 5000,
        }]
        self.s['closed_today'] = [{
            'ticker': 'META', 'direction': 'LONG', 'pnl_pct': -3.2,
            'exit_reason': 'stop price breached',
        }]
        msg = messages.build_daily_message(self.s)
        self.assertIn("Today's activity", msg)
        self.assertIn("Opened TSLA", msg)
        self.assertIn("Closed META", msg)
        self.assertIn("-3.2%", msg)
        self.assertIn("stop price breached", msg)


class TestWeeklyMessageStillWorks(unittest.TestCase):

    def test_weekly_smoke(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        sat = datetime(2026, 5, 23, 8, 0, tzinfo=ZoneInfo("Asia/Singapore"))
        s = stats.compute_weekly_stats(
            open_positions=[], closed_positions=[],
            current_prices={}, account_value=100_000,
            account_label='A — BASELINE', currency='USD', now=sat,
        )
        msg = messages.build_weekly_message(s)
        self.assertIn("WEEKLY", msg)
        self.assertIn("2026-05-18", msg)
        self.assertIn("2026-05-24", msg)


if __name__ == '__main__':
    unittest.main()
