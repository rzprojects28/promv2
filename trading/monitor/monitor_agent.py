"""
Prometheus — Monitor Agent (v3)
Runs daily. Checks every open position against its pre-committed thesis.

Can be run standalone via environment variable:
  PROMETHEUS_DATA_DIR=/root/prometheus/data/account_a IB_PORT=4002 python3 monitor_agent.py

Or called from trading/account_a/run.py via _run_monitor(data_dir).

Exit triggers (in order of priority):
  1. Hard time limit reached
  2. 21 DTE options management
  3. Calculated stop price breached (price-level rule, not AI judgment)
  4. Catalyst fired + significant profit (AI judgment)
  5. Invalidation condition triggered (AI judgment)
"""
import json
import os
import sys
import math
from datetime import datetime
import anthropic
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.expanduser('~/prometheus/.env'))
# Allow `import telegram_alerts` (one level up in trading/) to keep working
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import telegram_alerts as tg
from ib_insync import IB, Stock, LimitOrder

# ── Config from environment ────────────────────────────────────────────────
IB_HOST      = os.getenv('IB_HOST', '127.0.0.1')
IB_PORT      = int(os.getenv('IB_PORT', 4002))
IB_CLIENT_ID = int(os.getenv('IB_CLIENT_MONITOR', 4))

# Data directory — override via PROMETHEUS_DATA_DIR env var.
# Default for standalone runs points at the new data/account_a location.
BASE_DIR     = os.path.expanduser('~/prometheus')
DEFAULT_DATA = os.getenv(
    'PROMETHEUS_DATA_DIR',
    os.path.join(BASE_DIR, 'data', 'account_a'),
)

claude = anthropic.Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))

CATALYST_EXIT_MIN_GAIN_PCT    = 8.0
CATALYST_EXIT_STRONG_GAIN_PCT = 20.0


def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)


def get_current_price(ib, ticker):
    try:
        contract = Stock(ticker, 'SMART', 'USD')
        ib.qualifyContracts(contract)
        tdata = ib.reqMktData(contract, '', False, False)
        ib.sleep(2)
        for attr in ['last', 'close', 'bid']:
            val = getattr(tdata, attr, None)
            if val and not math.isnan(val) and val > 0:
                return float(val)
    except Exception as e:
        print(f"    Price error for {ticker}: {e}")
    return None


def check_hard_deadline(position):
    deadline = position.get('deadline_date', '')
    if not deadline:
        return False, None
    try:
        if datetime.now() > datetime.strptime(deadline[:10], '%Y-%m-%d'):
            days_over = (datetime.now() - datetime.strptime(deadline[:10], '%Y-%m-%d')).days
            return True, f"Hard time limit reached ({deadline}) — {days_over} day(s) over deadline"
    except Exception:
        pass
    return False, None


def check_21dte(position):
    if position.get('instrument') != 'options':
        return False, None
    try:
        entry     = datetime.strptime(position.get('entry_date', ''), '%Y-%m-%d')
        days_held = (datetime.now() - entry).days
        if days_held >= 24:
            return True, f"21 DTE management exit — {days_held} days held, theta decay accelerating"
    except Exception:
        pass
    return False, None


def check_stop_price(position, current_price):
    stop      = position.get('calculated_stop')
    direction = position.get('direction', 'LONG')
    if not stop:
        return False, None
    stop = float(stop)
    ticker = position.get('ticker', '')
    if direction == 'LONG' and current_price <= stop:
        return True, f"Stop price breached: {ticker} at ${current_price:.2f}, stop was ${stop:.2f}"
    if direction == 'SHORT' and current_price >= stop:
        return True, f"Stop price breached: {ticker} at ${current_price:.2f}, stop was ${stop:.2f} (short)"
    return False, None


