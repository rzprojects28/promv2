"""
Prometheus — Execution Agent (options-aware, v2)

Takes approved trade theses from Risk Manager and submits them to IBKR.

Two execution paths:
  - instrument="stock"   → equity Stock(...) + LimitOrder at mid
  - instrument="options" → parse options_structure, fetch live leg prices,
                            size contracts so total max-loss ≤ 0.5% NAV,
                            submit as a single Combo (Bag) for multi-leg
                            or a single Option order for singles.

Defined-risk only — naked shorts and other blocked structures are filtered
out upstream by the analysis_validator. If the option chain can't be
qualified, the option mids are stale, or sizing comes back as 0 contracts
(too expensive for the 0.5% budget), the trade is SKIPPED — no silent
degradation to the underlying stock.
"""
from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime, timedelta
from typing import Optional

from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.expanduser('~/prometheus/.env'))

# Make sibling modules importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import telegram_alerts as tg
import options_builder as ob

IB_HOST   = os.getenv('IB_HOST', '127.0.0.1')
IB_PORT   = int(os.getenv('IB_PORT', 4002))
IB_CLIENT = int(os.getenv('IB_CLIENT_EXEC', 3))


# ── Helpers ────────────────────────────────────────────────────────────────

def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path) if os.path.dirname(path) else '.', exist_ok=True)
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)


def _safe(v, default=0.0):
    try:
        f = float(v)
        return default if math.isnan(f) else f
    except (TypeError, ValueError):
        return default


def _get_account_value(ib) -> float:
    try:
        managed    = ib.managedAccounts()
        my_account = managed[0] if managed else None
        for av in ib.accountValues():
            if my_account and av.account != my_account:
                continue
            if av.tag == 'NetLiquidation' and av.currency and av.currency != 'BASE':
                return float(av.value)
    except Exception as e:
        print(f"    Account value error: {e}")
    return 100_000.0


def _get_stock_mid(ib, ticker: str):
    from ib_insync import Stock
    try:
        contract = Stock(ticker, 'SMART', 'USD')
        ib.qualifyContracts(contract)
        td = ib.reqMktData(contract, '', False, False)
        ib.sleep(2)
        bid, ask, last = td.bid or 0, td.ask or 0, td.last or td.close or 0
        mid = round((bid + ask) / 2, 2) if (bid > 0 and ask > 0) else last
        return mid, bid, ask, contract
    except Exception as e:
        print(f"    Stock price error for {ticker}: {e}")
        return 0, 0, 0, None


def _get_option_mid(ib, option_contract):
    """Return (mid, bid, ask) for a single qualified Option contract."""
    try:
        td = ib.reqMktData(option_contract, '', False, False)
        ib.sleep(2)
        bid = td.bid or 0
        ask = td.ask or 0
        if bid > 0 and ask > 0:
            return round((bid + ask) / 2, 4), bid, ask
        last = td.last or td.close or 0
        return last, bid, ask
    except Exception as e:
        print(f"    Option price error: {e}")
        return 0, 0, 0


# ── Stock execution path (legacy) ──────────────────────────────────────────

def _execute_stock(ib, thesis: dict, account_value: float) -> Optional[dict]:
    from ib_insync import Stock, LimitOrder

    ticker    = thesis.get('ticker', '')
    direction = thesis.get('direction', 'LONG')
    size_pct  = float(thesis.get('position_size_pct', 3.0))
    size_usd  = account_value * (size_pct / 100.0)

    print(f"  [STOCK] {ticker} {direction} ~${size_usd:,.0f} ({size_pct}%)")

    mid, bid, ask, contract = _get_stock_mid(ib, ticker)
    if not mid:
        print(f"    SKIP — no price for {ticker}")
        return None

    qty = max(1, int(size_usd / mid))
    action = 'BUY' if direction == 'LONG' else 'SELL'
    order = LimitOrder(action, qty, round(mid, 2))
    order.tif = 'DAY'

    trade = ib.placeOrder(contract, order)
    ib.sleep(2)

    position = {
        **thesis,
        'instrument':      'stock',
        'entry_date':      datetime.now().strftime('%Y-%m-%d'),
        'entry_time':      datetime.now().isoformat(),
        'entry_price':     round(mid, 2),
        'entry_qty':       qty,
        'entry_size_usd':  round(size_usd, 2),
        'order_id':        trade.order.orderId,
        'status':          'open',
        'paper_trade':     True,
        'deadline_date':   (datetime.now() + timedelta(days=45)).strftime('%Y-%m-%d'),
    }
    print(f"    OK {action} {qty} {ticker} @ ${mid:.2f}")
    tg.send_trade_opened_labeled(thesis, {'qty': qty, 'limit_price': mid,
                                           'status': trade.orderStatus.status},
                                  account_value, 'A — BASELINE')
    return position


