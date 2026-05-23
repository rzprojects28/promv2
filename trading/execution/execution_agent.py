"""
Prometheus Phase 3 — Execution Agent (Autonomous)
Takes approved trade theses from Risk Manager and executes them automatically.
Sends full reasoning to Telegram after each trade. No human approval gate.
The Risk Manager is the safety net — if it passes risk checks, it executes.
"""
import json
import os
import sys
from datetime import datetime, timedelta
from dotenv import load_dotenv
 
load_dotenv(dotenv_path=os.path.expanduser('~/prometheus/.env'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
 
from ib_insync import IB, Stock, LimitOrder
import telegram_alerts as tg
 
IB_HOST      = os.getenv('IB_HOST', '127.0.0.1')
IB_PORT      = int(os.getenv('IB_PORT', 4002))
IB_CLIENT_ID = 3
 
 
def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default
 
 
def save_json(path, data):
    os.makedirs(os.path.dirname(path) if os.path.dirname(path) else '.', exist_ok=True)
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)
 
 
def get_stock_price(ib, ticker):
    try:
        contract = Stock(ticker, 'SMART', 'USD')
        ib.qualifyContracts(contract)
        ticker_data = ib.reqMktData(contract, '', False, False)
        ib.sleep(2)
        bid = ticker_data.bid or 0
        ask = ticker_data.ask or 0
        if bid > 0 and ask > 0:
            return round((bid + ask) / 2, 2), bid, ask
        last = ticker_data.last or ticker_data.close or 0
        return last, 0, 0
    except Exception as e:
        print(f"    Price fetch error for {ticker}: {e}")
        return None, None, None
 
 
def get_account_value(ib):
    try:
        for av in ib.accountValues():
            if av.tag == 'NetLiquidation' and av.currency == 'USD':
                return float(av.value)
    except Exception as e:
        print(f"    Account value error: {e}")
    return 100_000
 
 
def place_stock_order(ib, ticker, direction, size_usd):
    try:
        contract = Stock(ticker, 'SMART', 'USD')
        ib.qualifyContracts(contract)
 
        mid, bid, ask = get_stock_price(ib, ticker)
        if not mid:
            return None, f"Could not get price for {ticker}"
 
        limit_price = round(mid, 2)
        qty = max(1, int(size_usd / limit_price))
        action = 'BUY' if direction == 'LONG' else 'SELL'
 
        order = LimitOrder(action, qty, limit_price)
        order.tif = 'DAY'
 
        trade = ib.placeOrder(contract, order)
        ib.sleep(2)
 
        return {
            'order_id':    trade.order.orderId,
            'ticker':      ticker,
            'action':      action,
            'qty':         qty,
            'limit_price': limit_price,
            'bid':         bid,
            'ask':         ask,
            'status':      trade.orderStatus.status,
        }, None
 
    except Exception as e:
        return None, str(e)
 
 
def execute_thesis(ib, thesis, account_value):
    ticker    = thesis.get('ticker', '')
    direction = thesis.get('direction', 'LONG')
    size_pct  = float(thesis.get('position_size_pct', 3.0))
    size_usd  = account_value * (size_pct / 100)
 
    print(f"  Executing: {ticker} {direction} | ${size_usd:,.0f} ({size_pct}%)")
 
    order_result, error = place_stock_order(ib, ticker, direction, size_usd)
 
    if error:
        print(f"    ERROR: {error}")
        tg.send(
            f"EXECUTION FAILED\n"
            f"{ticker} {direction}\n"
            f"Error: {error}"
        )
        return None
 
    print(f"    OK: {order_result['action']} {order_result['qty']} {ticker} "
          f"@ ${order_result['limit_price']} [{order_result['status']}]")
 
    position = {
        **thesis,
        'entry_date':     datetime.now().strftime('%Y-%m-%d'),
        'entry_time':     datetime.now().isoformat(),
        'entry_price':    order_result['limit_price'],
        'entry_qty':      order_result['qty'],
        'entry_size_usd': round(size_usd, 2),
        'order_id':       order_result['order_id'],
        'status':         'open',
        'paper_trade':    True,
        'deadline_date':  (datetime.now() + timedelta(days=45)).strftime('%Y-%m-%d'),
    }
 
    tg.send_trade_opened(thesis, order_result, account_value)
    return position
 
 
def run():
    print("[Execution Agent] Loading approved trades...")
 
    approved_data = load_json('data/approved_trades.json', {})
    approved = approved_data.get('trades', [])
 
    if not approved:
        print("[Execution Agent] No approved trades today.")
        tg.send("No trades passed risk checks today. No positions opened.")
        return []
 
    print(f"  {len(approved)} approved trade(s) - executing autonomously")
 
    print("[Execution Agent] Connecting to IBKR...")
    ib = IB()
    try:
        ib.connect(IB_HOST, IB_PORT, clientId=IB_CLIENT_ID)
        ib.reqMarketDataType(1)  # Real-time, fallback to frozen
        print(f"  Connected. Account: {ib.managedAccounts()}")
    except Exception as e:
        print(f"  IBKR connection failed: {e}")
        tg.send(f"PROMETHEUS ERROR - Could not connect to IBKR.\n{e}")
        return []
 
    account_value  = get_account_value(ib)
    open_positions = load_json('data/open_positions.json', [])
    executed       = []
 
    for thesis in approved:
        ticker = thesis.get('ticker', '')
        print(f"\n  Processing {ticker}...")
        position = execute_thesis(ib, thesis, account_value)
        if position:
            open_positions.append(position)
            executed.append(position)
 
    ib.disconnect()
 
    save_json('data/open_positions.json', open_positions)
    print(f"\n[Execution Agent] Complete - {len(executed)} trade(s) executed")
    return executed
 
 
if __name__ == '__main__':
    run()