def check_catalyst_exit(position, current_price, pnl_pct):
    """
    Profit-taking gate. Rule-based only (no AI catalyst-fired judgement —
    the agent had no real-world data to evaluate that and would guess from
    PnL alone). Exit when gain >= CATALYST_EXIT_STRONG_GAIN_PCT.
    """
    if pnl_pct >= CATALYST_EXIT_STRONG_GAIN_PCT:
        return True, (f"Strong catalyst payoff: position up {pnl_pct:.1f}% — "
                      f"taking profit at >={CATALYST_EXIT_STRONG_GAIN_PCT}% threshold")
    return False, None


def check_invalidation(position, current_price):
    """
    Ask the AI to evaluate whether a price-level invalidation has been
    breached. Limited to facts the AI can verify from the given data
    (current price vs stated levels). The system prompt forbids inference
    from training memory; ambiguous cases must return UNKNOWN/HOLD.
    """
    ticker      = position.get('ticker', '')
    direction   = position.get('direction', '')
    entry_price = float(position.get('entry_price', 0))
    conditions  = position.get('invalidation_conditions', '')
    pnl_pct     = ((current_price - entry_price) / entry_price * 100) if entry_price else 0
    if direction == 'SHORT':
        pnl_pct = -pnl_pct
    try:
        days_held = (datetime.now() - datetime.strptime(
            position.get('entry_date', '2026-01-01'), '%Y-%m-%d')).days
    except Exception:
        days_held = 0
    try:
        msg = claude.messages.create(
            model='claude-opus-4-7',
            max_tokens=200,
            system=(
                "You are the Monitor Agent for Prometheus. Evaluate ONLY price-level invalidation "
                "conditions against the provided current price. Do NOT use training memory, news "
                "intuition, or any data not in the prompt. If the condition is narrative (no specific "
                "price), or the data is ambiguous, return invalidation_triggered=false and "
                "recommended_action=HOLD. Bias is to HOLD unless a clearly breached price level is present."
            ),
            messages=[{'role': 'user', 'content': f"""Invalidation check.

{ticker} {direction} | Entry ${entry_price} | Current ${current_price:.2f} | P&L {pnl_pct:+.1f}% | {days_held} days held

Invalidation conditions (verbatim):
{conditions}

Has a PRICE-LEVEL invalidation condition been clearly breached by the current price?
- Only mark triggered if a numeric price threshold in the conditions has been crossed.
- Narrative-only conditions ("if sector reverses", "if catalyst weakens") → return false. You do not have sector or catalyst data here.
- Ambiguous → return false.

JSON only: {{"invalidation_triggered": true/false, "condition_triggered": "<exact text or null>", "recommended_action": "HOLD or EXIT"}}"""}]
        )
        result = json.loads(msg.content[0].text.strip())
        if result.get('invalidation_triggered') and result.get('recommended_action') == 'EXIT':
            cond = result.get('condition_triggered', 'Invalidation triggered')
            tg.send_invalidation(position, cond)
            return True, cond
    except Exception as e:
        print(f"    Invalidation check error: {e}")
    return False, None


def close_position(ib, position, exit_price):
    """
    Dispatch close to the right path based on instrument type.
    For options positions, reverses the combo (each leg's action flipped)
    and submits as a single market-ish order to flatten.
    """
    instrument = (position.get('instrument') or 'stock').lower()
    if instrument == 'options':
        return _close_options_position(ib, position)
    return _close_stock_position(ib, position, exit_price)


def _close_stock_position(ib, position, exit_price):
    ticker    = position.get('ticker', '')
    direction = position.get('direction', '')
    qty       = position.get('entry_qty', 1)
    try:
        contract     = Stock(ticker, 'SMART', 'USD')
        ib.qualifyContracts(contract)
        close_action = 'SELL' if direction == 'LONG' else 'BUY'
        limit_price  = round(exit_price * (0.998 if close_action == 'SELL' else 1.002), 2)
        order        = LimitOrder(close_action, qty, limit_price)
        order.tif    = 'DAY'
        ib.placeOrder(contract, order)
        ib.sleep(2)
        print(f"    Closing order: {close_action} {qty} {ticker} @ ${limit_price}")
        return limit_price
    except Exception as e:
        print(f"    Close order error: {e}")
        return exit_price


