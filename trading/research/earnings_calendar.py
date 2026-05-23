"""
Prometheus Phase 2 — Earnings Calendar
Fetches upcoming earnings dates for the next 45 days from FMP API.
Injected into Analysis Agent prompt so Claude uses real catalyst dates.
"""
import os
import json
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.expanduser('~/prometheus/.env'))

FMP_API_KEY = os.getenv('FMP_API_KEY', 'demo')


def get_earnings_calendar(days_ahead: int = 45) -> list:
    """
    Fetch earnings calendar for the next N days from FMP API.
    Returns list of {ticker, date, estimated_eps, revenue_estimate}
    """
    today = datetime.now()
    end   = today + timedelta(days=days_ahead)

    today_str = today.strftime('%Y-%m-%d')
    end_str   = end.strftime('%Y-%m-%d')

    try:
        url  = f"https://financialmodelingprep.com/stable/earnings-calendar?from={today_str}&to={end_str}&apikey={FMP_API_KEY}"
        resp = requests.get(url, timeout=15)

        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                # Filter to meaningful entries and sort by date
                events = []
                for item in data:
                    ticker = item.get('symbol', '')
                    date   = item.get('date', '')
                    if ticker and date and len(ticker) <= 5:
                        events.append({
                            'ticker':    ticker,
                            'date':      date,
                            'eps_est':   item.get('epsEstimated'),
                            'rev_est':   item.get('revenueEstimated'),
                            'days_away': (datetime.strptime(date, '%Y-%m-%d') - today).days
                        })
                # Sort by date
                events.sort(key=lambda x: x['date'])
                print(f"  Earnings calendar: {len(events)} events in next {days_ahead} days")
                return events
        else:
            print(f"  FMP earnings calendar failed: {resp.status_code}")

    except Exception as e:
        print(f"  Earnings calendar error: {e}")

    return []


def filter_earnings_for_tickers(calendar: list, tickers: list) -> list:
    """Filter earnings calendar to only the tickers we care about"""
    ticker_set = set(t.upper() for t in tickers)
    return [e for e in calendar if e['ticker'].upper() in ticker_set]


def format_earnings_for_prompt(calendar: list, tickers: list = None) -> str:
    """Format earnings calendar into a clean block for the Claude prompt"""
    if tickers:
        relevant = filter_earnings_for_tickers(calendar, tickers)
    else:
        relevant = calendar[:20]  # Show first 20 if no filter

    if not relevant:
        return f"UPCOMING EARNINGS (next 45 days): None found for current watchlist tickers"

    today = datetime.now()
    lines = [f"UPCOMING EARNINGS (next 45 days from today {today.strftime('%Y-%m-%d')}):"]

    for e in relevant[:15]:
        days = e['days_away']
        line = f"  {e['ticker']:<6} {e['date']}  ({days} days away)"
        if e.get('eps_est'):
            line += f"  EPS est: ${e['eps_est']:.2f}"
        lines.append(line)

    lines.append("  (Use these EXACT dates for all catalyst timelines — do not guess or use past dates)")
    return '\n'.join(lines)


def format_options_expiry_dates() -> str:
    """
    Generate the valid options expiry window for 45-60 DTE from today.
    Gives Claude exact date ranges to use instead of guessing month/year.
    """
    today    = datetime.now()
    dteStart = today + timedelta(days=45)
    dteEnd   = today + timedelta(days=60)

    # Find next monthly expiry Fridays in that range
    expiries = []
    current  = dteStart
    while current <= dteEnd + timedelta(days=30):
        # Third Friday of each month
        month_start = current.replace(day=1)
        first_friday = month_start + timedelta(days=(4 - month_start.weekday()) % 7)
        third_friday = first_friday + timedelta(weeks=2)
        if dteStart <= third_friday <= dteEnd + timedelta(days=30):
            expiries.append(third_friday)
        current = (current.replace(day=1) + timedelta(days=32)).replace(day=1)
        if len(expiries) >= 2:
            break

    lines = [f"OPTIONS EXPIRY WINDOW (45-60 DTE from today {today.strftime('%Y-%m-%d')}):"]
    lines.append(f"  45 DTE = {dteStart.strftime('%Y-%m-%d')}")
    lines.append(f"  60 DTE = {dteEnd.strftime('%Y-%m-%d')}")
    if expiries:
        for exp in expiries[:2]:
            lines.append(f"  Nearest monthly expiry in window: {exp.strftime('%Y-%m-%d')} ({(exp-today).days} DTE)")
    lines.append("  USE THESE DATES for all options structures — never reference past months or years")
    return '\n'.join(lines)


if __name__ == '__main__':
    print("Testing earnings calendar...")
    print(f"Today: {datetime.now().strftime('%Y-%m-%d')}\n")

    calendar = get_earnings_calendar(45)
    test_tickers = ['NVDA', 'AAPL', 'MSFT', 'AMD', 'XOM', 'JPM', 'DELL', 'MRVL']
    print(format_earnings_for_prompt(calendar, test_tickers))
    print()
    print(format_options_expiry_dates())
