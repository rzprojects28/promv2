"""
SQLite history database.

Append-only store for historical analytics that don't belong in the live
JSON state files. Single file at data/prometheus.db, queryable with any
SQLite tool.

Tables:
  closed_trades     — one row per closed position (mirrors closed_positions.json)
  daily_snapshots   — one row per day per account (account_value, exposure, etc.)
  trade_events      — log of approved/rejected/opened/closed/invalidation events
  journal_entries   — one row per journaled trade (mirrors trade_journal.json)

Use:
    from report.db import connect, upsert_closed_trade, write_daily_snapshot
    conn = connect()
    upsert_closed_trade(conn, closed_position_dict, account='A')
    conn.close()
"""
import json
import os
import sqlite3
from datetime import datetime
from typing import Iterable, Optional


BASE_DIR = os.path.expanduser('~/promv2')
DB_PATH  = os.path.join(BASE_DIR, 'data', 'prometheus.db')


SCHEMA = """
CREATE TABLE IF NOT EXISTS closed_trades (
    trade_id          TEXT PRIMARY KEY,        -- "{ticker}_{entry_date}"
    account           TEXT NOT NULL,
    ticker            TEXT NOT NULL,
    direction         TEXT,
    conviction        TEXT,
    sector            TEXT,
    entry_date        TEXT,
    exit_date         TEXT,
    entry_price       REAL,
    exit_price        REAL,
    entry_qty         INTEGER,
    entry_size_usd    REAL,
    pnl_pct           REAL,
    pnl_usd           REAL,
    exit_reason       TEXT,
    exit_category     TEXT,
    raw_json          TEXT,
    archived_at       TEXT
);

CREATE INDEX IF NOT EXISTS idx_closed_exit_date    ON closed_trades(exit_date);
CREATE INDEX IF NOT EXISTS idx_closed_entry_date   ON closed_trades(entry_date);
CREATE INDEX IF NOT EXISTS idx_closed_account      ON closed_trades(account);

CREATE TABLE IF NOT EXISTS daily_snapshots (
    snapshot_date            TEXT NOT NULL,
    account                  TEXT NOT NULL,
    currency                 TEXT,
    account_value            REAL,
    open_trades              INTEGER,
    total_unrealized_usd     REAL,
    unrealized_pct_account   REAL,
    unrealized_pct_avg_pos   REAL,
    budgeted_risk_usd        REAL,
    live_risk_usd            REAL,
    positions_missing_stop   INTEGER,
    realized_today_usd       REAL,
    raw_stats_json           TEXT,
    created_at               TEXT,
    PRIMARY KEY (snapshot_date, account)
);

CREATE INDEX IF NOT EXISTS idx_snap_date    ON daily_snapshots(snapshot_date);
CREATE INDEX IF NOT EXISTS idx_snap_account ON daily_snapshots(account);

CREATE TABLE IF NOT EXISTS trade_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_time    TEXT NOT NULL,
    event_date    TEXT NOT NULL,
    account       TEXT NOT NULL,
    event_type    TEXT NOT NULL,   -- approved | rejected | opened | closed | invalidation
    ticker        TEXT,
    direction     TEXT,
    detail        TEXT,
    raw_json      TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_date    ON trade_events(event_date);
CREATE INDEX IF NOT EXISTS idx_events_account ON trade_events(account);
CREATE INDEX IF NOT EXISTS idx_events_type    ON trade_events(event_type);

CREATE TABLE IF NOT EXISTS journal_entries (
    trade_id      TEXT PRIMARY KEY,
    account       TEXT NOT NULL,
    reviewed_at   TEXT,
    ticker        TEXT,
    direction     TEXT,
    pnl_pct       REAL,
    verdict       TEXT,
    key_lesson    TEXT,
    review_json   TEXT
);
"""


def connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    """
    Open (and lazily create) the database. Always returns a connection with
    the schema applied — safe to call from anywhere.
    """
    path = db_path or DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# ────────────────────────────────────────────────────────────────────
# Upserts (idempotent — safe to re-run, including for backfill)
# ────────────────────────────────────────────────────────────────────

