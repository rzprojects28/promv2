"""
Prometheus — Telegram Alerts (Updated for parallel A/B accounts)
Sends labeled notifications so you can tell Account A from Account B.
"""
import requests
import os
from datetime import datetime
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.expanduser('~/prometheus/.env'))

BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
CHAT_ID   = os.getenv('TELEGRAM_CHAT_ID', '')


def send(message: str) -> bool:
    if not BOT_TOKEN or not CHAT_ID or 'YOUR' in BOT_TOKEN:
        print(f"  [Telegram] NOT CONFIGURED\n  {message[:100]}")
        return False
    try:
        resp = requests.post(
            f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage',
            json={'chat_id': CHAT_ID, 'text': message, 'parse_mode': 'HTML'},
            timeout=10
        )
        return resp.status_code == 200
    except Exception as e:
        print(f"  [Telegram] Send failed: {e}")
        return False


def send_trade_opened_labeled(thesis: dict, order: dict,
                               account_value: float, account_label: str):
    ticker     = thesis.get('ticker', '')
    direction  = thesis.get('direction', '')
    conviction = thesis.get('conviction', '')
    sector     = thesis.get('sector', '')
    size_pct   = thesis.get('position_size_pct', '')
    size_usd   = account_value * (float(size_pct) / 100) if size_pct else 0

    is_learning = 'LEARNING' in account_label
    acct_emoji  = "🟣" if is_learning else "⚪"
    dir_emoji   = "🟢" if direction == 'LONG' else "🔴"
    conv_emoji  = {"HIGH": "🔥", "MEDIUM": "⚡", "LOW": "💡"}.get(conviction, "")

    msg = (
        f"{dir_emoji} <b>PAPER TRADE — ACCOUNT {account_label}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{acct_emoji} <b>{'Self-Improving (Learning ON)' if is_learning else 'Baseline (No Learning)'}</b>\n"
        f"{conv_emoji} Conviction: <b>{conviction}</b> | {direction}\n"
        f"Ticker: <b>{ticker}</b> | {sector}\n"
        f"Size: {size_pct}% (≈${size_usd:,.0f})\n"
        f"Entry: {order.get('qty')} shares @ ${order.get('limit_price')}\n"
        f"\n<b>WHY THIS TRADE:</b>\n{thesis.get('core_thesis','')}\n"
        f"\n<b>CATALYST:</b>\n{thesis.get('catalyst','')}\n"
        f"\n<b>EXIT IF:</b>\n{thesis.get('invalidation_conditions','')}\n"
        f"\n<b>DEADLINE:</b> {thesis.get('deadline_date') or thesis.get('hard_time_limit','')}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>{datetime.now().strftime('%Y-%m-%d %H:%M')} SGT</i>"
    )
    send(msg)


def send_trade_opened(thesis: dict, order: dict, account_value: float):
    send_trade_opened_labeled(thesis, order, account_value, 'SINGLE')


def send_trade_closed(position: dict, reason: str, pnl_pct: float = None, exit_price: float = None):
    ticker     = position.get('ticker', '')
    direction  = position.get('direction', '')
    account    = position.get('account', '')
    label      = 'A — BASELINE' if 'A_' in account else 'B — LEARNING' if 'B_' in account else account
    pnl_str    = f"{pnl_pct:+.1f}%" if pnl_pct is not None else "N/A"
    emoji      = "✅" if (pnl_pct and pnl_pct >= 0) else "❌"

    # Format exit price — use passed value or fall back to position's exit_price field
    ep = exit_price or position.get('exit_price')
    exit_price_str = f" @ ${float(ep):.2f}" if ep else ""

    msg = (
        f"{emoji} <b>CLOSED — {label}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>{ticker}</b> {direction}\n"
        f"Entry: {position.get('entry_date')} @ ${position.get('entry_price')}\n"
        f"Exit:  {datetime.now().strftime('%Y-%m-%d')}{exit_price_str}\n"
        f"P&L:   {pnl_str}\n\n"
        f"<b>Reason:</b> {reason}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>{datetime.now().strftime('%Y-%m-%d %H:%M')} SGT</i>"
    )
    send(msg)


