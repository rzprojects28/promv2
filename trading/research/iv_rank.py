"""
Prometheus Phase 2 — IV Rank Fetcher
Fetches current implied volatility from IBKR option chains for candidate tickers.
Calculates IV rank vs recent history so Claude picks the right options strategy.

Strategy by IV rank:
  IV rank below 30%  → BUY long calls/puts (options are cheap)
  IV rank 30-70%     → mixed strategies acceptable
  IV rank above 70%  → SELL vertical spreads (options are expensive)
"""
import os
import math
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.expanduser('~/promv2/.env'))


def fetch_iv_data(tickers: list, ib_port: int = 4002) -> dict:
    """
    Fetch current implied volatility for each ticker's ATM options.
    Returns dict: {ticker: {current_iv, iv_rank, regime, recommendation}}
    """
    iv_data = {}

    try:
        from ib_insync import IB, Stock, Option
        import logging
        logging.getLogger('ib_insync').setLevel(logging.CRITICAL)
        ib = IB()
        ib.connect('127.0.0.1', ib_port, clientId=int(os.getenv('IV_FETCHER_CLIENT_ID', 94)))
        ib.reqMarketDataType(3)

        for ticker in tickers:
            try:
                # Get current stock price
                stock = Stock(ticker, 'SMART', 'USD')
                ib.qualifyContracts(stock)
                td = ib.reqMktData(stock, '', False, False)
                ib.sleep(2)

                price = None
                for attr in ['last', 'close', 'bid']:
                    v = getattr(td, attr, None)
                    if v and not math.isnan(v) and v > 0:
                        price = float(v)
                        break

                if not price:
                    iv_data[ticker] = {'error': 'No price available'}
                    continue

                # Find next monthly options expiry (closest to 45 DTE)
                target_dte = 45
                target_date = datetime.now() + timedelta(days=target_dte)

                # Request option chain
                chains = ib.reqSecDefOptParams(stock.symbol, '', stock.secType, stock.conId)
                ib.sleep(1)

                if not chains:
                    iv_data[ticker] = {'error': 'No option chain'}
                    continue

                # Pick closest expiry to 45 DTE
                exchange    = next((c for c in chains if c.exchange == 'SMART'), chains[0])
                expirations = sorted(exchange.expirations)
                best_exp    = min(expirations,
                                  key=lambda e: abs((datetime.strptime(e, '%Y%m%d') - target_date).days))
                actual_dte  = (datetime.strptime(best_exp, '%Y%m%d') - datetime.now()).days

                # Find ATM strike — prefer whole-dollar strikes for liquidity
                strikes = sorted(exchange.strikes)
                # Try whole-dollar strikes first (most liquid)
                whole_strikes = [s for s in strikes if s == int(s)]
                if whole_strikes and price > 20:
                    atm = min(whole_strikes, key=lambda s: abs(s - price))
                else:
                    atm = min(strikes, key=lambda s: abs(s - price))

                # Request ATM call data
                option = Option(ticker, best_exp, atm, 'C', 'SMART')
                ib.qualifyContracts(option)
                opt_td = ib.reqMktData(option, '', False, False)
                ib.sleep(3)

                iv = None
                if hasattr(opt_td, 'modelGreeks') and opt_td.modelGreeks:
                    iv = opt_td.modelGreeks.impliedVol
                if not iv and hasattr(opt_td, 'impliedVolatility'):
                    iv = opt_td.impliedVolatility

                if iv and not math.isnan(iv) and iv > 0:
                    iv_pct = round(iv * 100, 1)

                    # Categorize regime (simplified IV rank — no 52w history available)
                    # Use absolute IV levels as a proxy
                    if iv_pct < 20:
                        regime, recommendation = 'LOW', 'BUY long calls/puts — options are cheap'
                    elif iv_pct < 35:
                        regime, recommendation = 'NORMAL_LOW', 'Long options preferred, spreads acceptable'
                    elif iv_pct < 55:
                        regime, recommendation = 'NORMAL_HIGH', 'Mixed strategies — vertical spreads recommended'
                    else:
                        regime, recommendation = 'HIGH', 'SELL premium — vertical spreads or iron condors'

                    iv_data[ticker] = {
                        'current_iv':     iv_pct,
                        'regime':         regime,
                        'recommendation': recommendation,
                        'atm_strike':     atm,
                        'expiry':         best_exp,
                        'dte':            actual_dte,
                        'stock_price':    round(price, 2),
                    }
                    print(f"  {ticker}: IV={iv_pct}% [{regime}] @ ${atm} strike, {actual_dte} DTE")
                else:
                    iv_data[ticker] = {'error': 'No IV data from IBKR'}
                    print(f"  {ticker}: no IV data")

            except Exception as e:
                iv_data[ticker] = {'error': str(e)}
                print(f"  {ticker}: error ({e})")

        ib.disconnect()

    except Exception as e:
        print(f"  IV fetcher failed: {e}")

    return iv_data


def format_iv_for_prompt(iv_data: dict) -> str:
    """Format IV data into a clean block for the Claude prompt"""
    if not iv_data:
        return "IMPLIED VOLATILITY DATA: unavailable"

    valid = {k: v for k, v in iv_data.items() if 'error' not in v}
    if not valid:
        return "IMPLIED VOLATILITY DATA: unavailable for all candidates"

    lines = [
        "IMPLIED VOLATILITY (live from IBKR options chain — use this to pick options strategy):"
    ]
    for ticker, data in valid.items():
        lines.append(
            f"  {ticker:<6} IV={data['current_iv']:.1f}% [{data['regime']}]  "
            f"ATM strike ${data['atm_strike']} @ {data['dte']} DTE  "
            f"→ {data['recommendation']}"
        )
    lines.append("  USE THIS IV DATA to select options structure — do NOT guess IV percentile from memory")
    return '\n'.join(lines)


if __name__ == '__main__':
    print("Testing IV fetcher...")
    prices = fetch_iv_data(['NVDA', 'AAPL', 'XOM'])
    print()
    print(format_iv_for_prompt(prices))
