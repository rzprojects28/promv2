"""
Prometheus Trading — Master Runner (Account A only)

Drop-in replacement for the old run_parallel.py. Runs the shared research
pipeline once, then runs Account A's pipeline (risk → execute → monitor →
journal). After the strategy finishes, the report cron picks up daily and
weekly Telegram messages from data/ — this script does NOT call report/.

Usage:
    python3 -m trading.run

Cron (production VPS):
    0 21 * * 1-5  cd ~/prometheus && /usr/bin/python3 -m trading.run >> data/logs/trading.log 2>&1
"""
import os
import sys
from datetime import datetime

BASE_DIR     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRADING_DIR  = os.path.join(BASE_DIR, 'trading')
RESEARCH_DIR = os.path.join(TRADING_DIR, 'research')
ACCT_A_DIR   = os.path.join(TRADING_DIR, 'account_a')

# Make every trading subfolder importable so legacy flat imports still resolve.
for p in (TRADING_DIR, RESEARCH_DIR, ACCT_A_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.expanduser('~/prometheus/.env'))


def run_all():
    start = datetime.now()
    print("=" * 60)
    print(f"  PROMETHEUS TRADING RUN — Account A")
    print(f"  {start.strftime('%Y-%m-%d %H:%M')}")
    print(f"  Gateway port: 4002")
    print("=" * 60)

    # ── 1: Research (shared, runs once) ──────────────────────────
    print("\n[1/2] Running research pipeline...")
    try:
        orig = os.getcwd()
        os.chdir(RESEARCH_DIR)        # research scripts assume cwd
        import run as research_run
        research_run.run_all()
        os.chdir(orig)
    except Exception as e:
        print(f"  Research failed: {e}")
        try: os.chdir(BASE_DIR)
        except: pass

    # ── 2: Account A pipeline ────────────────────────────────────
    print("\n[2/2] Running Account A pipeline...")
    result_a = {}
    try:
        sys.path.insert(0, ACCT_A_DIR)
        import run as account_a_run
        result_a = account_a_run.run() or {}
    except Exception as e:
        print(f"  Account A failed: {e}")
        import traceback; traceback.print_exc()

    open_a = result_a.get('monitor', {}).get('still_open', [])

    elapsed = (datetime.now() - start).seconds
    print(f"\n{'=' * 60}")
    print(f"  TRADING RUN COMPLETE — {elapsed}s")
    print(f"  Approved: {result_a.get('approved', 0)} | Open: {len(open_a)}")
    print(f"  Daily/weekly Telegram reports run on a separate cron.")
    print(f"{'=' * 60}\n")


if __name__ == '__main__':
    run_all()
