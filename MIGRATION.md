# Migration to V2 layout

This branch reorganises Prometheus into three clean folders and removes
Account B. After pulling, you'll need to run a one-shot migration script
on the VPS and update cron.

## What changed

### Removed
- `account_b/` (code) — Account B is gone. Existing B data on the VPS gets
  moved into `data/archive/account_b/` for posterity, then `account_b/` is
  removed.
- `learning_engine.py` — B-only.
- `run_parallel.py` — replaced by `trading/run.py` (single-account orchestrator).
- `send_weekly_report.py` — replaced by `python3 -m report.weekly`.
- `phase3/run_phase3.py` — old single-account orchestrator, superseded.

### Moved (file history preserved via `git mv`)

| Old path | New path |
|---|---|
| `phase2/*` | `trading/research/*` |
| `phase3/risk_manager.py` | `trading/risk/risk_manager.py` |
| `phase3/execution_agent.py` | `trading/execution/execution_agent.py` |
| `phase3/monitor_agent.py` | `trading/monitor/monitor_agent.py` |
| `phase3/journal_agent.py` | `trading/journal/journal_agent.py` |
| `phase3/telegram_alerts.py` | `trading/telegram_alerts.py` |
| `account_a/run_account_a.py` | `trading/account_a/run.py` |
| `account_a/prometheus_config.json` | `trading/account_a/config.json` |
| `daily_check.py` | `trading/daily_check.py` |
| `test_connection.py` | `trading/test_connection.py` |
| `analysis_agent_system_prompt_v2.txt` | `trading/research/prompt_v2.txt` |
| `reporting/*` | `report/*` (and `reporting/data.py` → `report/positions_loader.py`) |
| `dashboard/*` | `report/dashboard/*` |
| `daily_report_generator.py` (Plotus) | `report/plotus/generator.py` |
| `reports/plotus_daily_*.txt` | `report/plotus/sample_*.txt` |
| `account_a/data/*.json` | `data/account_a/*.json` (done by migrate_v2.py) |
| `account_b/data/*.json` | `data/archive/account_b/*.json` (done by migrate_v2.py) |

### Added
- `data/prometheus.db` — SQLite history (closed_trades, daily_snapshots,
  trade_events, journal_entries). Backfilled from existing JSON on first run.
- `report/db.py` — SQLite layer (connect, upsert helpers, read helpers).
- `report/dashboard/backend.py` v3 — single-account, SQLite-backed.
- `scripts/migrate_v2.py` — one-shot VPS migration script.
- 12 new unit tests for `report/db.py` and the expanded daily message
  (62 total tests now in `report/tests/`).

### Reports — expanded daily message
The daily Telegram message now also shows:
- **Today's decisions** — approved theses + rejected with the failing reason
- **Today's activity** — trades opened today, trades closed today with PnL

Weekly message unchanged.

## Migration steps on the VPS

```bash
# 1. Pull the new branch
cd ~/prometheus
git pull origin claude/daily-weekly-report-accuracy-qON8O

# 2. Make sure deps are installed (python-dotenv is now optional but recommended)
pip install -r report/requirements.txt

# 3. Dry-run the migration first — prints what it would do, changes nothing
python3 scripts/migrate_v2.py --dry-run

# 4. Run it for real (interactive — asks before destructive ops)
python3 scripts/migrate_v2.py

#    Or non-interactive if the dry run looked right:
#    python3 scripts/migrate_v2.py --yes

# 5. Sanity check
python3 -m unittest discover -s report/tests -v       # 62 tests pass
python3 -m report.daily                               # sends daily Telegram
python3 -m report.weekly                              # sends weekly Telegram
```

## Cron — replace the old entries

Remove any cron lines referencing `run_parallel.py`, `send_weekly_report.py`,
`phase3/run_phase3.py`, or `run_account_b.py`. Replace with:

```cron
# Trading run (research → Account A pipeline)
0  21 * * 1-5  cd ~/prometheus && /usr/bin/python3 -m trading.run    >> data/logs/trading.log 2>&1

# Daily Telegram + SQLite snapshot — ~30min after trading run
30 21 * * 1-5  cd ~/prometheus && /usr/bin/python3 -m report.daily   >> data/logs/daily.log   2>&1

# Weekly Telegram — Sat 08:00 SGT, after Fri US close
0  8  * * 6    cd ~/prometheus && /usr/bin/python3 -m report.weekly  >> data/logs/weekly.log  2>&1
```

If you use a venv (e.g. `~/prometheus/venv/`), replace `/usr/bin/python3` with
the venv's python path.

## Rollback

The migration script always copies `data/` to `data.backup.<timestamp>/`
before touching anything. To roll back:

```bash
rm -rf ~/prometheus/data
mv ~/prometheus/data.backup.<timestamp> ~/prometheus/data
git checkout main   # or whatever branch you were on
```

The trading strategy logic (risk, execution, monitor, journal agents) is
byte-identical to the pre-restructure code. Only paths and imports changed.