def upsert_closed_trade(conn: sqlite3.Connection, position: dict, account: str) -> None:
    ticker     = position.get('ticker', '')
    entry_date = position.get('entry_date', '')
    trade_id   = f"{ticker}_{entry_date}"
    pnl_pct    = float(position.get('pnl_pct', 0) or 0)
    size_usd   = float(position.get('entry_size_usd', 0) or 0)
    pnl_usd    = round(pnl_pct / 100.0 * size_usd, 2)
    conn.execute("""
        INSERT INTO closed_trades (
            trade_id, account, ticker, direction, conviction, sector,
            entry_date, exit_date, entry_price, exit_price, entry_qty,
            entry_size_usd, pnl_pct, pnl_usd, exit_reason, exit_category,
            raw_json, archived_at
        ) VALUES (
            :trade_id, :account, :ticker, :direction, :conviction, :sector,
            :entry_date, :exit_date, :entry_price, :exit_price, :entry_qty,
            :entry_size_usd, :pnl_pct, :pnl_usd, :exit_reason, :exit_category,
            :raw_json, :archived_at
        )
        ON CONFLICT(trade_id) DO UPDATE SET
            exit_date     = excluded.exit_date,
            exit_price    = excluded.exit_price,
            pnl_pct       = excluded.pnl_pct,
            pnl_usd       = excluded.pnl_usd,
            exit_reason   = excluded.exit_reason,
            exit_category = excluded.exit_category,
            raw_json      = excluded.raw_json,
            archived_at   = excluded.archived_at
    """, {
        'trade_id':       trade_id,
        'account':        account,
        'ticker':         ticker,
        'direction':      position.get('direction'),
        'conviction':     position.get('conviction'),
        'sector':         position.get('sector'),
        'entry_date':     entry_date,
        'exit_date':      position.get('exit_date'),
        'entry_price':    float(position.get('entry_price', 0) or 0),
        'exit_price':     float(position.get('exit_price', 0) or 0),
        'entry_qty':      int(float(position.get('entry_qty', 0) or 0)),
        'entry_size_usd': size_usd,
        'pnl_pct':        pnl_pct,
        'pnl_usd':        pnl_usd,
        'exit_reason':    position.get('exit_reason'),
        'exit_category':  position.get('exit_category'),
        'raw_json':       json.dumps(position),
        'archived_at':    datetime.utcnow().isoformat(),
    })
    conn.commit()


def write_daily_snapshot(conn: sqlite3.Connection, stats: dict, account: str,
                         realized_today_usd: float = 0.0,
                         snapshot_date: Optional[str] = None) -> None:
    """
    Save the stats dict produced by report.stats.compute_daily_stats as a
    historical row. snapshot_date defaults to today's UTC date in YYYY-MM-DD.
    """
    d = snapshot_date or datetime.utcnow().strftime('%Y-%m-%d')
    conn.execute("""
        INSERT INTO daily_snapshots (
            snapshot_date, account, currency, account_value, open_trades,
            total_unrealized_usd, unrealized_pct_account, unrealized_pct_avg_pos,
            budgeted_risk_usd, live_risk_usd, positions_missing_stop,
            realized_today_usd, raw_stats_json, created_at
        ) VALUES (
            :snapshot_date, :account, :currency, :account_value, :open_trades,
            :total_unrealized_usd, :unrealized_pct_account, :unrealized_pct_avg_pos,
            :budgeted_risk_usd, :live_risk_usd, :positions_missing_stop,
            :realized_today_usd, :raw_stats_json, :created_at
        )
        ON CONFLICT(snapshot_date, account) DO UPDATE SET
            currency               = excluded.currency,
            account_value          = excluded.account_value,
            open_trades            = excluded.open_trades,
            total_unrealized_usd   = excluded.total_unrealized_usd,
            unrealized_pct_account = excluded.unrealized_pct_account,
            unrealized_pct_avg_pos = excluded.unrealized_pct_avg_pos,
            budgeted_risk_usd      = excluded.budgeted_risk_usd,
            live_risk_usd          = excluded.live_risk_usd,
            positions_missing_stop = excluded.positions_missing_stop,
            realized_today_usd     = excluded.realized_today_usd,
            raw_stats_json         = excluded.raw_stats_json,
            created_at             = excluded.created_at
    """, {
        'snapshot_date':          d,
        'account':                account,
        'currency':               stats.get('currency'),
        'account_value':          stats.get('account_value'),
        'open_trades':            stats.get('open_trades'),
        'total_unrealized_usd':   stats.get('total_unrealized_usd'),
        'unrealized_pct_account': stats.get('unrealized_pct_of_account'),
        'unrealized_pct_avg_pos': stats.get('unrealized_pct_avg_position'),
        'budgeted_risk_usd':      stats.get('budgeted_risk_usd'),
        'live_risk_usd':          stats.get('live_risk_usd'),
        'positions_missing_stop': stats.get('positions_missing_stop'),
        'realized_today_usd':     realized_today_usd,
        'raw_stats_json':         json.dumps(stats),
        'created_at':             datetime.utcnow().isoformat(),
    })
    conn.commit()


