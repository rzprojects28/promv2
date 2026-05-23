"""
Standalone IBKR access for the reporting package.

Only used to read live prices and NetLiquidation. Never places orders.
Falls back to yfinance for price gaps. If both fail, the report still goes
out — unrealized for that ticker shows as N/A.
"""
import math
import os


def fetch_account_value_and_prices(open_positions: list, ib_port: int, label: str):
    """
    Returns (account_value, currency, {ticker: price-or-None}).

    Currency is detected from any NetLiquidation row whose currency tag is
    not 'BASE' and matches the gateway's managed account.
    """
    prices = {p.get('ticker'): None for p in open_positions if p.get('ticker')}
    account_value, currency = 100_000.0, 'USD'

    try:
        from ib_insync import IB, Stock
        ib = IB()
        ib.connect('127.0.0.1', ib_port, clientId=int(os.getenv('IB_CLIENT_REPORTING', 96)),
                   timeout=10)
        ib.reqMarketDataType(4)   # delayed (free tier)

        managed    = ib.managedAccounts()
        my_account = managed[0] if managed else None
        for av in ib.accountValues():
            if my_account and av.account != my_account:
                continue
            if av.tag == 'NetLiquidation' and av.currency and av.currency != 'BASE':
                currency      = av.currency
                account_value = float(av.value)

        for ticker in list(prices.keys()):
            try:
                contract = Stock(ticker, 'SMART', 'USD')
                ib.qualifyContracts(contract)
                td = ib.reqMktData(contract, '', False, False)
                ib.sleep(2)
                for attr in ['last', 'close', 'bid']:
                    v = getattr(td, attr, None)
                    if v and not math.isnan(v) and v > 0:
                        prices[ticker] = float(v)
                        break
            except Exception:
                pass

        ib.disconnect()
    except Exception as e:
        print(f"  [{label}] IBKR fetch failed: {e}")

    # yfinance fallback for tickers still missing
    missing = [t for t, v in prices.items() if v is None]
    if missing:
        try:
            import yfinance as yf
            for t in missing:
                try:
                    fi   = yf.Ticker(t).fast_info
                    p_yf = getattr(fi, "last_price", None) or getattr(fi, "previous_close", None)
                    if p_yf and not math.isnan(float(p_yf)) and float(p_yf) > 0:
                        prices[t] = float(p_yf)
                except Exception:
                    pass
        except ImportError:
            pass

    return account_value, currency, prices
