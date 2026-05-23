"""
Prometheus Phase 2 — Analysis Agent (v5)
Synthesises research data and generates ITPM-style trade theses via Claude API.

CRITICAL DATA INJECTIONS (eliminates Claude hallucination):
- TODAY'S DATE injected at top of every prompt
- LIVE PRICES from IBKR for all candidate tickers
- UPCOMING EARNINGS CALENDAR from FMP API
- EXACT OPTIONS EXPIRY DATES for 45-60 DTE window
- LIVE IMPLIED VOLATILITY from IBKR options chain
- ANALYST EPS REVISION DIRECTION from FMP
- TECHNICAL LEVELS (20d high/low, MA50, MA200) from yfinance

v5 hardening:
- system prompt cached via Anthropic ephemeral cache (Opus 4.7 deprecated
  the temperature parameter — extended-thinking mode handles determinism)
- Hard-fail when any live data source is empty (no silent guesswork)
- Post-generation validator: rejects any thesis whose entry_price drifts
  from the live price, whose stop is not parseable, or whose ticker isn't
  in the live data dict
- Explicit "return UNKNOWN, do not use training memory" guardrail
"""
import json
import os
import re
import sys
from datetime import datetime
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.expanduser('~/prometheus/.env'))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import anthropic
client = anthropic.Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'), timeout=300.0)

# ── Import live data modules ──────────────────────────────────────────────
LIVE_DATA_AVAILABLE   = True
IV_DATA_AVAILABLE     = True
ESTIMATES_AVAILABLE   = True
TECHNICALS_AVAILABLE  = True

try:
    from price_fetcher     import fetch_prices, format_prices_for_prompt, extract_candidate_tickers
    from earnings_calendar import get_earnings_calendar, format_earnings_for_prompt, format_options_expiry_dates
except ImportError as e:
    print(f"  WARNING: core live data modules not found ({e})")
    LIVE_DATA_AVAILABLE = False

try:
    from iv_rank import fetch_iv_data, format_iv_for_prompt
except ImportError:
    print("  WARNING: iv_rank module not found")
    IV_DATA_AVAILABLE = False

try:
    from analyst_estimates import fetch_analyst_estimates, format_estimates_for_prompt
except ImportError:
    print("  WARNING: analyst_estimates module not found")
    ESTIMATES_AVAILABLE = False

try:
    from technical_levels import fetch_technical_levels, format_levels_for_prompt
except ImportError:
    print("  WARNING: technical_levels module not found")
    TECHNICALS_AVAILABLE = False


