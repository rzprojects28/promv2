"""
Read-only helpers for loading the trading strategy's live JSON state.

Data layout under prometheus/data/:
  data/account_a/open_positions.json     — current open positions (operational)
  data/account_a/closed_positions.json   — appended on every exit
  data/account_a/trade_journal.json      — appended on every journal review
  data/account_a/approved_trades.json    — last risk-gate output
  data/account_a/rejected_trades.json    — last risk-gate output
  data/account_a/performance_stats.json  — last computed aggregates
  data/archive/account_b/                — frozen B data (read-only, off main flow)
  data/snapshots/YYYY-MM-DD.json         — daily snapshots (written by report.daily)
  data/prometheus.db                     — SQLite history (closed trades, daily metrics, events)
"""
import json
import os
from typing import Tuple


BASE_DIR     = os.path.expanduser('~/prometheus')
DATA_DIR     = os.path.join(BASE_DIR, 'data')
ACCT_A_DATA  = os.path.join(DATA_DIR, 'account_a')

# Single account list — kept as a list so adding accounts later is trivial.
# Tuple: (data_dir, ib_port, account_label)
ACCOUNTS = [
    (ACCT_A_DATA, 4002, 'A — BASELINE'),
]


def load_json(path: str, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def load_account_positions(data_dir: str) -> Tuple[list, list]:
    """Returns (open_positions, closed_positions) for one account."""
    open_p   = load_json(os.path.join(data_dir, 'open_positions.json'),   [])
    closed_p = load_json(os.path.join(data_dir, 'closed_positions.json'), [])
    return open_p, closed_p


def load_approved_rejected(data_dir: str) -> Tuple[list, list]:
    """Returns (approved_today, rejected_today) — the risk gate's latest output."""
    approved = load_json(os.path.join(data_dir, 'approved_trades.json'), {}).get('trades', [])
    rejected = load_json(os.path.join(data_dir, 'rejected_trades.json'), {}).get('trades', [])
    return approved, rejected
