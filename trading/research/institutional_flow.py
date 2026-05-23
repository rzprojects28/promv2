"""
Prometheus Phase 2 — Script 2: Institutional Flow Scanner
Fetches recent 13F filings and Form 4 insider transactions from SEC EDGAR
Output: data/institutional_flow.json
"""
import requests
import json
import os
import time
from datetime import datetime, timedelta
 
HEADERS = {'User-Agent': 'Prometheus Trading research@prometheus-trading.local'}
 
 
def get_recent_13f(days_back=45):
    end   = datetime.now()
    start = end - timedelta(days=days_back)
    url = (
        "https://efts.sec.gov/LATEST/search-index"
        f"?forms=13F-HR"
        f"&dateRange=custom"
        f"&startdt={start.strftime('%Y-%m-%d')}"
        f"&enddt={end.strftime('%Y-%m-%d')}"
        f"&_source=period_of_report,entity_name,file_date,form_type"
        f"&from=0"
    )
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        hits = resp.json().get('hits', {}).get('hits', [])
        return [
            {
                'entity':    h.get('_source', {}).get('entity_name', ''),
                'file_date': h.get('_source', {}).get('file_date', ''),
                'period':    h.get('_source', {}).get('period_of_report', ''),
            }
            for h in hits[:25]
        ]
    except Exception as e:
        print(f"  13F error: {e}")
        return []
 
 
def get_recent_form4(days_back=14):
    end   = datetime.now()
    start = end - timedelta(days=days_back)
    url = (
        "https://efts.sec.gov/LATEST/search-index"
        f"?forms=4"
        f"&dateRange=custom"
        f"&startdt={start.strftime('%Y-%m-%d')}"
        f"&enddt={end.strftime('%Y-%m-%d')}"
        f"&_source=entity_name,file_date"
        f"&from=0"
    )
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        hits = resp.json().get('hits', {}).get('hits', [])
        return [
            {
                'entity':    h.get('_source', {}).get('entity_name', ''),
                'file_date': h.get('_source', {}).get('file_date', ''),
            }
            for h in hits[:30]
        ]
    except Exception as e:
        print(f"  Form 4 error: {e}")
        return []
 
 
def run():
    print("[Institutional Flow] Fetching SEC EDGAR data...")
 
    print("  Fetching 13F filings (last 45 days)...")
    filings_13f = get_recent_13f()
    print(f"  Found {len(filings_13f)} 13F filings")
 
    time.sleep(1)   # polite pause for SEC servers
 
    print("  Fetching Form 4 insider filings (last 14 days)...")
    filings_4 = get_recent_form4()
    print(f"  Found {len(filings_4)} Form 4 filings")
 
    os.makedirs('data', exist_ok=True)
    output = {
        'generated_at':           datetime.now().isoformat(),
        'recent_13f_filings':     filings_13f,
        'recent_insider_filings': filings_4,
        'summary': {
            'total_13f':    len(filings_13f),
            'total_insider':len(filings_4),
        },
    }
    with open('data/institutional_flow.json', 'w') as f:
        json.dump(output, f, indent=2)
 
    print(f"[Institutional Flow] Complete. Saved → data/institutional_flow.json")
    return output
 
if __name__ == '__main__':
    run()
