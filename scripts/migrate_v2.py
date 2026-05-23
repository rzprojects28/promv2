#!/usr/bin/env python3
"""
Prometheus migration — V2 layout.

Run once on the VPS after `git pull` brings in the restructured tree.

What this script does:
  1. Creates the new data/ directory tree
  2. Moves   account_a/data/*  →  data/account_a/   (so the trading code keeps writing
     to its live JSON files, just at the new location)
  3. Archives account_b/data/  →  data/archive/account_b/   (frozen, read-only)
  4. Initializes data/prometheus.db with the SQLite schema
  5. Backfills closed_trades from data/account_a/closed_positions.json
  6. Backfills journal_entries from data/account_a/trade_journal.json
  7. Removes empty legacy folders (account_a/, account_b/, phase2/, phase3/, dashboard/, reporting/, reports/)
  8. Prints the cron checklist

Idempotent — safe to run multiple times. Always takes a timestamped backup
of the data folder before touching it.

Usage:
    python3 scripts/migrate_v2.py            # interactive — asks before destructive ops
    python3 scripts/migrate_v2.py --yes      # non-interactive (use only if you've reviewed)
    python3 scripts/migrate_v2.py --dry-run  # print actions, change nothing
"""
import argparse
import json
import os
import shutil
import sys
from datetime import datetime


BASE_DIR = os.path.expanduser('~/prometheus')


# ── Paths ─────────────────────────────────────────────────────────────────
OLD_ACCT_A_DATA = os.path.join(BASE_DIR, 'account_a', 'data')
OLD_ACCT_B_DATA = os.path.join(BASE_DIR, 'account_b', 'data')

NEW_DATA_DIR    = os.path.join(BASE_DIR, 'data')
NEW_ACCT_A_DATA = os.path.join(NEW_DATA_DIR, 'account_a')
ARCHIVE_DIR     = os.path.join(NEW_DATA_DIR, 'archive')
ARCHIVE_B_DIR   = os.path.join(ARCHIVE_DIR, 'account_b')
SNAPSHOTS_DIR   = os.path.join(NEW_DATA_DIR, 'snapshots')
LOGS_DIR        = os.path.join(NEW_DATA_DIR, 'logs')
DB_PATH         = os.path.join(NEW_DATA_DIR, 'prometheus.db')

LEGACY_EMPTY_DIRS = [
    os.path.join(BASE_DIR, 'account_a'),
    os.path.join(BASE_DIR, 'account_b'),
    os.path.join(BASE_DIR, 'phase2'),
    os.path.join(BASE_DIR, 'phase3'),
    os.path.join(BASE_DIR, 'dashboard'),
    os.path.join(BASE_DIR, 'reporting'),
    # NOTE: not removing reports/ — Plotus generator still writes there.
]


def _say(msg):
    print(f"  {msg}")


def _confirm(prompt: str, yes: bool, dry_run: bool = False) -> bool:
    """Skip prompts in dry-run (no harm can happen) and when --yes is passed."""
    if yes or dry_run:
        return True
    return input(f"  {prompt} [y/N] ").strip().lower() == 'y'


def _purge_pycache(root: str, dry_run: bool) -> int:
    """Recursively delete __pycache__ dirs under `root`. Returns count removed."""
    count = 0
    for dirpath, dirnames, _ in os.walk(root, topdown=False):
        if os.path.basename(dirpath) == '__pycache__':
            if dry_run:
                _say(f"DRY: rm -rf {dirpath}")
            else:
                shutil.rmtree(dirpath, ignore_errors=True)
            count += 1
    return count


def _rmdir_if_empty(d: str, dry_run: bool) -> bool:
    """rmdir d if it exists and is empty. Returns True if removed."""
    if not os.path.isdir(d):
        return False
    if os.listdir(d):
        return False
    if dry_run:
        _say(f"DRY: rmdir {d}")
    else:
        os.rmdir(d)
    return True


def step_make_dirs(dry_run: bool):
    print("\n[1/8] Create new data/ directory tree...")
    for d in (NEW_DATA_DIR, NEW_ACCT_A_DATA, ARCHIVE_DIR, ARCHIVE_B_DIR,
              SNAPSHOTS_DIR, LOGS_DIR):
        if dry_run:
            _say(f"DRY: mkdir -p {d}")
        else:
            os.makedirs(d, exist_ok=True)
            _say(f"OK   {d}")


