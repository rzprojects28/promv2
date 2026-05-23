"""
Prometheus — Account A Pipeline (Baseline)
Runs the full trading pipeline for the Account A paper account.
Connects to IB Gateway on port 4002.

Path layout (post-restructure):
  ACCOUNT_DIR  = trading/account_a/        (this file + config.json)
  TRADING_DIR  = trading/                  (research/, risk/, execution/, monitor/, journal/)
  BASE_DIR     = ~/prometheus              (repo root)
  DATA_DIR     = ~/prometheus/data/account_a/   (live JSON state)
"""
import json
import os
import sys
from datetime import datetime, date

# ── Paths ──────────────────────────────────────────────────────────────────
ACCOUNT_DIR  = os.path.dirname(os.path.abspath(__file__))
TRADING_DIR  = os.path.dirname(ACCOUNT_DIR)
BASE_DIR     = os.path.dirname(TRADING_DIR)
RESEARCH_DIR = os.path.join(TRADING_DIR, 'research')
RISK_DIR     = os.path.join(TRADING_DIR, 'risk')
EXEC_DIR     = os.path.join(TRADING_DIR, 'execution')
MONITOR_DIR  = os.path.join(TRADING_DIR, 'monitor')
JOURNAL_DIR  = os.path.join(TRADING_DIR, 'journal')
DATA_DIR     = os.path.join(BASE_DIR, 'data', 'account_a')

# Add every trading subfolder to sys.path so flat `import X` keeps working
# in the modules that were moved out of phase2/phase3.
for p in (TRADING_DIR, RESEARCH_DIR, RISK_DIR, EXEC_DIR, MONITOR_DIR, JOURNAL_DIR, ACCOUNT_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

# Override data directory to data/account_a/
os.environ['PROMETHEUS_DATA_DIR'] = DATA_DIR
os.environ['IB_PORT']             = '4002'
os.environ['IB_CLIENT_EXEC']      = '3'
os.environ['IB_CLIENT_MONITOR']   = '4'
os.environ['ACCOUNT_LABEL']       = 'BASELINE'
os.environ['LEARNING_MODE']       = 'no_learning'

from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.expanduser('~/prometheus/.env'))


def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def should_run_journal(config_path):
    config   = load_json(config_path, {})
    last_run = config.get('journal', {}).get('last_run')
    if not last_run:
        return True
    try:
        last     = datetime.strptime(last_run, '%Y-%m-%d').date()
        days_due = config.get('journal', {}).get('runs_every_n_days', 7)
        return (date.today() - last).days >= days_due
    except Exception:
        return True


def run():
    import telegram_alerts as tg

    data_dir    = os.environ['PROMETHEUS_DATA_DIR']
    config_path = os.path.join(ACCOUNT_DIR, 'config.json')
    os.makedirs(data_dir, exist_ok=True)

    start = datetime.now()
    print("\n" + "─" * 50)
    print(f"  ACCOUNT A — BASELINE — {start.strftime('%H:%M')}")
    print("─" * 50)

    # ── Risk Manager ──────────────────────────────────────────
    print("[A-1/4] Risk Manager...")
    risk_result = None
    try:
        from risk_manager import validate_thesis, load_json as rj_load
        import json as _json

        # Research outputs live next to the research code.
        theses_data = load_json(os.path.join(RESEARCH_DIR, 'data/trade_theses.json'), {})
        theses         = theses_data.get('theses', [])
        open_positions = load_json(os.path.join(data_dir, 'open_positions.json'), [])

        approved, rejected = [], []
        for thesis in theses:
            thesis['learning_mode'] = 'no_learning'
            passed, messages = validate_thesis(thesis, open_positions)
            result = {**thesis, 'risk_check_time': datetime.now().isoformat(),
                      'risk_checks': messages, 'approved': passed}
            (approved if passed else rejected).append(result)
            print(f"  {'✓' if passed else '✗'} {thesis.get('ticker')} {thesis.get('direction')}")

        os.makedirs(data_dir, exist_ok=True)
        with open(os.path.join(data_dir, 'approved_trades.json'), 'w') as f:
            _json.dump({'generated_at': datetime.now().isoformat(), 'trades': approved}, f, indent=2)
        with open(os.path.join(data_dir, 'rejected_trades.json'), 'w') as f:
            _json.dump({'generated_at': datetime.now().isoformat(), 'trades': rejected}, f, indent=2)

        risk_result = {'approved': approved, 'rejected': rejected}
        print(f"  Approved: {len(approved)} | Rejected: {len(rejected)}")
        tg.send_risk_summary(approved, rejected, 'A — BASELINE')
    except Exception as e:
        print(f"  Risk Manager failed: {e}")

    # ── Execution ─────────────────────────────────────────────
    # Delegated to trading.execution.execution_agent so the options-vs-stock
    # dispatch lives in one place. The agent reads PROMETHEUS_DATA_DIR for
    # the account's data folder and respects IB_PORT / IB_CLIENT_EXEC.
    executed = []
    approved_count = len((risk_result or {}).get('approved', []))
    if approved_count > 0:
        print(f"[A-2/4] Executing {approved_count} trade(s) via execution_agent...")
        try:
            sys.path.insert(0, EXEC_DIR)
            from execution_agent import run as execute_run
            executed = execute_run() or []
        except Exception as e:
            print(f"  Execution failed: {e}")
            import traceback; traceback.print_exc()
    else:
        print("[A-2/4] No approved trades.")

    # ── Monitor ───────────────────────────────────────────────
    print("[A-3/4] Monitor Agent...")
    monitor_result = {'still_open': [], 'closed': []}
    try:
        import monitor_agent
        # Temporarily redirect data paths
        _orig_open   = 'data/open_positions.json'
        _orig_closed = 'data/closed_positions.json'
        monitor_result = _run_monitor(data_dir) or monitor_result
    except Exception as e:
        print(f"  Monitor failed: {e}")

    # ── Journal (weekly) ──────────────────────────────────────
    if should_run_journal(config_path):
        print("[A-4/4] Journal Agent (weekly)...")
        try:
            import journal_agent
            # Run journal against account_a data
            _run_journal(data_dir, config_path, 'no_learning')
        except Exception as e:
            print(f"  Journal failed: {e}")
    else:
        print("[A-4/4] Journal not due yet.")

    elapsed = (datetime.now() - start).seconds
    print(f"  Account A complete — {elapsed}s | "
          f"{len(executed)} executed | "
          f"{len(monitor_result.get('still_open',[]))} open")

    return {
        'executed':      executed,
        'monitor':       monitor_result,
        'approved':      approved_count,
        'theses_count':  len(load_json(os.path.join(RESEARCH_DIR, 'data/trade_theses.json'), {}).get('theses', [])),
    }