def send_invalidation(position: dict, condition: str):
    ticker  = position.get('ticker', '')
    account = position.get('account', '')
    label   = 'A — BASELINE' if 'A_' in account else 'B — LEARNING' if 'B_' in account else account
    send(
        f"⚠️ <b>INVALIDATION — {label}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>{ticker}</b> thesis broken\n"
        f"Condition: {condition}\n"
        f"Action: EXIT placed automatically\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>{datetime.now().strftime('%Y-%m-%d %H:%M')} SGT</i>"
    )


def send_daily_summary(open_positions, theses_count, approved_count, closed_today):
    lines = [
        f"📊 <b>PROMETHEUS DAILY REPORT</b>",
        f"━━━━━━━━━━━━━━━━━━━━━━",
        f"<b>{datetime.now().strftime('%A, %d %b %Y')}</b>",
        f"",
        f"Theses generated: {theses_count}",
        f"Passed risk: {approved_count}",
        f"Open: {len(open_positions)} | Closed today: {len(closed_today)}",
        f"",
    ]
    if open_positions:
        lines.append("<b>OPEN:</b>")
        for p in open_positions:
            acct  = p.get('account', '')
            label = 'A' if 'A_' in acct else 'B' if 'B_' in acct else '?'
            lines.append(f"  [{label}] {p.get('ticker')} {p.get('direction')} @ ${p.get('entry_price')}")
    if closed_today:
        lines.append("<b>CLOSED TODAY:</b>")
        for p in closed_today:
            pnl     = p.get('pnl_pct', 'N/A')
            pnl_str = f"{pnl:+.1f}%" if isinstance(pnl, float) else str(pnl)
            acct    = p.get('account', '')
            label   = 'A' if 'A_' in acct else 'B' if 'B_' in acct else '?'
            ep      = p.get('exit_price')
            ep_str  = f" @ ${float(ep):.2f}" if ep else ""
            lines.append(f"  [{label}] {p.get('ticker')} {pnl_str}{ep_str}")
    lines += ["━━━━━━━━━━━━━━━━━━━━━━", "<i>Next run: tomorrow 9pm SGT</i>"]
    send('\n'.join(lines))


def send_ab_weekly_summary(stats_a: dict, stats_b: dict):
    def fmt(v): return f"{v:+.1f}%" if isinstance(v, float) else str(v)

    wr_a     = stats_a.get('overall_win_rate', 0)
    wr_b     = stats_b.get('overall_win_rate', 0)
    ap_a     = stats_a.get('overall_avg_pnl', 0)
    ap_b     = stats_b.get('overall_avg_pnl', 0)
    delta_wr = round(wr_b - wr_a, 1)
    delta_ap = round(ap_b - ap_a, 2)
    verdict  = "LEARNING BETTER 🟣" if delta_wr > 2 else "BASELINE BETTER ⚪" if delta_wr < -2 else "INCONCLUSIVE ⚖️"

    send(
        f"📓 <b>WEEKLY A/B REPORT</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Account A — Baseline</b>\n"
        f"  Trades: {stats_a.get('total_trades',0)} | Win rate: {wr_a}% | Avg P&L: {fmt(ap_a)}\n\n"
        f"<b>Account B — Learning</b>\n"
        f"  Trades: {stats_b.get('total_trades',0)} | Win rate: {wr_b}% | Avg P&L: {fmt(ap_b)}\n\n"
        f"<b>Delta (B vs A):</b>\n"
        f"  Win rate: {delta_wr:+.1f}pp | Avg P&L: {delta_ap:+.2f}%\n\n"
        f"<b>Verdict: {verdict}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>{datetime.now().strftime('%Y-%m-%d %H:%M')} SGT</i>"
    )