SYSTEM_PROMPT = """You are the Analysis Agent for Prometheus, an AI prop trading system built on ITPM methodology.

Your role: synthesise daily research data and generate high-conviction trade ideas exactly as a professional prop trader at ITPM would.

CRITICAL RULE — INSTRUMENT SELECTION:
- Sector ETFs (XLK, XLF, XLV, XLE, XLI, XLB, XLU, XLRE, XLY, XLP, XLC) are SCREENING TOOLS ONLY.
- NEVER recommend a trade on a sector ETF itself.
- NEVER recommend a trade on QQQ, SPY, IWM or any broad market ETF.
- All trade ideas must be on single-name US-listed stocks.

CRITICAL RULE — USE ONLY THE LIVE DATA PROVIDED:
You have been given LIVE DATA at the top of this prompt. You MUST use it for every quantitative claim.
- TODAY'S DATE is provided — use it for all date calculations
- LIVE PRICES are provided — use these for entry_price (NEVER use prices from memory)
- IV DATA is provided per ticker — use to pick options strategy (NEVER guess IV percentile)
- ANALYST EPS ESTIMATES are provided — use these for revision direction (NEVER guess revision trends)
- TECHNICAL LEVELS are provided (20d high/low, MA50, MA200) — use these for stop placement (NEVER invent support levels)
- UPCOMING EARNINGS DATES are provided — use these for catalyst dates (NEVER reference past earnings)
- OPTIONS EXPIRY WINDOW is provided — use these dates (NEVER reference past months or years)

ANTI-HALLUCINATION POLICY (CRITICAL):
- If the data provided does not contain enough information to answer confidently, OMIT that trade idea or note "INSUFFICIENT DATA" in the relevant field. Do NOT use general knowledge or training memory to fill gaps.
- If you cannot find a ticker in the LIVE PRICES block, do NOT propose a trade on it.
- If TECHNICAL LEVELS does not have a suggested stop for a ticker, do NOT propose a trade on it.
- Returning ZERO theses is acceptable when the data does not support 2+ signal convergence.

Core ITPM rules:
- Long the BEST individual stocks in the BEST sectors.
- Short the WORST individual stocks in the WORST sectors.
- Use options to maximise asymmetric risk/reward on high-conviction catalysts.
- EVERY trade needs: pre-committed thesis, specific catalyst, explicit invalidation conditions, hard time limit.
- Only generate ideas where AT LEAST 2 signals converge.

Stock selection process (use ONLY live data provided):
1. Which large-cap stocks are leaders within the top sector ETF?
2. Which have unusual options flow or dark pool activity from today's data?
3. Which have RISING analyst EPS revisions per the ANALYST EPS ESTIMATES block?
4. Which technical setup is favourable per the TECHNICAL LEVELS block?
5. Pick the ONE stock with the most signal convergence per sector.

Options strategy rules (use the IV DATA block for current IV per ticker):
- Ticker IV regime LOW (under 20%): BUY long calls or puts outright
- Ticker IV regime NORMAL_LOW (20-35%): Long options preferred, spreads acceptable
- Ticker IV regime NORMAL_HIGH (35-55%): Mixed — vertical spreads recommended
- Ticker IV regime HIGH (over 55%): SELL premium via vertical spreads
- Use ONLY the expiry dates from the OPTIONS EXPIRY WINDOW
- Manage at 21 DTE

POSITION SIZING — ITPM METHOD:
- Use entry_price from the LIVE PRICES block
- Use stop price from the TECHNICAL LEVELS suggested stop (or tighter if catalyst-specific)
- Risk Manager calculates: Max loss = 0.5% of portfolio / Risk per share = Entry - Stop

INVALIDATION FORMAT (MANDATORY — parser requirement):
The downstream risk manager extracts the stop price from invalidation_conditions via regex.
Your invalidation_conditions text MUST contain a numeric price level in one of these forms:
  "$118.50"  |  "below 118"  |  "under 118.50"  |  "118 support"  |  "breaks 118"
The matched price must be within 30% of entry_price (sanity gate).
If you cannot supply a price level from the TECHNICAL LEVELS block, DROP the thesis. Do not propose a trade without a parseable stop — the risk manager will fall back to oversized conviction-based sizing with no stop loss, which is unsafe.

Output fields per trade (ALL required):
- ticker
- direction (LONG or SHORT)
- conviction (HIGH / MEDIUM / LOW)
- sector (which sector ETF this stock belongs to)
- entry_price (MUST match the LIVE PRICES block exactly)
- core_thesis (must reference specific live data points — sectors, EPS revisions, IV, technical setup)
- catalyst (MUST use the upcoming earnings dates from the calendar provided)
- options_structure (MUST use the IV regime and expiry dates provided)
- invalidation_conditions (MUST include specific price level — see INVALIDATION FORMAT above)
- hard_time_limit (calculate as TODAY + N weeks, show the actual ISO date)
- position_size_pct (HIGH=4%, MEDIUM=3%, LOW=2%)

Generate 2-3 ideas. Only where 2+ signals align. Return an empty array if no thesis meets the bar.
Respond ONLY with valid JSON array. No markdown. No preamble."""


# Validator + stop extractor live in their own module so they're unit-testable
# without importing the Anthropic SDK or dotenv.
from analysis_validator import validate_thesis as _validate_thesis  # noqa: F401


def load_data():
    data   = {}
    phase2 = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
    for fname in ['sector_ranking.json', 'institutional_flow.json', 'unusual_whales_flow.json']:
        path = os.path.join(phase2, fname)
        if os.path.exists(path):
            with open(path) as f:
                data[fname.replace('.json', '')] = json.load(f)
    return data