# ── Options execution path ────────────────────────────────────────────────

MAX_RISK_PCT_PER_OPTIONS_TRADE = 0.5    # of NAV, in premium terms
MAX_NOTIONAL_DELTA_PCT         = 1.5    # of NAV, for long single options (leverage cap)


def _execute_options(ib, thesis: dict, account_value: float) -> Optional[dict]:
    """
    Parse options_structure, fetch leg mids, size, submit combo.

    Returns the position dict on success, None on any skip/failure.
    """
    from ib_insync import LimitOrder

    ticker = thesis.get('ticker', '')
    raw    = thesis.get('options_structure') or {}
    raw.setdefault('underlying', ticker)

    try:
        structure = ob.parse_options_structure(raw)
    except ValueError as e:
        print(f"  [OPTIONS] SKIP {ticker}: invalid structure: {e}")
        return None

    # Build option contracts and qualify
    from ib_insync import Option
    raw_contracts = []
    for leg in structure.legs:
        raw_contracts.append(Option(
            symbol=structure.underlying,
            lastTradeDateOrContractMonth=leg.to_ibkr_expiry(),
            strike=leg.strike,
            right=leg.right,
            exchange='SMART',
            currency='USD',
            tradingClass=structure.underlying,
        ))
    try:
        qualified = ib.qualifyContracts(*raw_contracts)
    except Exception as e:
        print(f"  [OPTIONS] SKIP {ticker}: qualify failed: {e}")
        return None
    if any(not q or not getattr(q, 'conId', None) for q in qualified):
        print(f"  [OPTIONS] SKIP {ticker}: one or more legs unavailable")
        return None

    # Fetch mid prices for each leg
    leg_prices = {}
    for q, leg in zip(qualified, structure.legs):
        mid, bid, ask = _get_option_mid(ib, q)
        if mid <= 0:
            print(f"  [OPTIONS] SKIP {ticker}: stale mid for {leg.right}{leg.strike}/{leg.expiry}")
            return None
        # Reject very wide markets (bid/ask spread > 25% of mid) — execution risk
        if bid > 0 and ask > 0:
            spread_pct = (ask - bid) / mid * 100
            if spread_pct > 25:
                print(f"  [OPTIONS] SKIP {ticker}: wide bid/ask {spread_pct:.0f}% on "
                      f"{leg.right}{leg.strike}/{leg.expiry}")
                return None
        leg_prices[(leg.action, leg.right, leg.strike, leg.expiry)] = mid

    # Compute max loss per contract, then size
    try:
        max_loss = ob.max_loss_per_contract_usd(structure, leg_prices)
    except ValueError as e:
        print(f"  [OPTIONS] SKIP {ticker}: max-loss calc failed: {e}")
        return None

    contracts = ob.position_size_contracts(max_loss, account_value,
                                           MAX_RISK_PCT_PER_OPTIONS_TRADE)
    if contracts <= 0:
        print(f"  [OPTIONS] SKIP {ticker}: max_loss/contract ${max_loss:.2f} > "
              f"${account_value * MAX_RISK_PCT_PER_OPTIONS_TRADE / 100:.2f} budget")
        return None

    # Compute net debit/credit per contract using actual leg signs
    net_debit = 0.0
    for leg in structure.legs:
        mid  = leg_prices[(leg.action, leg.right, leg.strike, leg.expiry)]
        sign = +1 if leg.action == 'BUY' else -1
        net_debit += sign * mid * leg.ratio
    net_debit_per_contract_usd = net_debit * 100

    # Submit order
    if structure.is_single_leg():
        # Single Option order
        leg = structure.legs[0]
        contract = qualified[0]
        action = leg.action
        # Limit at mid (or slightly aggressive)
        limit_price = round(leg_prices[(leg.action, leg.right, leg.strike, leg.expiry)], 2)
        order = LimitOrder(action, contracts * leg.ratio, limit_price)
        order.tif = 'DAY'
        trade = ib.placeOrder(contract, order)
    else:
        # Multi-leg combo (Bag) order
        from ib_insync import Bag, ComboLeg
        combo_legs = []
        for q, leg in zip(qualified, structure.legs):
            combo_legs.append(ComboLeg(
                conId=q.conId,
                ratio=leg.ratio,
                action=leg.action,
                exchange='SMART',
            ))
        bag = Bag(symbol=structure.underlying, exchange='SMART',
                  currency='USD', comboLegs=combo_legs)
        # Net debit positive → BUY action with positive limit
        # Net credit (negative debit) → SELL action; pass abs value as limit
        if net_debit >= 0:
            order = LimitOrder('BUY', contracts, round(net_debit, 2))
        else:
            order = LimitOrder('SELL', contracts, round(abs(net_debit), 2))
        order.tif = 'DAY'
        trade = ib.placeOrder(bag, order)

    ib.sleep(2)

    entry_size_usd = contracts * max_loss
    position = {
        **thesis,
        'instrument':       'options',
        'options_structure': raw,
        'entry_date':       datetime.now().strftime('%Y-%m-%d'),
        'entry_time':       datetime.now().isoformat(),
        'entry_price':      round(net_debit_per_contract_usd, 2),   # USD per contract
        'entry_qty':        contracts,
        'entry_size_usd':   round(entry_size_usd, 2),
        'max_loss_per_contract_usd': round(max_loss, 2),
        'leg_prices_at_entry': {f"{a}_{r}_{k}_{e}": v
                                 for (a, r, k, e), v in leg_prices.items()},
        'order_id':         trade.order.orderId,
        'status':           'open',
        'paper_trade':      True,
        'deadline_date':    (datetime.now() + timedelta(days=45)).strftime('%Y-%m-%d'),
    }
    print(f"    OK {structure.type} {ticker} × {contracts} contracts "
          f"(max loss ${entry_size_usd:.0f} = {entry_size_usd / account_value * 100:.2f}% NAV)")

    # Telegram notification (re-use existing send_trade_opened_labeled with adjusted dict)
    tg.send_trade_opened_labeled(
        {**thesis, 'position_size_pct': round(entry_size_usd / account_value * 100, 2)},
        {'qty': contracts, 'limit_price': round(net_debit_per_contract_usd, 2),
         'status': trade.orderStatus.status},
        account_value,
        'A — BASELINE',
    )
    return position


