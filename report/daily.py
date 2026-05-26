"""
Daily per-account Telegram report.

Usage:
    python3 -m report.daily

Cron (fires after the trading run completes):
    30 21 * * 1-5  cd ~/promv2 && /usr/bin/python3 -m report.daily

What it does, in order:
    1. Read open + closed positions (data/account_a/*.json) and today's risk-gate output
    2. Fetch live prices + NetLiquidation from IBKR (yfinance fallback for missing prices)
    3. Compute stats via report.stats.compute_daily_stats
    4. Build the Telegram message via report.messages.build_daily_message
       — message includes: open trades, $ at risk, unrealized PnL %,
         plus today's approved/rejected theses and trades opened/closed today
    5. Send to Telegram
    6. Persist a daily snapshot to data/prometheus.db (idempotent: re-runs upsert)

Writes:
    Telegram message + one row in daily_snapshots for each account.
"""
import sys, os, traceback
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from report import stats, messages, telegram, positions_loader, ibkr, db


def _today_str() -> str:
    return datetime.utcnow().strftime('%Y-%m-%d')


def _trades_for_today(open_p: list, closed_p: list) -> dict:
    today = _today_str()
    opened = [p for p in open_p   if (p.get('entry_date') or '')[:10] == today]
    closed = [p for p in closed_p if (p.get('exit_date')  or '')[:10] == today]
    return {'opened_today': opened, 'closed_today': closed}


def _realized_today_usd(closed_p: list) -> float:
    today = _today_str()
    rows  = [p for p in closed_p if (p.get('exit_date') or '')[:10] == today]
    return round(
        sum((float(p.get('pnl_pct', 0) or 0) / 100.0) * float(p.get('entry_size_usd', 0) or 0)
            for p in rows),
        2,
    )


def run_for_account(data_dir: str, ib_port: int, label: str, conn=None) -> dict:
    open_p, closed_p = positions_loader.load_account_positions(data_dir)
    approved, rejected = positions_loader.load_approved_rejected(data_dir)
    activity = _trades_for_today(open_p, closed_p)

    acct_val, currency, prices = ibkr.fetch_account_value_and_prices(
        open_p, ib_port, label
    )

    s = stats.compute_daily_stats(
        open_positions=open_p,
        current_prices=prices,
        account_value=acct_val,
        account_label=label,
        currency=currency,
    )

    # Attach decisions + activity so the message builder can include them.
    s['approved_today'] = approved
    s['rejected_today'] = rejected
    s['opened_today']   = activity['opened_today']
    s['closed_today']   = activity['closed_today']

    print(f"  [{label}] open={s['open_trades']}  "
          f"unreal={currency} {s['total_unrealized_usd']:+.2f}  "
          f"risk_live={currency} {s['live_risk_usd']:,.2f}  "
          f"approved={len(approved)} rejected={len(rejected)} "
          f"opened_today={len(activity['opened_today'])} closed_today={len(activity['closed_today'])}")

    telegram.send(messages.build_daily_message(s))

    # Persist snapshot to SQLite (idempotent — same date+account upserts).
    if conn is not None:
        slim = {k: v for k, v in s.items()
                if k not in ('positions', 'approved_today', 'rejected_today',
                             'opened_today', 'closed_today')}
        account_code = label.split(' ')[0]    # 'A' from 'A — BASELINE'
        db.write_daily_snapshot(
            conn, slim, account=account_code,
            realized_today_usd=_realized_today_usd(closed_p),
        )

    return s


def main() -> None:
    conn = None
    try:
        conn = db.connect()
    except Exception as e:
        print(f"  [DB] connect failed (snapshots will be skipped): {e}")

    for data_dir, ib_port, label in positions_loader.ACCOUNTS:
        try:
            run_for_account(data_dir, ib_port, label, conn=conn)
        except Exception as e:
            print(f"[{label}] Daily report failed: {e}")
            traceback.print_exc()

    if conn is not None:
        conn.close()
    print("Daily reports sent.")


if __name__ == '__main__':
    main()
