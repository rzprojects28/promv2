"""
Prometheus Trading — Master Runner (Account A only)

Drop-in replacement for the old run_parallel.py. Runs the shared research
pipeline once, then runs Account A's pipeline (risk → execute → monitor →
journal). After the strategy finishes, the report cron picks up daily and
weekly Telegram messages from data/ — this script does NOT call report/.

Usage:
    python3 -m trading.run

Cron (production VPS):
    0 21 * * 1-5  cd ~/promv2 && /usr/bin/python3 -m trading.run >> data/logs/trading.log 2>&1
"""
import importlib.util
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
load_dotenv(dotenv_path=os.path.join(BASE_DIR, '.env'))


def _load_module_from_path(name: str, path: str):
    """Load a Python module by explicit file path — avoids sys.path collisions
    between trading/run.py, trading/research/run.py, and trading/account_a/run.py."""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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
        research_run = _load_module_from_path('research_run', os.path.join(RESEARCH_DIR, 'run.py'))
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
        account_a_run = _load_module_from_path('account_a_run', os.path.join(ACCT_A_DIR, 'run.py'))
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