def _close_options_position(ib, position):
    """
    Close an options structure by reversing every leg.

    For singles: places a single Option order with action flipped.
    For combos: rebuilds the Bag with each ComboLeg's action flipped and
    submits a single combo order at a wide limit (mid +/- 0.05) to flatten.

    Returns the limit price submitted (the net debit/credit per contract,
    in USD per share — multiply by 100 for per-contract).
    """
    import sys as _sys, os as _os
    _exec_dir = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), 'execution')
    if _exec_dir not in _sys.path:
        _sys.path.insert(0, _exec_dir)
    import options_builder as ob
    from ib_insync import Option, Bag, ComboLeg, LimitOrder

    raw       = position.get('options_structure') or {}
    raw.setdefault('underlying', position.get('ticker', ''))
    contracts = int(position.get('entry_qty', 1))
    ticker    = position.get('ticker', '')

    try:
        structure = ob.parse_options_structure(raw)
    except ValueError as e:
        print(f"    Close error — can't parse stored options_structure: {e}")
        return None

    # Build + qualify option contracts
    raw_options = []
    for leg in structure.legs:
        raw_options.append(Option(
            symbol=structure.underlying,
            lastTradeDateOrContractMonth=leg.to_ibkr_expiry(),
            strike=leg.strike,
            right=leg.right,
            exchange='SMART',
            currency='USD',
            tradingClass=structure.underlying,
        ))
    try:
        qualified = ib.qualifyContracts(*raw_options)
    except Exception as e:
        print(f"    Close error — qualify failed: {e}")
        return None
    if any(not q or not getattr(q, 'conId', None) for q in qualified):
        print(f"    Close error — one or more legs missing")
        return None

    # Flip action for close
    def flip(action: str) -> str:
        return 'SELL' if action == 'BUY' else 'BUY'

    try:
        if structure.is_single_leg():
            leg     = structure.legs[0]
            close_action = flip(leg.action)
            # Market-style close: use a passive limit that should fill quickly
            order = LimitOrder(close_action, contracts * leg.ratio, 0.01)
            order.tif = 'DAY'
            order.outsideRth = False
            ib.placeOrder(qualified[0], order)
            ib.sleep(2)
            print(f"    Closing OPTIONS [single] {close_action} {contracts}×{ticker} "
                  f"{leg.right}{leg.strike}/{leg.expiry}")
            return 0.01

        # Multi-leg combo
        combo_legs = []
        for q, leg in zip(qualified, structure.legs):
            combo_legs.append(ComboLeg(
                conId=q.conId,
                ratio=leg.ratio,
                action=flip(leg.action),
                exchange='SMART',
            ))
        bag = Bag(symbol=structure.underlying, exchange='SMART',
                  currency='USD', comboLegs=combo_legs)
        # When flipped: a position opened as net debit closes as net credit, and vice versa.
        # Use a very passive limit (0.01) to ensure fill — combo MKT is unreliable.
        order = LimitOrder('SELL' if structure.net_action() == 'debit' else 'BUY',
                            contracts, 0.01)
        order.tif = 'DAY'
        ib.placeOrder(bag, order)
        ib.sleep(2)
        print(f"    Closing OPTIONS [combo] {structure.type} × {contracts} {ticker}")
        return 0.01
    except Exception as e:
        print(f"    Close order error: {e}")
        return None