# ── Public entry point ─────────────────────────────────────────────────────

def execute_thesis(ib, thesis: dict, account_value: float) -> Optional[dict]:
    """Dispatch to the right execution path based on the thesis's instrument field."""
    instrument = (thesis.get('instrument') or 'stock').lower()
    if instrument == 'options':
        return _execute_options(ib, thesis, account_value)
    elif instrument == 'stock':
        return _execute_stock(ib, thesis, account_value)
    else:
        print(f"  SKIP {thesis.get('ticker')}: unknown instrument {instrument!r}")
        return None


def run():
    print("[Execution Agent v2] Loading approved trades...")

    data_dir   = os.environ.get('PROMETHEUS_DATA_DIR', 'data')
    approved_d = load_json(os.path.join(data_dir, 'approved_trades.json'), {})
    approved   = approved_d.get('trades', [])

    if not approved:
        print("[Execution Agent v2] No approved trades today.")
        return []

    # ── Market hours gate ──
    # Refuse to submit orders when US equity/options market is closed.
    # Research + risk validation can still run any time; only execution gates.
    from market_hours import is_us_market_open, minutes_until_open
    is_open, reason = is_us_market_open()
    if not is_open:
        mins = minutes_until_open()
        suffix = f" (opens in {mins} min)" if mins else ""
        msg = f"⏸ Execution skipped — market closed: {reason}{suffix}"
        print(f"[Execution Agent v2] {msg}")
        try:
            tg.send(msg)
        except Exception:
            pass
        return []

    print(f"  {len(approved)} approved trade(s) — {reason}")

    from ib_insync import IB
    ib = IB()
    try:
        ib.connect(IB_HOST, IB_PORT, clientId=IB_CLIENT)
        ib.reqMarketDataType(4)   # delayed (free tier; switch to 1 with subscription)
    except Exception as e:
        print(f"  IBKR connection failed: {e}")
        tg.send(f"PROMETHEUS ERROR — Could not connect to IBKR: {e}")
        return []

    account_value  = _get_account_value(ib)
    open_positions = load_json(os.path.join(data_dir, 'open_positions.json'), [])
    executed       = []

    for thesis in approved:
        print(f"\n  Processing {thesis.get('ticker')} "
              f"[{(thesis.get('instrument') or 'stock').upper()}]...")
        try:
            position = execute_thesis(ib, thesis, account_value)
        except Exception as e:
            print(f"    ERROR: {e}")
            import traceback; traceback.print_exc()
            position = None
        if position:
            open_positions.append(position)
            executed.append(position)

    ib.disconnect()
    save_json(os.path.join(data_dir, 'open_positions.json'), open_positions)
    print(f"\n[Execution Agent v2] Complete — {len(executed)}/{len(approved)} executed")
    return executed


if __name__ == '__main__':
    run()