def _run_monitor(data_dir):
    """Run monitor agent against account-specific data directory"""
    import json, math
    from ib_insync import IB, Stock, LimitOrder
    import telegram_alerts as tg
    import anthropic
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=os.path.expanduser('~/prometheus/.env'))

    open_positions   = load_json(os.path.join(data_dir, 'open_positions.json'), [])
    closed_positions = load_json(os.path.join(data_dir, 'closed_positions.json'), [])

    # ── Market hours gate — see trading/monitor/monitor_agent.py for rationale.
    sys.path.insert(0, EXEC_DIR)
    from market_hours import is_us_market_open, minutes_until_open
    is_open, mh_reason = is_us_market_open()
    if not is_open:
        mins = minutes_until_open()
        suffix = f" (opens in {mins} min)" if mins else ""
        print(f"  [_run_monitor] ⏸ market closed: {mh_reason}{suffix}")
        return {'still_open': open_positions, 'closed': []}

    if not open_positions:
        return {'still_open': [], 'closed': []}

    claude = anthropic.Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))
    ib = IB()
    try:
        import time as _time
        for attempt in range(3):
            try:
                ib.connect('127.0.0.1', int(os.environ['IB_PORT']),
                           clientId=int(os.environ['IB_CLIENT_MONITOR']))
                break
            except Exception as ce:
                if attempt < 2:
                    print(f"  Monitor connect attempt {attempt+1} failed, retrying in 5s...")
                    _time.sleep(5)
                else:
                    raise ce
        ib.reqMarketDataType(4)  # Use delayed data (free tier)
    except Exception as e:
        print(f"  Monitor IBKR connect failed: {e}")
        return {'still_open': open_positions, 'closed': []}

    still_open, newly_closed = [], []
    for position in open_positions:
        ticker      = position.get('ticker', '')
        entry_price = float(position.get('entry_price', 0))
        try:
            contract = Stock(ticker, 'SMART', 'USD')
            ib.qualifyContracts(contract)
            td = ib.reqMktData(contract, '', False, False)
            ib.sleep(2)
            current = None
            for attr in ['last', 'close', 'bid']:
                v = getattr(td, attr, None)
                if v and not math.isnan(v) and v > 0:
                    current = float(v)
                    break
        except Exception:
            current = None

        if not current:
            still_open.append(position)
            continue

        pnl = ((current - entry_price) / entry_price * 100) if entry_price else 0
        if position.get('direction') == 'SHORT':
            pnl = -pnl

        exit_reason = None
        deadline = position.get('deadline_date', '')
        if deadline:
            try:
                if datetime.now() > datetime.strptime(deadline[:10], '%Y-%m-%d'):
                    exit_reason = f"Hard time limit reached ({deadline})"
            except Exception:
                pass

        if not exit_reason:
            try:
                system_msg = (
                    "You are the Monitor Agent for Prometheus. Evaluate ONLY price-level "
                    "invalidation conditions against the provided current price. Do NOT use "
                    "training memory, news intuition, or any data not in the prompt. "
                    "Narrative-only or ambiguous conditions → return false. Bias is to HOLD."
                )
                prompt = f"""Invalidation check.
Ticker: {ticker} | Direction: {position.get('direction')} | P&L: {pnl:+.1f}%
Invalidation conditions (verbatim): {position.get('invalidation_conditions','')}
Current price: ${current:.2f} | Entry: ${entry_price}

Has a PRICE-LEVEL invalidation been clearly breached by the current price?
- Only trigger on numeric price thresholds that have crossed.
- Narrative-only conditions → false.
- Ambiguous → false.

JSON only: {{"invalidation_triggered":true/false,"condition_triggered":"..or null","recommended_action":"HOLD or EXIT"}}"""
                msg = claude.messages.create(
                    model='claude-opus-4-7',
                    max_tokens=200,
                    system=system_msg,
                    messages=[{'role':'user','content':prompt}],
                )
                result = json.loads(msg.content[0].text.strip())
                if result.get('invalidation_triggered') and result.get('recommended_action') == 'EXIT':
                    exit_reason = result.get('condition_triggered', 'Invalidation triggered')
                    tg.send_invalidation(position, exit_reason)
            except Exception:
                pass

        if exit_reason:
            try:
                close_action = 'SELL' if position.get('direction') == 'LONG' else 'BUY'
                limit = round(current * (0.998 if close_action == 'SELL' else 1.002), 2)
                contract = Stock(ticker, 'SMART', 'USD')
                ib.qualifyContracts(contract)
                ib.placeOrder(contract, LimitOrder(close_action, position.get('entry_qty',1), limit))
                ib.sleep(2)
            except Exception:
                pass
            closed = {**position, 'exit_date': datetime.now().strftime('%Y-%m-%d'),
                      'exit_time': datetime.now().isoformat(), 'exit_price': current,
                      'exit_reason': exit_reason, 'pnl_pct': round(pnl, 2), 'status': 'closed'}
            closed_positions.append(closed)
            newly_closed.append(closed)
            tg.send_trade_closed(position, exit_reason, pnl)
        else:
            still_open.append(position)

    ib.disconnect()
    import json as _j
    with open(os.path.join(data_dir, 'open_positions.json'), 'w') as f:
        _j.dump(still_open, f, indent=2)
    with open(os.path.join(data_dir, 'closed_positions.json'), 'w') as f:
        _j.dump(closed_positions, f, indent=2)
    return {'still_open': still_open, 'closed': newly_closed}