def build_research_prompt(data: dict) -> str:
    parts = []

    if 'sector_ranking' in data:
        sr   = data['sector_ranking']
        top3 = sr.get('top_sectors', [])[:3]
        bot3 = sr.get('bottom_sectors', [])
        parts.append("=== SECTOR RANKING (today, live) ===")
        parts.append("TOP SECTORS (long candidates):")
        for s in top3:
            parts.append(
                f"  #{s['rank']} {s['ticker']} ({s['name']})  "
                f"composite:{s['composite_score']:+.2f}%  "
                f"1w:{s['return_1w']:+.1f}%  1m:{s['return_1m']:+.1f}%  3m:{s['return_3m']:+.1f}%"
            )
        parts.append("WEAK SECTORS (short candidates):")
        for s in bot3:
            parts.append(f"  #{s['rank']} {s['ticker']} ({s['name']})  score:{s['composite_score']:+.2f}%")

    if 'unusual_whales_flow' in data:
        uw   = data['unusual_whales_flow']
        summ = uw.get('summary', {})
        parts.append("\n=== UNUSUAL WHALES FLOW (today — live data) ===")
        dp   = summ.get('top_darkpool_tickers', [])
        fl   = summ.get('top_flow_tickers', [])
        if dp: parts.append(f"Dark pool tickers: {', '.join(dp)}")
        if fl: parts.append(f"Options flow tickers: {', '.join(fl)}")
        top_flow = uw.get('options_flow', [])[:8]
        if top_flow:
            parts.append("Top options flow:")
            for f in top_flow:
                parts.append(
                    f"  {f['ticker']} {f['call_put']} strike:${f['strike']} "
                    f"exp:{f['expiry']} premium:${f['premium_usd']:,} sentiment:{f['sentiment']}"
                )

    if 'institutional_flow' in data:
        inst = data['institutional_flow']
        summ = inst.get('summary', {})
        parts.append(f"\n=== INSTITUTIONAL FILINGS ===")
        parts.append(f"13F filings last 45 days: {summ.get('total_13f', 0)}")
        parts.append(f"Form 4 insider filings last 14 days: {summ.get('total_insider', 0)}")

    return '\n'.join(parts)


def _write_empty_theses(data_dir: str, today_str: str, reason: str, learning_mode: str) -> dict:
    """Write a marker file when we refuse to call the API due to missing data."""
    os.makedirs(data_dir, exist_ok=True)
    output = {
        'generated_at':   datetime.now().isoformat(),
        'generated_date': today_str,
        'model':          'claude-opus-4-7',
        'learning_mode':  learning_mode,
        'data_missing':   reason,
        'theses':         [],
    }
    with open(os.path.join(data_dir, 'trade_theses.json'), 'w') as f:
        json.dump(output, f, indent=2)
    return output


