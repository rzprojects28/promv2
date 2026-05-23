"""
Prometheus Phase 2 — Script 1: Sector Ranking
Ranks all 11 SPDR sector ETFs by composite momentum (1w / 1m / 3m)
Output: data/sector_ranking.json
"""
import yfinance as yf
import json
import os
from datetime import datetime
 
SECTOR_ETFS = {
    'XLK':  'Technology',
    'XLF':  'Financials',
    'XLV':  'Healthcare',
    'XLE':  'Energy',
    'XLI':  'Industrials',
    'XLB':  'Materials',
    'XLU':  'Utilities',
    'XLRE': 'Real Estate',
    'XLY':  'Consumer Discretionary',
    'XLP':  'Consumer Staples',
    'XLC':  'Communication Services',
}
 
def get_momentum(ticker):
    data = yf.download(ticker, period='3mo', progress=False, auto_adjust=True)
    if data.empty or len(data) < 5:
        return None
 
    close = data['Close'].squeeze()
    current      = float(close.iloc[-1])
    week_ago     = float(close.iloc[-6])  if len(close) >= 6  else float(close.iloc[0])
    month_ago    = float(close.iloc[-22]) if len(close) >= 22 else float(close.iloc[0])
    quarter_ago  = float(close.iloc[0])
 
    r1w = (current - week_ago)    / week_ago
    r1m = (current - month_ago)   / month_ago
    r3m = (current - quarter_ago) / quarter_ago
    composite = (r1w + r1m + r3m) / 3
 
    return {
        'ticker':          ticker,
        'name':            SECTOR_ETFS[ticker],
        'return_1w':       round(r1w * 100, 2),
        'return_1m':       round(r1m * 100, 2),
        'return_3m':       round(r3m * 100, 2),
        'composite_score': round(composite * 100, 4),
        'current_price':   round(current, 2),
    }
 
def run():
    print("[Sector Ranking] Fetching momentum data...")
    results = []
 
    for ticker in SECTOR_ETFS:
        try:
            row = get_momentum(ticker)
            if row:
                results.append(row)
                print(f"  {ticker}: {row['composite_score']:+.2f}%")
        except Exception as e:
            print(f"  {ticker}: ERROR — {e}")
 
    results.sort(key=lambda x: x['composite_score'], reverse=True)
    for i, r in enumerate(results):
        r['rank'] = i + 1
 
    os.makedirs('data', exist_ok=True)
    output = {
        'generated_at':  datetime.now().isoformat(),
        'top_sectors':   results[:3],
        'bottom_sectors':results[-3:],
        'all_sectors':   results,
    }
    with open('data/sector_ranking.json', 'w') as f:
        json.dump(output, f, indent=2)
 
    print(f"\n[Sector Ranking] Complete.")
    print(f"  #1 {results[0]['ticker']} ({results[0]['name']}) {results[0]['composite_score']:+.2f}%")
    print(f"  #2 {results[1]['ticker']} ({results[1]['name']}) {results[1]['composite_score']:+.2f}%")
    print(f"  #3 {results[2]['ticker']} ({results[2]['name']}) {results[2]['composite_score']:+.2f}%")
    print(f"  Worst: {results[-1]['ticker']} ({results[-1]['name']}) {results[-1]['composite_score']:+.2f}%")
    print(f"  Saved → data/sector_ranking.json")
    return output
 
if __name__ == '__main__':
    run()
 
