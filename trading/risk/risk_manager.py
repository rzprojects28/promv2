"""
Prometheus Phase 3 — Risk Manager Agent (v2)
Validates every trade thesis against hard risk rules before allowing execution.

Upgrades from v1:
- Risk-per-trade sizing: position sized by distance to invalidation stop
- Correlation check: blocks highly correlated positions
- Portfolio Greeks stub: tracks sector exposure as proxy for correlation

Output: data/approved_trades.json  +  data/rejected_trades.json
"""
import json
import os
import re
from datetime import datetime

# ── Hard risk rules ────────────────────────────────────────────────────────
MAX_POSITION_PCT        = 5.0    # max % of capital in any single name
MAX_SECTOR_PCT          = 20.0   # max % of capital in any single sector
MAX_CORRELATED_NAMES    = 5      # max open positions
MAX_TOTAL_DELTA_PCT     = 30.0   # max net directional exposure
MAX_RISK_PER_TRADE_PCT  = 0.5    # max % of portfolio to LOSE on any single trade
CORRELATION_BLOCK_PCT   = 60.0   # block if >60% of open positions in same sector

SECTOR_MAP = {
    'XLK':'Technology',    'XLF':'Financials',     'XLV':'Healthcare',
    'XLE':'Energy',        'XLI':'Industrials',    'XLB':'Materials',
    'XLU':'Utilities',     'XLRE':'Real Estate',   'XLY':'Consumer Discretionary',
    'XLP':'Consumer Staples', 'XLC':'Communication Services',
}

# Correlation groups — names in same group are treated as correlated
CORRELATION_GROUPS = {
    'mega_cap_tech':    ['AAPL','MSFT','GOOGL','GOOG','AMZN','META','NVDA','TSLA'],
    'semiconductors':   ['NVDA','AMD','INTC','QCOM','AVGO','MU','AMAT','LRCX'],
    'financials':       ['JPM','BAC','GS','MS','WFC','C','BLK','SCHW'],
    'energy_majors':    ['XOM','CVX','COP','OXY','SLB','EOG','PXD'],
    'healthcare':       ['JNJ','UNH','PFE','ABBV','MRK','LLY','TMO'],
    'consumer_disc':    ['AMZN','TSLA','HD','MCD','NKE','SBUX','TGT'],
    'utilities':        ['NEE','DUK','SO','D','AEP','EXC','SRE'],
    'reits':            ['PLD','AMT','EQIX','CCI','SPG','O','DLR'],
}


def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def get_sector_etf(thesis):
    sector_field = thesis.get('sector', '')
    for etf in SECTOR_MAP:
        if etf in sector_field:
            return etf
    return 'UNKNOWN'


def extract_stop_price(invalidation_text, entry_price):
    """
    Extract the stop price from invalidation conditions text.
    Looks for patterns like '$118', 'below 118', 'closes below $120'
    Returns stop price or None if not parseable.
    """
    if not invalidation_text or not entry_price:
        return None
    
    # Patterns: $118, below $118, below 118, closes below $118.50
    patterns = [
        r'\$(\d+\.?\d*)',           # $118 or $118.50
        r'below\s+(\d+\.?\d*)',     # below 118
        r'under\s+(\d+\.?\d*)',     # under 118
        r'(\d+\.?\d*)\s+support',   # 118 support
        r'breaks?\s+(\d+\.?\d*)',   # breaks 118
    ]
    
    candidates = []
    for pattern in patterns:
        matches = re.findall(pattern, str(invalidation_text), re.IGNORECASE)
        for m in matches:
            try:
                price = float(m)
                # Must be plausible stop — within 30% of entry price
                if entry_price and 0.7 * entry_price <= price <= 1.3 * entry_price:
                    candidates.append(price)
            except ValueError:
                continue
    
    if candidates:
        # Return the closest price to entry as the stop
        return min(candidates, key=lambda x: abs(x - entry_price))
    return None


def calculate_risk_based_size(thesis, account_value):
    """
    Calculate position size based on risk per trade.
    
    ITPM method:
    Max loss = account_value * MAX_RISK_PER_TRADE_PCT
    Risk per share = entry_price - stop_price
    Position size = max_loss / risk_per_share
    Position % = (position_size * entry_price) / account_value
    
    Falls back to conviction-based sizing if stop can't be parsed.
    """
    entry_price     = float(thesis.get('entry_price', 0))
    invalidation    = thesis.get('invalidation_conditions', '')
    direction       = thesis.get('direction', 'LONG')
    conviction      = thesis.get('conviction', 'MEDIUM')
    
    # Try to extract stop price
    stop_price = extract_stop_price(invalidation, entry_price)
    
    if stop_price and entry_price:
        risk_per_share = abs(entry_price - stop_price)
        if risk_per_share > 0:
            max_loss_usd    = account_value * (MAX_RISK_PER_TRADE_PCT / 100)
            shares          = int(max_loss_usd / risk_per_share)
            position_usd    = shares * entry_price
            position_pct    = (position_usd / account_value) * 100
            
            # Cap at max position size
            position_pct = min(position_pct, MAX_POSITION_PCT)
            
            return round(position_pct, 2), stop_price, 'risk_based', risk_per_share
    
    # Fallback: conviction-based sizing
    conviction_map = {'HIGH': 4.0, 'MEDIUM': 3.0, 'LOW': 1.5}
    size = conviction_map.get(conviction, 2.0)
    return size, None, 'conviction_based', None


