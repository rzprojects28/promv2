"""
Prometheus — Reporting (pure functions)

Computes the daily and weekly stats the user wants on Telegram:
  Daily : open trades, $ at risk (budgeted + live), unrealized PnL %.
  Weekly: trades opened/closed this week, realized PnL, win rate,
          best/worst trade, end-of-week open positions.

These functions are deliberately pure — no IBKR, no Telegram, no file IO.
Callers fetch prices and account values and pass them in. This module
is fully unit-testable.

All week/day boundaries are in Singapore time (SGT, UTC+8), matching
the trading desk and the docstrings already in the codebase.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, date
from typing import Iterable, Optional, Mapping, Sequence
from zoneinfo import ZoneInfo


SGT = ZoneInfo("Asia/Singapore")


# ────────────────────────────────────────────────────────────────────
# Time helpers
# ────────────────────────────────────────────────────────────────────

def now_sgt() -> datetime:
    return datetime.now(tz=SGT)


def today_sgt(now: Optional[datetime] = None) -> date:
    n = now or now_sgt()
    if n.tzinfo is None:
        n = n.replace(tzinfo=SGT)
    return n.astimezone(SGT).date()


def week_bounds_sgt(now: Optional[datetime] = None) -> tuple[date, date]:
    """
    Return (week_start_monday, week_end_sunday) for the reporting week.

    Trigger convention: weekly report fires Sat morning SGT (= after Fri US close).
    - On Sat or Sun, the US trading week (Mon–Fri) of the CURRENT calendar week
      has just ended → return Mon→Sun of THIS calendar week.
    - On Mon–Fri (mid-week, used for testing or backfills) → return the
      PREVIOUS Mon→Sun, i.e. the most recently fully-completed week.
    """
    today    = today_sgt(now)
    weekday  = today.weekday()        # Mon=0 .. Sun=6
    if weekday >= 5:                  # Sat or Sun
        week_start = today - timedelta(days=weekday)
    else:                             # Mon..Fri → prior week
        week_start = today - timedelta(days=weekday + 7)
    week_end = week_start + timedelta(days=6)
    return week_start, week_end


def parse_date(s: str) -> Optional[date]:
    """Parse YYYY-MM-DD or ISO datetime. Returns None on failure."""
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


# ────────────────────────────────────────────────────────────────────
# Numeric helpers
# ────────────────────────────────────────────────────────────────────

def safe_float(v, default: float = 0.0) -> float:
    try:
        f = float(v)
        if f != f:  # NaN
            return default
        return f
    except (TypeError, ValueError):
        return default


def position_pnl_usd(position: Mapping) -> float:
    """Realized $ PnL for a closed position, derived from stored pnl_pct."""
    return safe_float(position.get("pnl_pct")) / 100.0 * safe_float(position.get("entry_size_usd"))


# ────────────────────────────────────────────────────────────────────
# Risk
# ────────────────────────────────────────────────────────────────────

def position_budgeted_risk_usd(position: Mapping) -> tuple[float, bool]:
    """
    Returns (risk_usd, has_stop).

    - If risk_per_share is stored, return risk_per_share × entry_qty.
    - Otherwise, treat the full entry_size_usd as at risk (per user spec:
      unstopped positions are 100% at risk).
    """
    rps = position.get("risk_per_share")
    qty = int(safe_float(position.get("entry_qty"), 0))
    if rps is not None and qty:
        return safe_float(rps) * qty, True
    return safe_float(position.get("entry_size_usd")), False


def position_live_risk_usd(position: Mapping, current_price: Optional[float]) -> tuple[float, bool]:
    """
    Live (mark-to-market) $ at risk if the stop hits from the current price.

    - Long:   max(current - stop, 0) × qty
    - Short:  max(stop - current, 0) × qty
    - Unstopped or no price: fall back to full entry_size_usd (100% at risk).
    """
    stop = position.get("calculated_stop")
    qty = int(safe_float(position.get("entry_qty"), 0))
    direction = (position.get("direction") or "LONG").upper()
    if stop is None or current_price is None or not qty:
        return safe_float(position.get("entry_size_usd")), False
    stop_f = safe_float(stop)
    if direction == "SHORT":
        delta = max(stop_f - current_price, 0.0)
    else:
        delta = max(current_price - stop_f, 0.0)
    return delta * qty, True


# ────────────────────────────────────────────────────────────────────
# Unrealized PnL
# ────────────────────────────────────────────────────────────────────

def position_unrealized(position: Mapping, current_price: Optional[float]) -> dict:
    """
    Compute unrealized $ and % for a single open position.

    For stock positions, current_price is the share price.
    For options positions, the share price is meaningless for P&L
    (the combo's premium has its own price). We mark options positions as
    "unavailable" here so they don't garble the aggregate unrealized.
    Live combo pricing is a v2 feature.
    """
    instrument = (position.get("instrument") or "stock").lower()
    if instrument == "options":
        return {
            "ticker":         position.get("ticker"),
            "current_price":  current_price,
            "unrealized_usd": None,
            "unrealized_pct": None,
            "available":      False,
        }

    entry = safe_float(position.get("entry_price"))
    qty = int(safe_float(position.get("entry_qty"), 0))
    size_usd = safe_float(position.get("entry_size_usd"))
    direction = (position.get("direction") or "LONG").upper()

    if current_price is None or entry == 0:
        return {
            "ticker": position.get("ticker"),
            "current_price": current_price,
            "unrealized_usd": None,
            "unrealized_pct": None,
            "available": False,
        }

    pct = (current_price - entry) / entry * 100.0
    if direction == "SHORT":
        pct = -pct
    usd = pct / 100.0 * size_usd if size_usd else (current_price - entry) * qty * (-1 if direction == "SHORT" else 1)
    return {
        "ticker": position.get("ticker"),
        "current_price": current_price,
        "unrealized_usd": round(usd, 2),
        "unrealized_pct": round(pct, 2),
        "available": True,
    }


# ────────────────────────────────────────────────────────────────────
# Daily report
# ────────────────────────────────────────────────────────────────────

def compute_daily_stats(
    open_positions: Sequence[Mapping],
    current_prices: Mapping[str, Optional[float]],
    account_value: float,
    account_label: str,
    currency: str = "USD",
    now: Optional[datetime] = None,
) -> dict:
    """
    Build the dict consumed by send_daily_account_report.

    current_prices: {ticker: float-or-None}.  Caller is responsible for
                    fetching live prices (IBKR / yfinance / etc).
    account_value:  NetLiquidation in `currency` (the same units as
                    entry_size_usd — i.e. USD-denominated for these accounts).
    """
    n = now or now_sgt()
    enriched, total_unreal_usd, total_open_cost = [], 0.0, 0.0
    budgeted_risk, live_risk = 0.0, 0.0
    missing_stop = 0
    pct_weighted_sum = 0.0
    priced_count = 0

    for p in open_positions:
        ticker = p.get("ticker")
        cp = current_prices.get(ticker) if ticker else None
        unreal = position_unrealized(p, cp)
        b_risk, has_stop = position_budgeted_risk_usd(p)
        l_risk, _ = position_live_risk_usd(p, cp)

        size_usd = safe_float(p.get("entry_size_usd"))
        total_open_cost += size_usd
        budgeted_risk += b_risk
        live_risk += l_risk
        if not has_stop:
            missing_stop += 1

        if unreal["available"]:
            priced_count += 1
            total_unreal_usd += unreal["unrealized_usd"] or 0.0
            pct_weighted_sum += (unreal["unrealized_pct"] or 0.0) * size_usd

        enriched.append({
            "ticker":            ticker,
            "direction":         p.get("direction"),
            "conviction":        p.get("conviction"),
            "instrument":        (p.get("instrument") or "stock").lower(),
            "options_structure": p.get("options_structure"),
            "entry_price":       safe_float(p.get("entry_price")),
            "entry_qty":         int(safe_float(p.get("entry_qty"), 0)),
            "entry_size_usd":    size_usd,
            "current_price":     cp,
            "unrealized_usd":    unreal["unrealized_usd"],
            "unrealized_pct":    unreal["unrealized_pct"],
            "calculated_stop":   p.get("calculated_stop"),
            "has_stop":          has_stop,
            "budgeted_risk_usd": round(b_risk, 2),
            "live_risk_usd":     round(l_risk, 2),
            "available":         unreal["available"],
        })

    unreal_pct_of_account = (total_unreal_usd / account_value * 100.0) if account_value else 0.0
    # avg position % = size-weighted average of per-position unrealized %
    if total_open_cost > 0:
        avg_position_pct = pct_weighted_sum / total_open_cost
    else:
        avg_position_pct = 0.0

    return {
        "account":                       account_label,
        "currency":                      currency,
        "as_of":                         n.astimezone(SGT).isoformat(),
        "open_trades":                   len(open_positions),
        "priced_count":                  priced_count,
        "account_value":                 round(account_value, 2),
        "total_open_cost":               round(total_open_cost, 2),
        "total_unrealized_usd":          round(total_unreal_usd, 2),
        "unrealized_pct_of_account":     round(unreal_pct_of_account, 3),
        "unrealized_pct_avg_position":   round(avg_position_pct, 2),
        "budgeted_risk_usd":             round(budgeted_risk, 2),
        "live_risk_usd":                 round(live_risk, 2),
        "budgeted_risk_pct_of_account":  round(budgeted_risk / account_value * 100.0, 3) if account_value else 0.0,
        "live_risk_pct_of_account":      round(live_risk / account_value * 100.0, 3) if account_value else 0.0,
        "positions_missing_stop":        missing_stop,
        "positions":                     enriched,
    }


# ────────────────────────────────────────────────────────────────────
# Weekly report
# ────────────────────────────────────────────────────────────────────

def filter_by_date_range(
    rows: Iterable[Mapping],
    date_field: str,
    start: date,
    end: date,
) -> list[dict]:
    """Return rows where rows[date_field] (YYYY-MM-DD) is within [start, end] inclusive."""
    out = []
    for r in rows:
        d = parse_date(r.get(date_field, ""))
        if d is not None and start <= d <= end:
            out.append(dict(r))
    return out


def compute_weekly_stats(
    open_positions: Sequence[Mapping],
    closed_positions: Sequence[Mapping],
    current_prices: Mapping[str, Optional[float]],
    account_value: float,
    account_label: str,
    currency: str = "USD",
    now: Optional[datetime] = None,
) -> dict:
    """
    Build a per-account weekly summary for the most recently completed
    Mon→Sun SGT week.

    closed_this_week  = closed_positions with exit_date in week
    opened_this_week  = positions whose entry_date in week (across BOTH open and closed)
    realized_pnl_usd  = sum of pnl_pct/100 × entry_size_usd for closed_this_week
    end_of_week_open  = current open_positions with unrealized PnL %
    """
    week_start, week_end = week_bounds_sgt(now)

    # Opened this week — from both still-open and closed (closed-in-same-week count too)
    opened_open   = filter_by_date_range(open_positions,   "entry_date", week_start, week_end)
    opened_closed = filter_by_date_range(closed_positions, "entry_date", week_start, week_end)
    opened_this_week = opened_open + opened_closed

    closed_this_week = filter_by_date_range(closed_positions, "exit_date", week_start, week_end)

    # Realized PnL (USD-weighted, not sum-of-percents)
    for r in closed_this_week:
        r["_pnl_usd"] = round(position_pnl_usd(r), 2)
    realized_usd = round(sum(r["_pnl_usd"] for r in closed_this_week), 2)
    realized_pct_of_account = (realized_usd / account_value * 100.0) if account_value else 0.0

    # Win rate (this week only)
    wins   = [r for r in closed_this_week if safe_float(r.get("pnl_pct")) > 0]
    losses = [r for r in closed_this_week if safe_float(r.get("pnl_pct")) <= 0]
    win_rate = (len(wins) / len(closed_this_week) * 100.0) if closed_this_week else 0.0

    # Best / worst of the week (by pnl_pct)
    best  = max(closed_this_week, key=lambda r: safe_float(r.get("pnl_pct")), default=None)
    worst = min(closed_this_week, key=lambda r: safe_float(r.get("pnl_pct")), default=None)

    # End-of-week snapshot — current open with unrealized
    eow_open = []
    for p in open_positions:
        cp = current_prices.get(p.get("ticker")) if p.get("ticker") else None
        u = position_unrealized(p, cp)
        days_held = None
        d_entry = parse_date(p.get("entry_date", ""))
        if d_entry:
            days_held = (week_end - d_entry).days
        eow_open.append({
            "ticker":         p.get("ticker"),
            "direction":      p.get("direction"),
            "conviction":     p.get("conviction"),
            "entry_date":     p.get("entry_date"),
            "entry_price":    safe_float(p.get("entry_price")),
            "current_price":  cp,
            "days_held":      days_held,
            "unrealized_pct": u["unrealized_pct"],
            "unrealized_usd": u["unrealized_usd"],
            "available":      u["available"],
        })

    return {
        "account":                    account_label,
        "currency":                   currency,
        "week_start":                 week_start.isoformat(),
        "week_end":                   week_end.isoformat(),
        "opened_count":               len(opened_this_week),
        "closed_count":               len(closed_this_week),
        "opened_this_week":           opened_this_week,
        "closed_this_week":           closed_this_week,
        "realized_pnl_usd":           realized_usd,
        "realized_pnl_pct_of_account": round(realized_pct_of_account, 3),
        "win_count":                  len(wins),
        "loss_count":                 len(losses),
        "win_rate_pct":               round(win_rate, 1),
        "best_trade":                 best,
        "worst_trade":                worst,
        "open_count_eow":             len(open_positions),
        "end_of_week_open":           eow_open,
    }
