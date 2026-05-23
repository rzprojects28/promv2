"""
Weekly per-account Telegram report.

Usage:
    python3 -m report.weekly

Cron (fires Sat 08:00 SGT, after Fri US close):
    0 8 * * 6  cd ~/prometheus && /usr/bin/python3 -m report.weekly

Window: Mon 00:00 → Sun 23:59 SGT of the most recently completed week
        (see report.stats.week_bounds_sgt for the full rule).

Reads:  data/account_a/{open,closed}_positions.json
        IBKR live prices + NetLiquidation
Writes: Telegram message per account. (Does not write to SQLite — the
        weekly report is read-only; the daily report owns snapshots.)
"""
import sys, os, traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from report import stats, messages, telegram, positions_loader, ibkr


def run_for_account(data_dir: str, ib_port: int, label: str) -> None:
    open_p, closed_p = positions_loader.load_account_positions(data_dir)
    acct_val, currency, prices = ibkr.fetch_account_value_and_prices(
        open_p, ib_port, label
    )
    s = stats.compute_weekly_stats(
        open_positions=open_p,
        closed_positions=closed_p,
        current_prices=prices,
        account_value=acct_val,
        account_label=label,
        currency=currency,
    )
    print(f"  [{label}] window {s['week_start']} → {s['week_end']}  "
          f"opened={s['opened_count']}  closed={s['closed_count']}  "
          f"realized={currency} {s['realized_pnl_usd']:+.2f}")
    telegram.send(messages.build_weekly_message(s))


def main() -> None:
    for data_dir, ib_port, label in positions_loader.ACCOUNTS:
        try:
            run_for_account(data_dir, ib_port, label)
        except Exception as e:
            print(f"[{label}] Weekly report failed: {e}")
            traceback.print_exc()
    print("Weekly reports sent.")


if __name__ == '__main__':
    main()