def check_correlation(thesis, open_positions):
    """
    Block if adding this position would make the portfolio too correlated.
    
    Two correlation checks:
    1. Ticker is in same correlation group as existing positions
    2. Adding this would put >60% of positions in same sector
    """
    ticker = thesis.get('ticker', '').upper()
    sector = get_sector_etf(thesis)
    
    if not open_positions:
        return True, "No existing positions — no correlation risk"
    
    # Check 1: Direct correlation group overlap
    new_ticker_groups = [g for g, tickers in CORRELATION_GROUPS.items() if ticker in tickers]
    
    correlated_existing = []
    for pos in open_positions:
        pos_ticker = pos.get('ticker', '').upper()
        pos_groups = [g for g, tickers in CORRELATION_GROUPS.items() if pos_ticker in tickers]
        
        # Check if they share any correlation group
        shared_groups = set(new_ticker_groups) & set(pos_groups)
        if shared_groups:
            correlated_existing.append(f"{pos_ticker} ({', '.join(shared_groups)})")
    
    if len(correlated_existing) >= 2:
        return False, f"High correlation: {ticker} shares group with {', '.join(correlated_existing)}"
    
    # Check 2: Sector concentration after adding
    sector_positions = [p for p in open_positions if get_sector_etf(p) == sector]
    total_positions  = len(open_positions)
    
    if total_positions >= 3:
        sector_pct_after = (len(sector_positions) + 1) / (total_positions + 1) * 100
        if sector_pct_after > CORRELATION_BLOCK_PCT:
            return False, (f"Sector concentration: adding {ticker} would put "
                          f"{sector_pct_after:.0f}% of positions in {sector} sector")
    
    return True, f"Correlation check passed — {len(correlated_existing)} related positions"


def check_position_size(size_pct):
    if size_pct > MAX_POSITION_PCT:
        return False, f"Position size {size_pct:.1f}% exceeds max {MAX_POSITION_PCT}%"
    if size_pct <= 0:
        return False, "Position size is 0 or missing"
    return True, f"Position size {size_pct:.1f}% OK"


def check_sector_concentration(thesis, open_positions, size_pct):
    sector = get_sector_etf(thesis)
    existing_pct = sum(
        float(p.get('position_size_pct', 0))
        for p in open_positions
        if get_sector_etf(p) == sector
    )
    total = existing_pct + size_pct
    if total > MAX_SECTOR_PCT:
        return False, f"Sector {sector} would be {total:.1f}% — exceeds max {MAX_SECTOR_PCT}%"
    return True, f"Sector {sector} concentration {total:.1f}% OK"


def check_open_count(open_positions):
    if len(open_positions) >= MAX_CORRELATED_NAMES:
        return False, f"Already {len(open_positions)} open positions — max is {MAX_CORRELATED_NAMES}"
    return True, f"Open positions {len(open_positions)}/{MAX_CORRELATED_NAMES} OK"


def check_duplicate(thesis, open_positions):
    ticker = thesis.get('ticker', '').upper()
    for p in open_positions:
        if p.get('ticker', '').upper() == ticker:
            return False, f"{ticker} already has an open position"
    return True, f"No duplicate for {ticker}"


def check_delta(thesis, open_positions, size_pct):
    current = sum(
        float(p.get('position_size_pct', 0)) * (1 if p.get('direction') == 'LONG' else -1)
        for p in open_positions
    )
    new_delta  = size_pct * (1 if thesis.get('direction') == 'LONG' else -1)
    total_delta = abs(current + new_delta)
    if total_delta > MAX_TOTAL_DELTA_PCT:
        return False, f"Net delta {total_delta:.1f}% would exceed max {MAX_TOTAL_DELTA_PCT}%"
    return True, f"Net delta {total_delta:.1f}% OK"


