"""
Prometheus Phase 2 — Analyst Estimates Fetcher
Fetches EPS estimate revision trends from FMP API.
Tells Claude whether analysts are raising or cutting estimates — a key ITPM signal.

Rising EPS revisions = institutional consensus improving = bullish
Falling EPS revisions = institutional consensus deteriorating = bearish
"""
import os
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.expanduser('~/promv2/.env'))

FMP_API_KEY = os.getenv('FMP_API_KEY', 'demo')


def fetch_analyst_estimates(tickers: list) -> dict:
    """
    Fetch analyst EPS estimate history for each ticker.
    Returns dict: {ticker: {current_eps_est, prior_eps_est, revision_direction, ...}}
    """
    estimates = {}

    for ticker in tickers:
        try:
            url  = f"https://financialmodelingprep.com/stable/analyst-estimates?symbol={ticker}&period=quarter&page=0&limit=4&apikey={FMP_API_KEY}"
            resp = requests.get(url, timeout=10)

            if resp.status_code != 200:
                estimates[ticker] = {'error': f'HTTP {resp.status_code}'}
                continue

            data = resp.json()
            if not data or not isinstance(data, list) or len(data) == 0:
                estimates[ticker] = {'error': 'No estimates data'}
                continue

            # Sort by date descending
            data.sort(key=lambda x: x.get('date', ''), reverse=True)

            # Current and prior estimates
            current = data[0]
            prior   = data[1] if len(data) > 1 else None

            current_eps = current.get('epsAvg')
            prior_eps   = prior.get('epsAvg') if prior else None

            # Calculate revision direction
            if current_eps and prior_eps:
                change_pct = ((current_eps - prior_eps) / abs(prior_eps) * 100) if prior_eps != 0 else 0
                if change_pct > 2:
                    direction = 'RISING_STRONG'
                    signal    = 'bullish — analysts upgrading'
                elif change_pct > 0:
                    direction = 'RISING'
                    signal    = 'mildly bullish'
                elif change_pct > -2:
                    direction = 'FLAT'
                    signal    = 'neutral'
                elif change_pct > -5:
                    direction = 'FALLING'
                    signal    = 'mildly bearish'
                else:
                    direction = 'FALLING_STRONG'
                    signal    = 'bearish — analysts downgrading'
            else:
                direction  = 'UNKNOWN'
                signal     = 'insufficient data'
                change_pct = 0

            # Count analysts
            num_analysts = current.get('numAnalystsEps', 0)

            estimates[ticker] = {
                'current_eps_est':     current_eps,
                'current_date':        current.get('date'),
                'prior_eps_est':       prior_eps,
                'prior_date':          prior.get('date') if prior else None,
                'revision_pct':        round(change_pct, 2),
                'direction':           direction,
                'signal':              signal,
                'num_analysts':        num_analysts,
                'revenue_est_low':     current.get('revenueLow'),
                'revenue_est_high':    current.get('revenueHigh'),
                'revenue_est_avg':     current.get('revenueAvg'),
            }
            print(f"  {ticker}: EPS est ${current_eps} ({direction} {change_pct:+.1f}% — {signal})")

        except Exception as e:
            estimates[ticker] = {'error': str(e)}
            print(f"  {ticker}: error ({e})")

    return estimates


def format_estimates_for_prompt(estimates: dict) -> str:
    """Format analyst estimates into a clean block for the Claude prompt"""
    if not estimates:
        return "ANALYST ESTIMATES: unavailable"

    valid = {k: v for k, v in estimates.items() if 'error' not in v and v.get('direction') != 'UNKNOWN'}
    if not valid:
        return "ANALYST ESTIMATES: unavailable for all candidates"

    lines = [
        "ANALYST EPS ESTIMATES (live from FMP — use this for revision-direction signals):"
    ]
    for ticker, data in valid.items():
        lines.append(
            f"  {ticker:<6} Current ${data['current_eps_est']:.2f}  "
            f"Prior ${data['prior_eps_est']:.2f}  "
            f"Revision: {data['revision_pct']:+.1f}% [{data['direction']}]  "
            f"({data['num_analysts']} analysts)  "
            f"→ {data['signal']}"
        )
    lines.append("  USE THIS for the 'earnings revision direction' ITPM filter — do NOT guess from memory")
    return '\n'.join(lines)


if __name__ == '__main__':
    print("Testing analyst estimates...")
    est = fetch_analyst_estimates(['NVDA', 'AAPL', 'XOM', 'JPM'])
    print()
    print(format_estimates_for_prompt(est))