def run(learning_mode: str = 'no_learning', learning_insights: str = ''):
    today_str = datetime.now().strftime('%Y-%m-%d')
    data_dir  = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
    print(f"[Analysis Agent v5] {today_str} | Mode: {learning_mode}")

    data = load_data()
    if not data:
        print("[Analysis Agent v5] No research data found. Run Phase 2 pipeline first.")
        return _write_empty_theses(data_dir, today_str,
                                   "research data missing (sector_ranking, etc.)", learning_mode)

    research_prompt = build_research_prompt(data)

    # ── Candidates ─────────────────────────────────────────────────────
    candidates = []
    if LIVE_DATA_AVAILABLE:
        sector_data = data.get('sector_ranking', {})
        uw_data     = data.get('unusual_whales_flow', {})
        candidates  = extract_candidate_tickers(sector_data, uw_data)

    if not candidates:
        msg = "no candidate tickers extracted from sector + flow data"
        print(f"[Analysis Agent v5] HARD FAIL — {msg}")
        return _write_empty_theses(data_dir, today_str, msg, learning_mode)

    # ── Live data fetches (HARD FAIL if any required source is empty) ──
    if not LIVE_DATA_AVAILABLE:
        msg = "price_fetcher / earnings_calendar modules unavailable — cannot generate fact-grounded theses"
        print(f"[Analysis Agent v5] HARD FAIL — {msg}")
        return _write_empty_theses(data_dir, today_str, msg, learning_mode)

    print(f"[Analysis Agent v5] Fetching live prices for {len(candidates)} tickers...")
    prices = fetch_prices(candidates)
    priced = {k: v for k, v in (prices or {}).items() if v and v.get('price')}
    if not priced:
        msg = f"IBKR + yfinance both returned no prices for any of {candidates[:5]}..."
        print(f"[Analysis Agent v5] HARD FAIL — {msg}")
        return _write_empty_theses(data_dir, today_str, msg, learning_mode)
    live_prices_block = format_prices_for_prompt(priced)

    print("[Analysis Agent v5] Fetching earnings calendar...")
    calendar             = get_earnings_calendar(45)
    earnings_block       = format_earnings_for_prompt(calendar, candidates)
    options_expiry_block = format_options_expiry_dates()

    iv_block = estimates_block = technicals_block = ""
    if IV_DATA_AVAILABLE:
        print(f"[Analysis Agent v5] Fetching live IV for {len(candidates[:10])} tickers...")
        iv_block = format_iv_for_prompt(fetch_iv_data(candidates[:10]))
    if ESTIMATES_AVAILABLE:
        print(f"[Analysis Agent v5] Fetching analyst EPS estimates...")
        estimates_block = format_estimates_for_prompt(fetch_analyst_estimates(candidates[:10]))
    if TECHNICALS_AVAILABLE:
        print(f"[Analysis Agent v5] Fetching technical levels...")
        technicals_block = format_levels_for_prompt(fetch_technical_levels(candidates[:10]))

    # ── Build full prompt ──────────────────────────────────────────────
    full_prompt = f"""TODAY'S DATE: {today_str}
IMPORTANT: Use this date for ALL calculations. Do not use any dates from your training memory.

{live_prices_block}

{options_expiry_block}

{earnings_block}

{iv_block}

{estimates_block}

{technicals_block}

=== MARKET RESEARCH (live data fetched today) ===
{research_prompt}
{learning_insights}

Generate 2-3 high-conviction ITPM trade theses as a JSON array.

REQUIREMENTS:
- entry_price MUST come from LIVE PRICES section (exact match to that ticker's price)
- Stop price in invalidation_conditions MUST be a numeric level parseable as "$X" or "below X" — from TECHNICAL LEVELS
- Options structure MUST use IV regime from IV DATA section
- Catalyst MUST reference real earnings dates from EARNINGS CALENDAR section
- Options expiry MUST be from OPTIONS EXPIRY WINDOW section
- All dates MUST be calculated from TODAY'S DATE
- Reference revision direction from ANALYST ESTIMATES section in your thesis text
- If you cannot satisfy ALL of the above for a candidate, OMIT that thesis. Returning an empty array [] is acceptable."""

    print("[Analysis Agent v5] Calling Claude API (system prompt cached)...")
    try:
        msg = client.messages.create(
            model='claude-opus-4-7',
            max_tokens=3000,
            system=[{
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{'role': 'user', 'content': full_prompt}],
        )
        raw = msg.content[0].text.strip()

        try:
            theses = json.loads(raw)
        except json.JSONDecodeError:
            match  = re.search(r'\[.*\]', raw, re.DOTALL)
            theses = json.loads(match.group()) if match else [{'raw_output': raw}]

        # ── POST-GENERATION VALIDATION (drop hallucinated theses) ──
        validated, rejected = [], []
        for t in theses:
            ok, reason = _validate_thesis(t, priced)
            if ok:
                t['learning_mode']  = learning_mode
                t['generated_date'] = today_str
                validated.append(t)
            else:
                rejected.append({'thesis': t, 'rejection_reason': reason})
                print(f"  ✗ REJECTED: {reason}")

        ts     = datetime.now().strftime('%Y%m%d_%H%M')
        output = {
            'generated_at':   datetime.now().isoformat(),
            'generated_date': today_str,
            'model':          'claude-opus-4-7',
            'learning_mode':  learning_mode,
            'data_sources_used': {
                'live_prices':       True,
                'earnings_calendar': True,
                'iv_data':           IV_DATA_AVAILABLE,
                'analyst_estimates': ESTIMATES_AVAILABLE,
                'technical_levels':  TECHNICALS_AVAILABLE,
            },
            'theses':                validated,
            'rejected_by_validator': rejected,
            'research_snapshot': {
                'top_sectors':      data.get('sector_ranking', {}).get('top_sectors', [])[:3],
                'darkpool_tickers': data.get('unusual_whales_flow', {}).get('summary', {}).get('top_darkpool_tickers', []),
                'flow_tickers':     data.get('unusual_whales_flow', {}).get('summary', {}).get('top_flow_tickers', []),
            },
        }

        os.makedirs(data_dir, exist_ok=True)
        with open(os.path.join(data_dir, 'trade_theses.json'), 'w') as f:
            json.dump(output, f, indent=2)
        with open(os.path.join(data_dir, f'trade_theses_{ts}.json'), 'w') as f:
            json.dump(output, f, indent=2)

        print(f"\n[Analysis Agent v5] Complete — {len(validated)} validated theses, {len(rejected)} rejected")
        for i, t in enumerate(validated):
            ep = t.get('entry_price', '?')
            print(f"  {i+1}. {t.get('ticker')} {t.get('direction')} [{t.get('conviction')}] entry:${ep}")

        return output

    except Exception as e:
        print(f"[Analysis Agent v5] ERROR: {e}")
        return _write_empty_theses(data_dir, today_str, f"API call failed: {e}", learning_mode)


if __name__ == '__main__':
    run()
