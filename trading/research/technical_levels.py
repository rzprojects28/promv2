"""
Prometheus Phase 2 — Technical Levels Fetcher
Fetches recent price history (20-day high/low, 50-day MA, 200-day MA) for candidate tickers.
Gives Claude real support/resistance levels to anchor stop prices.

Without this, Claude invents stop levels from training memory.
With this, Claude can place stops at actual recent swing lows or moving averages.
"""
import os
from datetime import datetime, timedelta


def fetch_technical_levels(tickers: list) -> dict:
    """
    Fetch 20-day high/low, 50-day MA, and 200-day MA for each ticker.
    Uses yfinance — free and reliable for historical price data.
    Returns dict: {ticker: {high_20d, low_20d, ma_50, ma_200, current_vs_ma50}}
    """
    levels = {}

    try:
        import yfinance as yf

        for ticker in tickers:
            try:
                stock = yf.Ticker(ticker)
                hist  = stock.history(period='1y')

                if hist.empty or len(hist) < 50:
                    levels[ticker] = {'error': 'Insufficient history'}
                    continue

                current      = float(hist['Close'].iloc[-1])
                high_20d     = round(float(hist['High'].tail(20).max()), 2)
                low_20d      = round(float(hist['Low'].tail(20).min()), 2)
                high_52w     = round(float(hist['High'].max()), 2)
                low_52w      = round(float(hist['Low'].min()), 2)
                ma_50        = round(float(hist['Close'].tail(50).mean()), 2)
                ma_200       = round(float(hist['Close'].mean()), 2) if len(hist) >= 200 else None

                # Distance from key levels (as %)
                pct_from_high_20d = round((current - high_20d) / high_20d * 100, 1)
                pct_from_low_20d  = round((current - low_20d) / low_20d * 100, 1)
                pct_from_ma_50   = round((current - ma_50) / ma_50 * 100, 1)

                # Trend classification
                if ma_200 and ma_50 > ma_200 * 1.02 and current > ma_50:
                    trend = 'UPTREND_STRONG'
                elif current > ma_50:
                    trend = 'UPTREND'
                elif current < ma_50 * 0.98 and ma_200 and ma_50 < ma_200:
                    trend = 'DOWNTREND_STRONG'
                elif current < ma_50:
                    trend = 'DOWNTREND'
                else:
                    trend = 'SIDEWAYS'

                # Suggested stop levels for LONG and SHORT
                suggested_long_stop  = min(low_20d, round(ma_50 * 0.98, 2))
                suggested_short_stop = max(high_20d, round(ma_50 * 1.02, 2))

                levels[ticker] = {
                    'current':              round(current, 2),
                    'high_20d':             high_20d,
                    'low_20d':              low_20d,
                    'high_52w':             high_52w,
                    'low_52w':              low_52w,
                    'ma_50':                ma_50,
                    'ma_200':               ma_200,
                    'pct_from_high_20d':    pct_from_high_20d,
                    'pct_from_low_20d':     pct_from_low_20d,
                    'pct_from_ma_50':       pct_from_ma_50,
                    'trend':                trend,
                    'suggested_long_stop':  suggested_long_stop,
                    'suggested_short_stop': suggested_short_stop,
                }
                print(f"  {ticker}: ${current:.2f} [{trend}] 20d range ${low_20d}-${high_20d}, MA50 ${ma_50}")

            except Exception as e:
                levels[ticker] = {'error': str(e)}
                print(f"  {ticker}: error ({e})")

    except ImportError:
        print("  yfinance not installed — technical levels unavailable")

    return levels


def format_levels_for_prompt(levels: dict) -> str:
    """Format technical levels into a clean block for the Claude prompt"""
    if not levels:
        return "TECHNICAL LEVELS: unavailable"

    valid = {k: v for k, v in levels.items() if 'error' not in v}
    if not valid:
        return "TECHNICAL LEVELS: unavailable for all candidates"

    lines = [
        "TECHNICAL LEVELS (live from market history — use these EXACT levels for stops):"
    ]
    for ticker, d in valid.items():
        ma200_str = f"MA200 ${d['ma_200']}" if d.get('ma_200') else "MA200 n/a"
        lines.append(
            f"  {ticker:<6} ${d['current']}  [{d['trend']}]\n"
            f"          20d range: ${d['low_20d']} – ${d['high_20d']}  "
            f"({d['pct_from_low_20d']:+.1f}% / {d['pct_from_high_20d']:+.1f}% from edges)\n"
            f"          MA50 ${d['ma_50']} ({d['pct_from_ma_50']:+.1f}% from current)  |  {ma200_str}\n"
            f"          → Suggested LONG stop: ${d['suggested_long_stop']}  "
            f"|  Suggested SHORT stop: ${d['suggested_short_stop']}"
        )
    lines.append("  USE THESE LEVELS for stop placement — do NOT invent support/resistance from memory")
    return '\n'.join(lines)


if __name__ == '__main__':
    print("Testing technical levels...")
    lvls = fetch_technical_levels(['NVDA', 'AAPL', 'XOM'])
    print()
    print(format_levels_for_prompt(lvls))