def run(data_dir=None, ib_port=None, ib_client_id=None):
    """
    Main monitor run function.
    Parameters can be passed directly (from account runners)
    or read from environment variables (when run standalone).
    """
    data_dir      = data_dir      or DEFAULT_DATA
    ib_port       = ib_port       or IB_PORT
    ib_client_id  = ib_client_id  or IB_CLIENT_ID

    open_path   = os.path.join(data_dir, 'open_positions.json')
    closed_path = os.path.join(data_dir, 'closed_positions.json')

    print(f"[Monitor Agent v2] Checking: {data_dir}")

    open_positions   = load_json(open_path, [])
    closed_positions = load_json(closed_path, [])

    if not open_positions:
        print("[Monitor Agent v2] No open positions.")
        return {'still_open': [], 'closed': []}

    print(f"  {len(open_positions)} open position(s) | Port: {ib_port}")

    ib = IB()
    try:
        ib.connect(IB_HOST, ib_port, clientId=ib_client_id)
        ib.reqMarketDataType(3)  # Delayed data
        print(f"  Connected. Account: {ib.managedAccounts()}")
    except Exception as e:
        print(f"  IBKR connection failed: {e}")
        tg.send(f"MONITOR ERROR — Could not connect to IBKR port {ib_port}.\n{e}")
        return {'still_open': open_positions, 'closed': []}

    still_open, newly_closed = [], []

    for position in open_positions:
        ticker      = position.get('ticker', '')
        direction   = position.get('direction', '')
        entry_price = float(position.get('entry_price', 0))
        account     = position.get('account', 'UNKNOWN')

        print(f"\n  [{account}] {ticker} {direction} — entered {position.get('entry_date')}")

        current_price = get_current_price(ib, ticker)
        if not current_price:
            print(f"    No price — holding")
            still_open.append(position)
            continue

        pnl_pct = ((current_price - entry_price) / entry_price * 100) if entry_price else 0
        if direction == 'SHORT':
            pnl_pct = -pnl_pct

        print(f"    ${current_price:.2f} | Entry ${entry_price} | P&L {pnl_pct:+.1f}%")
        if position.get('calculated_stop'):
            stop      = float(position['calculated_stop'])
            dist      = abs(current_price - stop)
            dist_pct  = dist / current_price * 100
            print(f"    Stop: ${stop:.2f} | Distance: ${dist:.2f} ({dist_pct:.1f}%)")

        exit_reason, exit_category = None, None

        # 1. Hard deadline
        triggered, reason = check_hard_deadline(position)
        if triggered:
            exit_reason, exit_category = reason, 'time_limit'

        # 2. 21 DTE
        if not exit_reason:
            triggered, reason = check_21dte(position)
            if triggered:
                exit_reason, exit_category = reason, '21dte'

        # 3. Rule-based stop
        if not exit_reason:
            triggered, reason = check_stop_price(position, current_price)
            if triggered:
                exit_reason, exit_category = reason, 'stop_price'

        # 4. Profit-take threshold (rule-based, no AI judgment)
        if not exit_reason:
            triggered, reason = check_catalyst_exit(position, current_price, pnl_pct)
            if triggered:
                exit_reason, exit_category = reason, 'catalyst_exit'

        # 5. Invalidation (AI judgment)
        if not exit_reason:
            triggered, reason = check_invalidation(position, current_price)
            if triggered:
                exit_reason, exit_category = reason, 'invalidation'

        if exit_reason:
            print(f"    EXIT [{exit_category}]: {exit_reason[:100]}")
            actual_exit = close_position(ib, position, current_price)
            closed = {
                **position,
                'exit_date':     datetime.now().strftime('%Y-%m-%d'),
                'exit_time':     datetime.now().isoformat(),
                'exit_price':    actual_exit,
                'exit_reason':   exit_reason,
                'exit_category': exit_category,
                'pnl_pct':       round(pnl_pct, 2),
                'status':        'closed',
            }
            closed_positions.append(closed)
            newly_closed.append(closed)
            tg.send_trade_closed(position, exit_reason, pnl_pct)
        else:
            print(f"    HOLDING")
            still_open.append(position)

    ib.disconnect()

    save_json(open_path, still_open)
    save_json(closed_path, closed_positions)

    print(f"\n[Monitor Agent v2] Complete")
    print(f"  Still open: {len(still_open)} | Closed today: {len(newly_closed)}")

    return {'still_open': still_open, 'closed': newly_closed}


if __name__ == '__main__':
    # Standalone: monitor Account A (the only account post-B removal).
    run(
        data_dir=os.path.join(BASE_DIR, 'data', 'account_a'),
        ib_port=4002,
        ib_client_id=4,
    )
