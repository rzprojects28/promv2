# CLAUDE.md — project conventions for Claude Code

## Security guardrails — MANDATORY before any commit/push

Before any `git add`, `git commit`, or `git push` you propose, you MUST:

1. Scan the staged file list and diff for:
   - Plaintext passwords (e.g. `TWS_PASSWORD: "literal"`, `password=`, `pwd=`)
   - API keys / tokens / bearer strings — typically 20+ char random literals
   - Account credentials (IBKR usernames count — flag if hardcoded outside env vars)
   - Private keys (`-----BEGIN ... PRIVATE KEY-----`)
   - URLs with embedded credentials (`https://user:pass@...`)

2. Refuse to push if any of these paths are staged:
   - `settings/`, `settings_a/`, `settings_b/` (IB Gateway encrypted credentials + logs)
   - `.env`, `*.env`, `*.pem`, `*.key`, `id_rsa*`, `*.ibgzenc`
   - `data/account_*/*.json` (live trade state)
   - `data/*.db`, `data/*.db-journal` (SQLite history)
   - `reports/*.txt`, `data/logs/*.log`, `data.backup.*/`

3. When you find a candidate secret, STOP and print:
   - File + line number
   - What you think it is
   - Whether it's an env-var reference (`${VAR}`, `os.getenv(...)`) — those are fine
   - Whether it's a hardcoded literal — those are NOT fine; move to .env first
   - Whether the secret may already be in git history — flag the need to rotate

4. Verify `.gitignore` covers the sensitive paths above before any push. If a category is missing, propose adding it before staging.

5. Never use `git add -A` or `git add .` without first listing what's about to be staged. Always show the user the file list.

## Single-account constraint

Account B was removed in the v2 restructure. Do NOT re-introduce any of:
- `account_b/` folder, `run_account_b.py`, `learning_engine.py`
- A/B test labels in Telegram messages
- `with_learning` learning_mode branches in analysis_agent
- `ib-gateway-b` service in docker-compose.yml
- Port 4003 references

If multi-account becomes a real need later, generalize the `ACCOUNTS` list in `report/positions_loader.py` rather than copy-paste another account folder.

## Trading strategy is sacred

Code under `trading/research/`, `trading/risk/`, `trading/execution/`, `trading/monitor/`, `trading/journal/` is the live trading strategy. Never modify business logic there unless the user explicitly asks. Path / import fixes are fine; behavior changes are not.

## Layout

    trading/   strategy code (research -> risk -> execution -> monitor -> journal)
               never imports from report/
    report/    daily + weekly Telegram + FastAPI dashboard + SQLite history layer
               reads data/account_a/*.json + data/prometheus.db
               never writes position JSON files
    data/      live state + history (mostly gitignored; only .gitkeep tracked)
               data/account_a/         live JSON (operational)
               data/archive/account_b/ frozen B data (archived, do not revive)
               data/snapshots/         daily JSON snapshots (optional)
               data/logs/              cron log output
               data/prometheus.db      SQLite history
    scripts/   one-shot utilities (migration, etc.)

## Cron (production VPS)

    40 21 * * 1-5  python3 -m trading.run     (trading pipeline)
    0  5  * * 2-6  python3 -m report.daily    (daily Telegram + SQLite snapshot)
    0  8  * * 6    python3 -m report.weekly   (weekly Telegram)

Don't propose schedule changes without asking.

## Tests

After any change to `report/` or `trading/`, run:

    python3 -m unittest discover -s report/tests -v

62 tests should pass. If any fail, fix before committing.
