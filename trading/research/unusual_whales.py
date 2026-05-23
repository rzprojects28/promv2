"""
Prometheus Phase 2 — Script 3: Unusual Whales Flow Scanner
Pulls dark pool block prints and unusual options flow
Output: data/unusual_whales_flow.json
"""
import requests
import json
import os
from datetime import datetime
from dotenv import load_dotenv
 
load_dotenv()
 
API_KEY  = os.getenv('UNUSUAL_WHALES_KEY')
BASE_URL = 'https://api.unusualwhales.com'
 
DARKPOOL_MIN_NOTIONAL  = 1_000_000   # $1M+
OPTIONS_MIN_PREMIUM    =   500_000   # $500k+
 
 
def headers():
    return {'Authorization': f'Bearer {API_KEY}'}
 
 
def get_darkpool():
    try:
        resp = requests.get(f'{BASE_URL}/api/darkpool/recent', headers=headers(), timeout=15)
        resp.raise_for_status()
        prints = []
        for d in resp.json().get('data', [])[:100]:
            try:
                notional = float(d.get('price', 0)) * int(d.get('size', 0))
                if notional >= DARKPOOL_MIN_NOTIONAL:
                    prints.append({
                        'ticker':       d.get('ticker', ''),
                        'size':         d.get('size', ''),
                        'price':        d.get('price', ''),
                        'notional_usd': round(notional),
                        'executed_at':  d.get('executed_at', ''),
                    })
            except Exception:
                continue
        return prints
    except Exception as e:
        print(f"  Dark pool error: {e}")
        return []
 
 
def get_options_flow():
    try:
        resp = requests.get(
            f'{BASE_URL}/api/option-trades/flow-alerts',
            headers=headers(),
            params={'limit': 50},
            timeout=15,
        )
        resp.raise_for_status()
        alerts = []
        for d in resp.json().get('data', [])[:50]:
            try:
                premium = float(d.get('total_premium') or d.get('premium') or 0)
                if premium >= OPTIONS_MIN_PREMIUM:
                    alerts.append({
                        'ticker':      d.get('ticker', ''),
                        'strike':      d.get('strike', ''),
                        'expiry':      d.get('expiry', ''),
                        'call_put':    d.get('call_put', ''),
                        'premium_usd': round(premium),
                        'sentiment':   d.get('sentiment', ''),
                        'executed_at': d.get('created_at', ''),
                    })
            except Exception:
                continue
        return alerts
    except Exception as e:
        print(f"  Options flow error: {e}")
        return []
 
 
def get_market_tide():
    try:
        resp = requests.get(f'{BASE_URL}/api/market/market-tide', headers=headers(), timeout=15)
        if resp.status_code == 200:
            return resp.json().get('data', {})
    except Exception as e:
        print(f"  Market tide error: {e}")
    return {}
 
 
def run():
    if not API_KEY or API_KEY == 'YOUR-KEY-HERE':
        print("[Unusual Whales] ERROR: UNUSUAL_WHALES_KEY not set in .env")
        return {}
 
    print("[Unusual Whales] Fetching flow data...")
 
    print("  Fetching dark pool prints...")
    dp = get_darkpool()
    print(f"  Found {len(dp)} large dark pool prints (>${DARKPOOL_MIN_NOTIONAL/1e6:.0f}M+)")
 
    print("  Fetching options flow alerts...")
    flow = get_options_flow()
    print(f"  Found {len(flow)} significant flow alerts (>${OPTIONS_MIN_PREMIUM/1e3:.0f}k+)")
 
    print("  Fetching market tide...")
    tide = get_market_tide()
 
    dp_tickers   = list(dict.fromkeys([d['ticker'] for d in dp]))[:15]
    flow_tickers = list(dict.fromkeys([d['ticker'] for d in flow]))[:15]
 
    os.makedirs('data', exist_ok=True)
    output = {
        'generated_at':   datetime.now().isoformat(),
        'dark_pool_prints': dp,
        'options_flow':   flow,
        'market_tide':    tide,
        'summary': {
            'large_darkpool_prints':      len(dp),
            'significant_options_flow':   len(flow),
            'top_darkpool_tickers':       dp_tickers,
            'top_flow_tickers':           flow_tickers,
        },
    }
    with open('data/unusual_whales_flow.json', 'w') as f:
        json.dump(output, f, indent=2)
 
    print(f"[Unusual Whales] Complete. Saved → data/unusual_whales_flow.json")
    if dp_tickers:
        print(f"  Dark pool tickers : {', '.join(dp_tickers)}")
    if flow_tickers:
        print(f"  Options flow tickers: {', '.join(flow_tickers)}")
    return output
 
if __name__ == '__main__':
    run()
 