def _run_journal(data_dir, config_path, learning_mode):
    """Run journal agent against account-specific data"""
    import json, anthropic
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=os.path.expanduser('~/prometheus/.env'))
    import telegram_alerts as tg

    closed     = load_json(os.path.join(data_dir, 'closed_positions.json'), [])
    journal    = load_json(os.path.join(data_dir, 'trade_journal.json'), [])
    config     = load_json(config_path, {})
    reviewed   = {e.get('trade_id') for e in journal}
    new_trades = [t for t in closed if f"{t.get('ticker')}_{t.get('entry_date')}" not in reviewed]

    if not new_trades:
        print(f"  All trades reviewed ({len(journal)} total)")
        return

    # Delegate to the canonical journal review (single source of truth, single
    # prompt with the anti-hallucination system message).
    from journal_agent import review_trade_with_claude

    for trade in new_trades:
        trade_id = f"{trade.get('ticker')}_{trade.get('entry_date')}"
        try:
            review = review_trade_with_claude(trade)
        except Exception as e:
            review = {'verdict':'NEUTRAL','key_lesson':str(e),'pattern_tags':[],'repeat_this_setup':False}

        journal.append({
            'trade_id': trade_id, 'reviewed_at': datetime.now().isoformat(),
            'ticker': trade.get('ticker'), 'direction': trade.get('direction'),
            'conviction': trade.get('conviction'), 'sector': trade.get('sector'),
            'entry_date': trade.get('entry_date'), 'exit_date': trade.get('exit_date'),
            'pnl_pct': float(trade.get('pnl_pct', 0)),
            'exit_reason': trade.get('exit_reason'),
            'learning_mode': learning_mode, 'review': review,
        })
        print(f"  Reviewed {trade.get('ticker')}: {review.get('verdict')}")

    import json as _j
    with open(os.path.join(data_dir, 'trade_journal.json'), 'w') as f:
        _j.dump(journal, f, indent=2)
    config['journal']['last_run'] = datetime.now().strftime('%Y-%m-%d')
    with open(config_path, 'w') as f:
        _j.dump(config, f, indent=2)
    print(f"  Journal saved — {len(journal)} total reviews")


if __name__ == '__main__':
    run()
