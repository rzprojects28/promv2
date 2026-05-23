"""
Prometheus report package — daily and weekly Telegram reports plus dashboard.

Isolated from the trading strategy: this package does not import from
trading/, and trading/ does not import from here. The only contract is
the on-disk state in data/account_a/*.json and the SQLite history at
data/prometheus.db.

Entry points:
  python3 -m report.daily    — per-account daily snapshot + writes SQLite snapshot
  python3 -m report.weekly   — per-account weekly summary (Mon→Sun SGT)
"""