def log_event(conn: sqlite3.Connection, event_type: str, account: str,
              ticker: str = '', direction: str = '', detail: str = '',
              raw: Optional[dict] = None, when: Optional[datetime] = None) -> None:
    t = when or datetime.utcnow()
    conn.execute("""
        INSERT INTO trade_events (event_time, event_date, account, event_type,
                                  ticker, direction, detail, raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        t.isoformat(), t.strftime('%Y-%m-%d'), account, event_type,
        ticker, direction, detail, json.dumps(raw) if raw else None,
    ))
    conn.commit()


def upsert_journal_entry(conn: sqlite3.Connection, entry: dict, account: str) -> None:
    review = entry.get('review', {}) or {}
    conn.execute("""
        INSERT INTO journal_entries (trade_id, account, reviewed_at, ticker,
                                     direction, pnl_pct, verdict, key_lesson, review_json)
        VALUES (:trade_id, :account, :reviewed_at, :ticker, :direction,
                :pnl_pct, :verdict, :key_lesson, :review_json)
        ON CONFLICT(trade_id) DO UPDATE SET
            reviewed_at = excluded.reviewed_at,
            pnl_pct     = excluded.pnl_pct,
            verdict     = excluded.verdict,
            key_lesson  = excluded.key_lesson,
            review_json = excluded.review_json
    """, {
        'trade_id':    entry.get('trade_id'),
        'account':     account,
        'reviewed_at': entry.get('reviewed_at'),
        'ticker':      entry.get('ticker'),
        'direction':   entry.get('direction'),
        'pnl_pct':     float(entry.get('pnl_pct', 0) or 0),
        'verdict':     review.get('verdict'),
        'key_lesson':  review.get('key_lesson'),
        'review_json': json.dumps(review),
    })
    conn.commit()


# ────────────────────────────────────────────────────────────────────
# Read helpers (for dashboard + weekly diff)
# ────────────────────────────────────────────────────────────────────

def closed_trades_in_range(conn: sqlite3.Connection, start: str, end: str,
                           account: Optional[str] = None) -> list:
    sql = "SELECT * FROM closed_trades WHERE exit_date >= ? AND exit_date <= ?"
    args = [start, end]
    if account:
        sql += " AND account = ?"
        args.append(account)
    sql += " ORDER BY exit_date DESC"
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def daily_snapshots(conn: sqlite3.Connection, account: Optional[str] = None,
                    limit: int = 365) -> list:
    sql = "SELECT * FROM daily_snapshots"
    args = []
    if account:
        sql += " WHERE account = ?"
        args.append(account)
    sql += " ORDER BY snapshot_date DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def recent_events(conn: sqlite3.Connection, account: Optional[str] = None,
                  limit: int = 50) -> list:
    sql = "SELECT * FROM trade_events"
    args = []
    if account:
        sql += " WHERE account = ?"
        args.append(account)
    sql += " ORDER BY event_time DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in conn.execute(sql, args).fetchall()]
