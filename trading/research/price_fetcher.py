"""
Prometheus Phase 2 — Price Fetcher
Fetches current live prices for a list of tickers from IBKR.
Called before Analysis Agent runs so Claude has real prices to anchor theses.
"""
import os
import math
from datetime import datetime
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.expanduser('~/promv2/.env'))


def fetch_prices(tickers: list, ib_port: int = 4002) -> dict:
    """
    Fetch current prices for a list of tickers from IBKR.
    Returns dict: {ticker: {price, bid, ask, high_52w, low_52w, change_pct}}
    Falls back to Yahoo Finance if IBKR is unavailable.
    """
    prices = {}

    # Try IBKR first
    try:
        from ib_insync import IB, Stock
        ib = IB()
        ib.connect('127.0.0.1', ib_port, clientId=int(os.getenv('PRICE_FETCHER_CLIENT_ID', 95)))
        ib.reqMarketDataType(3)

        for ticker in tickers:
            try:
                contract = Stock(ticker, 'SMART', 'USD')
                ib.qualifyContracts(contract)
                td = ib.reqMktData(contract, '165', False, False)  # 165 = 52w high/low
                ib.sleep(2)

                price = None
                for attr in ['last', 'close', 'bid']:
                    v = getattr(td, attr, None)
                    if v and not math.isnan(v) and v > 0:
                        price = round(float(v), 2)
                        break

                if price:
                    bid    = round(float(td.bid), 2)  if td.bid  and not math.isnan(td.bid)  else price
                    ask    = round(float(td.ask), 2)  if td.ask  and not math.isnan(td.ask)  else price
                    high52 = round(float(td.high52WeekPrice), 2) if hasattr(td, 'high52WeekPrice') and td.high52WeekPrice and not math.isnan(td.high52WeekPrice) else None
                    low52  = round(float(td.low52WeekPrice), 2)  if hasattr(td, 'low52WeekPrice')  and td.low52WeekPrice  and not math.isnan(td.low52WeekPrice)  else None
                    chg    = round(float(td.changePercent), 2)   if hasattr(td, 'changePercent')   and td.changePercent   and not math.isnan(td.changePercent)   else None

                    prices[ticker] = {
                        'price':      price,
                        'bid':        bid,
                        'ask':        ask,
                        'high_52w':   high52,
                        'low_52w':    low52,
                        'change_pct': chg,
                        'source':     'IBKR',
                        'fetched_at': datetime.now().isoformat(),
                    }
                    print(f"  ${ticker}: ${price} (IBKR)")
                else:
                    print(f"  ${ticker}: no price from IBKR — trying Yahoo")
                    prices[ticker] = _yahoo_price(ticker)

            except Exception as e:
                print(f"  ${ticker}: IBKR error ({e}) — trying Yahoo")
                prices[ticker] = _yahoo_price(ticker)

        ib.disconnect()

    except Exception as e:
        print(f"  IBKR unavailable ({e}) — using Yahoo Finance for all tickers")
        for ticker in tickers:
            prices[ticker] = _yahoo_price(ticker)

    return prices


def _yahoo_price(ticker: str) -> dict:
    """Fallback price fetch from Yahoo Finance"""
    try:
        import yfinance as yf
        stock = yf.Ticker(ticker)
        info  = stock.info
        price = info.get('currentPrice') or info.get('regularMarketPrice') or info.get('previousClose')
        if price:
            return {
                'price':      round(float(price), 2),
                'bid':        round(float(info.get('bid', price)), 2),
                'ask':        round(float(info.get('ask', price)), 2),
                'high_52w':   info.get('fiftyTwoWeekHigh'),
                'low_52w':    info.get('fiftyTwoWeekLow'),
                'change_pct': info.get('regularMarketChangePercent'),
                'source':     'Yahoo',
                'fetched_at': datetime.now().isoformat(),
            }
    except Exception as e:
        print(f"  Yahoo fallback failed for {ticker}: {e}")
    return {'price': None, 'source': 'unavailable', 'fetched_at': datetime.now().isoformat()}


def format_prices_for_prompt(prices: dict) -> str:
    """Format prices into a clean block for the Claude prompt"""
    if not prices:
        return ""

    lines = ["LIVE PRICES (fetched live from market right now — use these for ALL price levels):"]
    for ticker, data in prices.items():
        p = data.get('price')
        if not p:
            continue
        line = f"  {ticker:<6} ${p:.2f}"
        if data.get('change_pct') is not None:
            line += f"  (today: {data['change_pct']:+.1f}%)"
        if data.get('high_52w') and data.get('low_52w'):
            line += f"  52w: ${data['low_52w']:.2f} – ${data['high_52w']:.2f}"
        lines.append(line)

    lines.append(f"  (fetched at {datetime.now().strftime('%Y-%m-%d %H:%M')} SGT)")
    return '\n'.join(lines)


def extract_candidate_tickers(sector_data: dict, uw_data: dict) -> list:
    """
    Extract candidate tickers to price from research data.
    Uses dark pool and options flow tickers as primary candidates,
    plus common large-caps in the top sectors.
    """
    tickers = set()

    # From unusual whales flow
    summ = uw_data.get('summary', {})
    tickers.update(summ.get('top_darkpool_tickers', []))
    tickers.update(summ.get('top_flow_tickers', []))

    # Top options flow tickers
    for flow in uw_data.get('options_flow', [])[:10]:
        t = flow.get('ticker', '')
        if t and len(t) <= 5:
            tickers.add(t)

    # Large-cap representatives for top 3 sectors
    sector_stocks = {
        'XLK':  ['NVDA', 'AAPL', 'MSFT', 'AMD', 'AVGO', 'META', 'GOOGL'],
        'XLF':  ['JPM', 'BAC', 'GS', 'MS', 'WFC'],
        'XLE':  ['XOM', 'CVX', 'COP', 'OXY', 'SLB'],
        'XLV':  ['UNH', 'JNJ', 'LLY', 'ABBV', 'MRK'],
        'XLI':  ['CAT', 'DE', 'HON', 'GE', 'RTX'],
        'XLY':  ['AMZN', 'TSLA', 'HD', 'MCD', 'NKE'],
        'XLB':  ['LIN', 'APD', 'FCX', 'NEM', 'DOW'],
        'XLU':  ['NEE', 'DUK', 'SO', 'D', 'AEP'],
        'XLRE': ['PLD', 'AMT', 'EQIX', 'SPG', 'O'],
        'XLP':  ['PG', 'KO', 'PEP', 'COST', 'WMT'],
        'XLC':  ['META', 'GOOGL', 'NFLX', 'DIS', 'VZ'],
    }

    top_sectors = [s['ticker'] for s in sector_data.get('top_sectors', [])[:3]]
    bottom_sectors = [s['ticker'] for s in sector_data.get('bottom_sectors', [])[:2]]

    for sector in top_sectors + bottom_sectors:
        tickers.update(sector_stocks.get(sector, [])[:4])

    # Cap at 20 tickers to avoid too many IBKR requests
    return list(tickers)[:20]


if __name__ == '__main__':
    print("Testing price fetcher...")
    test_tickers = ['NVDA', 'AAPL', 'XOM', 'JPM', 'NEE']
    prices = fetch_prices(test_tickers)
    print()
    print(format_prices_for_prompt(prices))
