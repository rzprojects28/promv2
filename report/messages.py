"""
Build the daily and weekly Telegram message strings from stats dicts.

Pure formatting — no IO, no IBKR, no Telegram send. Easy to snapshot-test
or eyeball.
"""
from datetime import datetime


def _fmt_money(amount: float, currency: str) -> str:
    sign = "+" if amount >= 0 else ""
    return f"{currency} {sign}{amount:,.2f}"


def build_daily_message(stats: dict) -> str:
    """
    Per-account daily snapshot — exactly the three things the user asked for:
      1. Number of open trades
      2. Current risk exposure ($ at risk, budgeted + live)
      3. Unrealized PnL (portfolio % and avg position %, in account currency)
    """
    label    = stats.get("account", "?")
    ccy      = stats.get("currency", "USD")
    is_b     = "LEARNING" in label or label.strip().startswith("B")
    emoji    = "🟣" if is_b else "⚪"

    n_open      = stats.get("open_trades", 0)
    priced      = stats.get("priced_count", 0)
    unreal_usd  = stats.get("total_unrealized_usd", 0.0)
    unreal_acct = stats.get("unrealized_pct_of_account", 0.0)
    unreal_pos  = stats.get("unrealized_pct_avg_position", 0.0)
    risk_b      = stats.get("budgeted_risk_usd", 0.0)
    risk_l      = stats.get("live_risk_usd", 0.0)
    risk_b_pct  = stats.get("budgeted_risk_pct_of_account", 0.0)
    risk_l_pct  = stats.get("live_risk_pct_of_account", 0.0)
    missing     = stats.get("positions_missing_stop", 0)
    acct_val    = stats.get("account_value", 0.0)

    lines = [
        f"{emoji} <b>DAILY — {label}</b>",
        f"━━━━━━━━━━━━━━━━━━━━━━",
        f"Open trades:    <b>{n_open}</b>"
        + (f"  ({priced}/{n_open} priced)" if n_open and priced != n_open else ""),
        f"Account value:  {ccy} {acct_val:,.2f}",
        f"",
        f"<b>Unrealized PnL</b>",
        f"  {_fmt_money(unreal_usd, ccy)}",
        f"  {unreal_acct:+.2f}% of account · {unreal_pos:+.2f}% avg position",
        f"",
        f"<b>Risk exposure ($ at risk)</b>",
        f"  Budgeted: {_fmt_money(risk_b, ccy)}  ({risk_b_pct:.2f}% of account)",
        f"  Live:     {_fmt_money(risk_l, ccy)}  ({risk_l_pct:.2f}% of account)",
    ]
    if missing:
        lines.append(f"  <i>({missing} position(s) without parsed stop — counted at full size)</i>")

    if stats.get("positions"):
        lines.append("")
        lines.append("<b>Open positions</b>")
        for p in stats["positions"]:
            up     = p.get("unrealized_pct")
            up_str = f"{up:+.1f}%" if up is not None else "  N/A"
            bar    = "▲" if (up or 0) >= 0 else "▼"
            lines.append(
                f"  {bar} {p['ticker']:<6} {p.get('direction','?'):<5} "
                f"{up_str}  (entry ${p['entry_price']:.2f})"
            )

    # ── Today's decisions (risk-gate output) ──
    approved = stats.get("approved_today") or []
    rejected = stats.get("rejected_today") or []
    if approved or rejected:
        lines.append("")
        lines.append(f"<b>Today's decisions</b>  ({len(approved)} approved · {len(rejected)} rejected)")
        for t in approved:
            lines.append(
                f"  ✅ {t.get('ticker','?'):<6} {t.get('direction','?'):<5} "
                f"[{t.get('conviction','')}] {float(t.get('position_size_pct') or 0):.1f}%"
            )
        for t in rejected:
            # Surface the first failed check, like send_risk_summary does.
            fail = next(
                (m[2:].strip() for m in (t.get('risk_checks') or []) if str(m).startswith('✗')),
                'rejected',
            )
            lines.append(f"  ❌ {t.get('ticker','?'):<6} — {fail[:60]}")

    # ── Today's activity (trades that actually opened or closed today) ──
    opened_today = stats.get("opened_today") or []
    closed_today = stats.get("closed_today") or []
    if opened_today or closed_today:
        lines.append("")
        lines.append(f"<b>Today's activity</b>  ({len(opened_today)} opened · {len(closed_today)} closed)")
        for p in opened_today:
            lines.append(
                f"  ▶ Opened {p.get('ticker','?'):<6} {p.get('direction','?'):<5} "
                f"@ ${float(p.get('entry_price') or 0):.2f}  ({float(p.get('entry_size_usd') or 0):,.0f} {ccy})"
            )
        for p in closed_today:
            pnl = float(p.get('pnl_pct') or 0)
            mark = "✅" if pnl >= 0 else "❌"
            reason = (p.get('exit_reason') or '')[:60]
            lines.append(
                f"  {mark} Closed {p.get('ticker','?'):<6} {pnl:+.1f}%  — {reason}"
            )

    lines += [
        f"━━━━━━━━━━━━━━━━━━━━━━",
        f"<i>{datetime.now().strftime('%Y-%m-%d %H:%M')} SGT</i>",
    ]
    return '\n'.join(lines)