def step_backup_existing(dry_run: bool):
    print("\n[2/8] Backup existing data folder (if present)...")
    if not os.path.exists(NEW_DATA_DIR) or not os.listdir(NEW_DATA_DIR):
        _say("skip — data/ doesn't exist or is empty")
        return
    backup = os.path.join(BASE_DIR, f"data.backup.{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    if dry_run:
        _say(f"DRY: cp -a {NEW_DATA_DIR} {backup}")
    else:
        shutil.copytree(NEW_DATA_DIR, backup)
        _say(f"backup → {backup}")


def step_move_account_a(dry_run: bool, yes: bool):
    print("\n[3/8] Move account_a/data/* → data/account_a/...")
    if not os.path.isdir(OLD_ACCT_A_DATA):
        _say(f"skip — {OLD_ACCT_A_DATA} not found (already migrated?)")
        return
    files = os.listdir(OLD_ACCT_A_DATA)
    if not files:
        _say("skip — nothing to move")
        return

    _say(f"will move {len(files)} file(s) from {OLD_ACCT_A_DATA}")
    if not _confirm("proceed?", yes, dry_run):
        _say("aborted at user request")
        return

    for fname in files:
        src = os.path.join(OLD_ACCT_A_DATA, fname)
        dst = os.path.join(NEW_ACCT_A_DATA, fname)
        if dry_run:
            _say(f"DRY: mv {src} {dst}")
            continue
        if os.path.exists(dst):
            _say(f"skip — {dst} already exists (won't overwrite)")
            continue
        shutil.move(src, dst)
        _say(f"moved {fname}")

    # Source dir is now empty — drop it so the legacy folder can be cleaned in step 8.
    _rmdir_if_empty(OLD_ACCT_A_DATA, dry_run)


def step_archive_account_b(dry_run: bool, yes: bool):
    print("\n[4/8] Archive account_b/data/ → data/archive/account_b/...")
    if not os.path.isdir(OLD_ACCT_B_DATA):
        _say(f"skip — {OLD_ACCT_B_DATA} not found")
        return
    files = os.listdir(OLD_ACCT_B_DATA)
    if not files:
        _say("skip — account_b/data is empty")
        return

    _say(f"will archive {len(files)} file(s) from {OLD_ACCT_B_DATA}")
    if not _confirm("proceed?", yes, dry_run):
        _say("aborted at user request")
        return

    for fname in files:
        src = os.path.join(OLD_ACCT_B_DATA, fname)
        dst = os.path.join(ARCHIVE_B_DIR, fname)
        if dry_run:
            _say(f"DRY: mv {src} {dst}")
            continue
        if os.path.exists(dst):
            _say(f"skip — {dst} already exists")
            continue
        shutil.move(src, dst)
        _say(f"archived {fname}")

    _rmdir_if_empty(OLD_ACCT_B_DATA, dry_run)


def step_init_db(dry_run: bool):
    print("\n[5/8] Initialize SQLite database (data/prometheus.db)...")
    if dry_run:
        _say(f"DRY: would init {DB_PATH}")
        return
    sys.path.insert(0, BASE_DIR)
    from report import db as repo_db
    conn = repo_db.connect(DB_PATH)
    conn.close()
    _say(f"OK   schema applied at {DB_PATH}")


def _resolve_backfill_source(filename: str, dry_run: bool) -> str:
    """
    During dry-run, step 3 hasn't actually moved files yet — the backfill
    source still lives at the OLD path. Pick whichever exists so dry-run
    output reflects what the live run will see.
    """
    new_path = os.path.join(NEW_ACCT_A_DATA, filename)
    old_path = os.path.join(OLD_ACCT_A_DATA, filename)
    if os.path.exists(new_path):
        return new_path
    if dry_run and os.path.exists(old_path):
        return old_path
    return new_path   # for the "not found" message


def step_backfill_closed(dry_run: bool):
    print("\n[6/8] Backfill closed_trades from closed_positions.json...")
    src = _resolve_backfill_source('closed_positions.json', dry_run)
    if not os.path.exists(src):
        _say(f"skip — {src} not found")
        return
    with open(src) as f:
        rows = json.load(f)
    if not rows:
        _say("skip — no closed trades to backfill")
        return
    if dry_run:
        _say(f"DRY: would upsert {len(rows)} closed trade(s) from {src}")
        return

    sys.path.insert(0, BASE_DIR)
    from report import db as repo_db
    conn = repo_db.connect(DB_PATH)
    for row in rows:
        repo_db.upsert_closed_trade(conn, row, account='A')
    conn.close()
    _say(f"upserted {len(rows)} closed trade(s)")


def step_backfill_journal(dry_run: bool):
    print("\n[7/8] Backfill journal_entries from trade_journal.json...")
    src = _resolve_backfill_source('trade_journal.json', dry_run)
    if not os.path.exists(src):
        _say(f"skip — {src} not found")
        return
    with open(src) as f:
        rows = json.load(f)
    if not rows:
        _say("skip — no journal entries to backfill")
        return
    if dry_run:
        _say(f"DRY: would upsert {len(rows)} journal entry/entries from {src}")
        return

    sys.path.insert(0, BASE_DIR)
    from report import db as repo_db
    conn = repo_db.connect(DB_PATH)
    for row in rows:
        repo_db.upsert_journal_entry(conn, row, account='A')
    conn.close()
    _say(f"upserted {len(rows)} journal entry/entries")


def step_remove_empty_legacy(dry_run: bool, yes: bool):
    print("\n[8/8] Remove empty legacy folders...")
    for d in LEGACY_EMPTY_DIRS:
        if not os.path.isdir(d):
            continue

        # First: purge any __pycache__ left behind by previous python runs.
        _purge_pycache(d, dry_run)

        # Then: drop any now-empty subdirs (e.g. account_a/data/ after step 3
        # already moved everything out of it).
        for sub in os.listdir(d):
            subpath = os.path.join(d, sub)
            if os.path.isdir(subpath):
                _rmdir_if_empty(subpath, dry_run)

        # On dry-run we couldn't actually delete the subdirs above, so the
        # parent will look non-empty. Report optimistically.
        contents = [c for c in os.listdir(d) if c != '__pycache__'] if os.path.isdir(d) else []
        if dry_run:
            if contents:
                _say(f"DRY: rm -rf {d}  (after empty subdirs cleared above)")
            else:
                _say(f"DRY: rm -rf {d}")
            continue

        if contents:
            _say(f"skip {d} — not empty: {contents[:5]}{'…' if len(contents) > 5 else ''}")
            continue

        if _confirm(f"remove empty {d}?", yes):
            shutil.rmtree(d, ignore_errors=True)
            _say(f"removed {d}")


def print_cron_checklist():
    print("\n" + "=" * 60)
    print("  CRON CHECKLIST — update on the VPS")
    print("=" * 60)
    print("""
Edit your crontab (crontab -e) and replace the old prometheus entries with:

    # Research + Account A trading pipeline (replaces run_parallel.py)
    0  21 * * 1-5  cd ~/prometheus && /usr/bin/python3 -m trading.run    >> data/logs/trading.log 2>&1

    # Daily Telegram + SQLite snapshot (~30min after the trading run)
    30 21 * * 1-5  cd ~/prometheus && /usr/bin/python3 -m report.daily   >> data/logs/daily.log   2>&1

    # Weekly Telegram (Sat 08:00 SGT, after Fri US close)
    0  8  * * 6    cd ~/prometheus && /usr/bin/python3 -m report.weekly  >> data/logs/weekly.log  2>&1

If you use a venv, replace /usr/bin/python3 with the venv path (e.g.
/root/prometheus/venv/bin/python3).

Old cron entries to REMOVE:
    - anything calling run_parallel.py
    - anything calling send_weekly_report.py
    - anything calling phase3/run_phase3.py
    - anything calling account_b/run_account_b.py

Sanity checks after cron is in place:
    python3 -m report.daily     # should print "Daily reports sent."
    python3 -m report.weekly    # should print "Weekly reports sent."
    python3 -m unittest discover -s report/tests -v   # 62 tests pass
""")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--yes',     action='store_true', help='skip interactive confirms')
    ap.add_argument('--dry-run', action='store_true', help='show actions, change nothing')
    args = ap.parse_args()

    print("=" * 60)
    print("  PROMETHEUS V2 MIGRATION")
    print(f"  Base: {BASE_DIR}")
    print(f"  Mode: {'DRY RUN' if args.dry_run else 'LIVE'}")
    print("=" * 60)

    step_make_dirs(args.dry_run)
    step_backup_existing(args.dry_run)
    step_move_account_a(args.dry_run, args.yes)
    step_archive_account_b(args.dry_run, args.yes)
    step_init_db(args.dry_run)
    step_backfill_closed(args.dry_run)
    step_backfill_journal(args.dry_run)
    step_remove_empty_legacy(args.dry_run, args.yes)

    print_cron_checklist()


if __name__ == '__main__':
    main()