def send_risk_summary(approved: list, rejected: list, account_label: str):
    """Send daily risk gate summary — what passed and what was blocked."""
    is_learning = 'LEARNING' in account_label
    acct_emoji  = "🟣" if is_learning else "⚪"

    lines = [
        f"{acct_emoji} <b>RISK MANAGER — {account_label}</b>",
        f"━━━━━━━━━━━━━━━━━━━━━━",
    ]

    if approved:
        lines.append(f"<b>✅ APPROVED ({len(approved)}):</b>")
        for t in approved:
            lines.append(f"  {t.get('ticker')} {t.get('direction')} [{t.get('conviction')}] — {t.get('position_size_pct')}%")

    if rejected:
        lines.append(f"\n<b>❌ REJECTED ({len(rejected)}):</b>")
        for t in rejected:
            ticker = t.get('ticker','')
            # Find the first failing check
            fail_reason = 'Unknown reason'
            for msg in t.get('risk_checks', []):
                if msg.startswith('✗'):
                    fail_reason = msg[2:].strip()
                    break
            lines.append(f"  {ticker} — {fail_reason}")

    if not approved and not rejected:
        lines.append("No theses evaluated today.")

    lines.append(f"\n<i>{datetime.now().strftime('%Y-%m-%d %H:%M')} SGT</i>")
    send('\n'.join(lines))

def send_portfolio_snapshot(snap_a: dict, snap_b: dict):
    """Send full P&L and risk snapshot for both accounts."""

    def fmt_pnl(v):
        return f"{v:+.2f}%" if v != 0 else "0.00%"

    def account_block(s):
        is_b = 'LEARNING' in s['label']
        emoji = "🟣" if is_b else "⚪"
        ccy = s.get('currency', 'USD')
        lines = [
            f"{emoji} <b>{s['label']}</b>",
            f"  Account value:  {ccy} {s['account_value']:,.0f}",
            f"  Realized P&L:   {fmt_pnl(s['realized_pnl'])} ({s['closed_trades']} closed | {s['win_rate']}% WR)",
            f"  Unrealized P&L: {ccy} {s['unrealized_pnl']:+,.2f}",
            f"  Open positions: {s['open_trades']} ({s['deployed_pct']}% deployed)",
            f"  Top sector:     {s['top_sector'][0]} ({s['top_sector'][1]:.1f}%)",
        ]
        if s['positions_pnl']:
            lines.append("  <b>Open P&L (USD, per contract):</b>")
            for p in sorted(s['positions_pnl'], key=lambda x: x['pnl_usd'], reverse=True):
                bar = "▲" if p['pnl_usd'] >= 0 else "▼"
                lines.append(f"    {bar} {p['ticker']:<6} ${p['pnl_usd']:+,.2f} ({p['pct']:+.1f}%)")
        return '\n'.join(lines)

    # Combined delta
    delta_unreal = snap_b['unrealized_pnl'] - snap_a['unrealized_pnl']
    delta_wr     = round(snap_b['win_rate'] - snap_a['win_rate'], 1)
    verdict      = "🟣 Learning ahead" if delta_wr > 2 else "⚪ Baseline ahead" if delta_wr < -2 else "⚖️ Inconclusive"

    msg = (
        f"💰 <b>PORTFOLIO SNAPSHOT</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{account_block(snap_a)}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{account_block(snap_b)}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>A/B Delta:</b> Unrealized {delta_unreal:+,.2f} | WR {delta_wr:+.1f}pp\n"
        f"<b>Verdict:</b> {verdict}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>{datetime.now().strftime('%Y-%m-%d %H:%M')} SGT</i>"
    )
    send(msg)

# Aliases for backwards compatibility
def alert_trade_entry(thesis, order_result):
    send_trade_opened(thesis, order_result, 100_000)

def alert_trade_exit(position, reason, pnl_pct=None):
    send_trade_closed(position, reason, pnl_pct)

def alert_invalidation(position, condition):
    send_invalidation(position, condition)

def alert_daily_summary(open_positions, theses_count, approved_count):
    send_daily_summary(open_positions, theses_count, approved_count, [])


if __name__ == '__main__':
    print("Testing Telegram...")
    ok = send("🤖 <b>Prometheus Parallel A/B System</b> — Telegram working.\n"
              "⚪ Account A (Baseline) and 🟣 Account B (Learning) both active.")
    print("OK" if ok else "Failed — check .env")
