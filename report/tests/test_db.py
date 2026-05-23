"""
Unit tests for report.db (SQLite history layer).

Run from repo root:
    python3 -m unittest report.tests.test_db -v
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from report import db as D


class TestSchema(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp.close()
        self.conn = D.connect(self.tmp.name)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    def test_tables_created(self):
        tables = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        self.assertIn('closed_trades',   tables)
        self.assertIn('daily_snapshots', tables)
        self.assertIn('trade_events',    tables)
        self.assertIn('journal_entries', tables)

    def test_connect_is_idempotent(self):
        # Calling connect() again on the same path must not error or wipe data.
        D.connect(self.tmp.name).close()
        D.connect(self.tmp.name).close()


class TestClosedTradeUpsert(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp.close()
        self.conn = D.connect(self.tmp.name)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    def test_insert_then_update(self):
        pos = {
            'ticker': 'AAPL', 'direction': 'LONG', 'conviction': 'HIGH',
            'sector': 'XLK', 'entry_date': '2026-05-15', 'exit_date': '2026-05-20',
            'entry_price': 150, 'exit_price': 156, 'entry_qty': 50,
            'entry_size_usd': 7500, 'pnl_pct': 4.0,
            'exit_reason': 'catalyst', 'exit_category': 'catalyst_exit',
        }
        D.upsert_closed_trade(self.conn, pos, account='A')
        rows = self.conn.execute("SELECT * FROM closed_trades").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['pnl_usd'], 300.0)         # 4% × 7500

        # Re-upsert with updated values — must not duplicate
        pos['pnl_pct']    = 6.0
        pos['exit_price'] = 159
        D.upsert_closed_trade(self.conn, pos, account='A')
        rows = self.conn.execute("SELECT * FROM closed_trades").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['pnl_pct'], 6.0)
        self.assertEqual(rows[0]['pnl_usd'], 450.0)         # 6% × 7500


class TestDailySnapshot(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp.close()
        self.conn = D.connect(self.tmp.name)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    def test_upsert_on_same_day(self):
        stats = {
            'currency': 'USD', 'account_value': 100_000, 'open_trades': 3,
            'total_unrealized_usd': 250.0, 'unrealized_pct_of_account': 0.25,
            'unrealized_pct_avg_position': 1.5, 'budgeted_risk_usd': 500,
            'live_risk_usd': 450, 'positions_missing_stop': 0,
        }
        D.write_daily_snapshot(self.conn, stats, 'A', snapshot_date='2026-05-23')
        D.write_daily_snapshot(self.conn, {**stats, 'open_trades': 4},
                               'A', snapshot_date='2026-05-23')
        rows = self.conn.execute("SELECT * FROM daily_snapshots").fetchall()
        self.assertEqual(len(rows), 1)       # upserted, not duplicated
        self.assertEqual(rows[0]['open_trades'], 4)

    def test_different_days_kept_separately(self):
        stats = {'currency': 'USD', 'account_value': 100_000, 'open_trades': 1}
        D.write_daily_snapshot(self.conn, stats, 'A', snapshot_date='2026-05-22')
        D.write_daily_snapshot(self.conn, stats, 'A', snapshot_date='2026-05-23')
        rows = self.conn.execute("SELECT * FROM daily_snapshots").fetchall()
        self.assertEqual(len(rows), 2)


class TestEventLog(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp.close()
        self.conn = D.connect(self.tmp.name)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    def test_log_and_query(self):
        D.log_event(self.conn, 'opened',   'A', ticker='AAPL', direction='LONG')
        D.log_event(self.conn, 'rejected', 'A', ticker='TSLA', direction='LONG',
                    detail='size cap')
        rows = D.recent_events(self.conn, account='A')
        self.assertEqual(len(rows), 2)


class TestReadHelpers(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp.close()
        self.conn = D.connect(self.tmp.name)
        for d, ticker in [('2026-05-15', 'OLD'),
                          ('2026-05-20', 'IN1'),
                          ('2026-05-23', 'IN2'),
                          ('2026-06-01', 'FUT')]:
            D.upsert_closed_trade(self.conn, {
                'ticker': ticker, 'entry_date': '2026-05-01', 'exit_date': d,
                'entry_size_usd': 1000, 'pnl_pct': 1.0,
            }, account='A')

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    def test_closed_trades_in_range_inclusive(self):
        rows = D.closed_trades_in_range(self.conn, '2026-05-18', '2026-05-24')
        tickers = sorted(r['ticker'] for r in rows)
        self.assertEqual(tickers, ['IN1', 'IN2'])


if __name__ == '__main__':
    unittest.main()