def validate_thesis(thesis, open_positions, account_value=100_000):
    """
    Full validation with risk-based sizing.

    For OPTIONS theses (instrument='options'):
      - position_size_pct is a placeholder for the portfolio checks (use the
        max_risk pct directly — execution_agent computes actual contracts)
      - Skip the share-based risk_per_share path; the underlying stop is still
        used as the invalidation level (extracted from invalidation_conditions)
        but contract sizing happens at execution time with live option prices.
    """
    instrument = (thesis.get('instrument') or 'stock').lower()

    if instrument == 'options':
        # Parse the underlying stop (used by monitor for invalidation level)
        entry_price       = float(thesis.get('entry_price', 0))
        invalidation_text = thesis.get('invalidation_conditions', '')
        underlying_stop   = extract_stop_price(invalidation_text, entry_price)

        thesis['position_size_pct'] = MAX_RISK_PER_TRADE_PCT  # 0.5% premium budget
        thesis['calculated_stop']   = underlying_stop          # for the monitor
        thesis['sizing_method']     = 'options_risk_based'
        thesis['risk_per_share']    = None
        thesis['max_risk_usd']      = round(account_value * MAX_RISK_PER_TRADE_PCT / 100, 2)

        # Portfolio-level checks still apply: don't open duplicate names,
        # don't blow past max open, don't overconcentrate sectors.
        size_pct_for_portfolio = MAX_RISK_PER_TRADE_PCT
        checks = [
            check_duplicate(thesis, open_positions),
            check_sector_concentration(thesis, open_positions, size_pct_for_portfolio),
            check_open_count(open_positions),
            check_correlation(thesis, open_positions),
        ]
        passed   = all(ok for ok, _ in checks)
        messages = [f"{'✓' if ok else '✗'} {msg}" for ok, msg in checks]
        return passed, messages

    # ── Stock path (legacy) ───────────────────────────────────────────────
    size_pct, stop_price, sizing_method, risk_per_share = calculate_risk_based_size(
        thesis, account_value
    )

    thesis['position_size_pct']  = size_pct
    thesis['calculated_stop']    = stop_price
    thesis['sizing_method']      = sizing_method
    thesis['risk_per_share']     = round(risk_per_share, 2) if risk_per_share else None
    thesis['max_risk_usd']       = round(account_value * MAX_RISK_PER_TRADE_PCT / 100, 2)

    checks = [
        check_duplicate(thesis, open_positions),
        check_position_size(size_pct),
        check_sector_concentration(thesis, open_positions, size_pct),
        check_open_count(open_positions),
        check_delta(thesis, open_positions, size_pct),
        check_correlation(thesis, open_positions),
    ]
    
    passed   = all(ok for ok, _ in checks)
    messages = [f"{'✓' if ok else '✗'} {msg}" for ok, msg in checks]
    
    return passed, messages


def run():
    print("[Risk Manager v2] Loading trade theses and open positions...")

    # Research outputs live next to the research code (trading/research/data/).
    _research_data = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'research', 'data', 'trade_theses.json',
    )
    theses_data = load_json(_research_data, {})
    theses      = theses_data.get('theses', [])
    if not theses:
        print("[Risk Manager v2] No theses found. Run Phase 2 pipeline first.")
        return

    open_positions = load_json('data/open_positions.json', [])

    # Get account value — try to read from IBKR, fallback to 100k
    account_value = 100_000
    try:
        from ib_insync import IB
        from dotenv import load_dotenv
        import os
        load_dotenv(dotenv_path=os.path.expanduser('~/promv2/.env'))
        ib = IB()
        ib.connect(os.getenv('IB_HOST','127.0.0.1'), int(os.getenv('IB_PORT',4002)), clientId=10)
        for av in ib.accountValues():
            if av.tag == 'NetLiquidation' and av.currency == 'USD':
                account_value = float(av.value)
                break
        ib.disconnect()
        print(f"  Account value: ${account_value:,.2f}")
    except Exception:
        print(f"  Account value: ${account_value:,.2f} (fallback — IBKR not connected)")

    print(f"  {len(theses)} theses | {len(open_positions)} open | "
          f"Max risk/trade: {MAX_RISK_PER_TRADE_PCT}% (${account_value * MAX_RISK_PER_TRADE_PCT / 100:,.0f})")

    approved, rejected = [], []

    for thesis in theses:
        ticker = thesis.get('ticker', 'UNKNOWN')
        passed, messages = validate_thesis(thesis, open_positions, account_value)

        # Log sizing info
        sizing   = thesis.get('sizing_method', '')
        size_pct = thesis.get('position_size_pct', 0)
        stop     = thesis.get('calculated_stop')
        rps      = thesis.get('risk_per_share')

        result = {
            **thesis,
            'risk_check_time': datetime.now().isoformat(),
            'risk_checks':     messages,
            'approved':        passed,
        }

        if passed:
            approved.append(result)
            print(f"  ✓ APPROVED  {ticker} {thesis.get('direction')} "
                  f"[{thesis.get('conviction')}] — "
                  f"{size_pct:.1f}% position ({sizing})"
                  + (f" | stop ${stop:.2f} | risk ${rps:.2f}/share" if stop else ""))
        else:
            rejected.append(result)
            print(f"  ✗ REJECTED  {ticker} {thesis.get('direction')}")
            for m in messages:
                if any(word in m.lower() for word in ['exceed', 'already', 'duplicate', 'correlation']):
                    print(f"    → {m}")

    os.makedirs('data', exist_ok=True)
    with open('data/approved_trades.json', 'w') as f:
        json.dump({'generated_at': datetime.now().isoformat(), 'trades': approved}, f, indent=2)
    with open('data/rejected_trades.json', 'w') as f:
        json.dump({'generated_at': datetime.now().isoformat(), 'trades': rejected}, f, indent=2)

    print(f"\n[Risk Manager v2] Complete — {len(approved)} approved, {len(rejected)} rejected")
    return {'approved': approved, 'rejected': rejected}


if __name__ == '__main__':
    run()