def build_weekly_message(stats: dict) -> str:
    """
    Per-account weekly summary covering the most recently completed Mon–Sun
    SGT week (fired Sat morning SGT).
    """
    label  = stats.get("account", "?")
    ccy    = stats.get("currency", "USD")
    is_b   = "LEARNING" in label or label.strip().startswith("B")
    emoji  = "🟣" if is_b else "⚪"

    realized     = stats.get("realized_pnl_usd", 0.0)
    realized_pct = stats.get("realized_pnl_pct_of_account", 0.0)
    wr           = stats.get("win_rate_pct", 0.0)
    wins         = stats.get("win_count", 0)
    losses       = stats.get("loss_count", 0)
    opened       = stats.get("opened_count", 0)
    closed       = stats.get("closed_count", 0)
    eow_open     = stats.get("end_of_week_open", [])
    best         = stats.get("best_trade")
    worst        = stats.get("worst_trade")

    lines = [
        f"{emoji} <b>WEEKLY — {label}</b>",
        f"<b>Week:</b> {stats.get('week_start')} → {stats.get('week_end')}",
        f"━━━━━━━━━━━━━━━━━━━━━━",
        f"<b>Activity</b>",
        f"  Opened:   {opened}",
        f"  Closed:   {closed}  ({wins}W / {losses}L)",
        f"  Win rate: {wr:.1f}%" if closed else "  Win rate: —",
        f"",
        f"<b>Realized PnL (this week)</b>",
        f"  {_fmt_money(realized, ccy)}  ({realized_pct:+.2f}% of account)",
    ]

    if best and best is not worst:
        bp = best.get("pnl_pct", 0)
        bu = best.get("_pnl_usd", 0)
        lines.append(
            f"  ▲ Best:  {best.get('ticker')} {bp:+.1f}% "
            f"({_fmt_money(bu, ccy)}) — {(best.get('exit_reason') or '')[:60]}"
        )
    if worst and worst is not best:
        wp = worst.get("pnl_pct", 0)
        wu = worst.get("_pnl_usd", 0)
        lines.append(
            f"  ▼ Worst: {worst.get('ticker')} {wp:+.1f}% "
            f"({_fmt_money(wu, ccy)}) — {(worst.get('exit_reason') or '')[:60]}"
        )

    lines += [
        f"",
        f"<b>End-of-week open positions ({len(eow_open)})</b>",
    ]
    if eow_open:
        for p in eow_open:
            up       = p.get("unrealized_pct")
            up_str   = f"{up:+.1f}%" if up is not None else "N/A"
            held     = p.get("days_held")
            held_str = f" · day {held}" if held is not None else ""
            lines.append(
                f"  {p['ticker']:<6} {p.get('direction','?'):<5} "
                f"{up_str}{held_str}"
            )
    else:
        lines.append("  (none carried into next week)")

    lines += [
        f"━━━━━━━━━━━━━━━━━━━━━━",
        f"<i>{datetime.now().strftime('%Y-%m-%d %H:%M')} SGT</i>",
    ]
    return '\n'.join(lines)
