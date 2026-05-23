"""
Plotus Daily Report Generator
Runs on the Trading VPS at 4:30am SGT (8:30pm UTC previous day = after US market close)
Generates a structured daily report and pushes to Google Drive as a text file.
The Content VPS Hermes picks this up at 5am SGT to generate Instagram posts.
"""
import json
import os
import sys
from datetime import datetime, timedelta
from dotenv import load_dotenv
import anthropic
import subprocess

load_dotenv(dotenv_path=os.path.expanduser('~/prometheus/.env'))

BASE_DIR   = os.path.expanduser('~/prometheus')
PHASE2_DIR = os.path.join(BASE_DIR, 'phase2/data')
ACCT_A_DIR = os.path.join(BASE_DIR, 'account_a/data')
ACCT_B_DIR = os.path.join(BASE_DIR, 'account_b/data')
REPORTS_DIR = os.path.join(BASE_DIR, 'reports')
GDRIVE_PATH = 'gdrive:AI Trading/Plotus/Daily Reports'

claude = anthropic.Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))


def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def get_all_data():
    sectors   = load_json(os.path.join(PHASE2_DIR, 'sector_ranking.json'), {})
    theses    = load_json(os.path.join(PHASE2_DIR, 'trade_theses.json'), {})
    uw        = load_json(os.path.join(PHASE2_DIR, 'unusual_whales_flow.json'), {})
    open_a    = load_json(os.path.join(ACCT_A_DIR, 'open_positions.json'), [])
    closed_a  = load_json(os.path.join(ACCT_A_DIR, 'closed_positions.json'), [])
    open_b    = load_json(os.path.join(ACCT_B_DIR, 'open_positions.json'), [])
    closed_b  = load_json(os.path.join(ACCT_B_DIR, 'closed_positions.json'), [])
    journal_a = load_json(os.path.join(ACCT_A_DIR, 'trade_journal.json'), [])
    journal_b = load_json(os.path.join(ACCT_B_DIR, 'trade_journal.json'), [])

    all_closed = closed_a + closed_b
    all_open   = open_a + open_b
    wins       = [p for p in all_closed if float(p.get('pnl_pct', 0)) > 0]
    avg_pnl    = sum(float(p.get('pnl_pct', 0)) for p in all_closed) / len(all_closed) if all_closed else 0

    # A/B stats
    def acct_stats(closed):
        if not closed:
            return {'trades': 0, 'win_rate': 0, 'avg_pnl': 0}
        w  = [p for p in closed if float(p.get('pnl_pct', 0)) > 0]
        ap = sum(float(p.get('pnl_pct', 0)) for p in closed) / len(closed)
        return {
            'trades':   len(closed),
            'win_rate': round(len(w) / len(closed) * 100, 1),
            'avg_pnl':  round(ap, 2)
        }

    # Trades opened/closed today
    today = datetime.now().strftime('%Y-%m-%d')
    opened_today = [p for p in all_open   if p.get('entry_date', '') == today]
    closed_today = [p for p in all_closed if p.get('exit_date', '')  == today]

    return {
        'sectors':       sectors.get('all_sectors', []),
        'top_sectors':   sectors.get('top_sectors', [])[:3],
        'bottom_sector': sectors.get('bottom_sectors', [{}])[0] if sectors.get('bottom_sectors') else {},
        'theses':        theses.get('theses', []),
        'uw_flow':       uw.get('summary', {}),
        'open':          all_open,
        'open_a':        open_a,
        'open_b':        open_b,
        'closed':        all_closed,
        'opened_today':  opened_today,
        'closed_today':  closed_today,
        'journal':       journal_a + journal_b,
        'stats_a':       acct_stats(closed_a),
        'stats_b':       acct_stats(closed_b),
        'overall': {
            'total_trades': len(all_open) + len(all_closed),
            'win_rate':     round(len(wins) / len(all_closed) * 100, 1) if all_closed else 0,
            'avg_pnl':      round(avg_pnl, 2),
            'open_count':   len(all_open),
        }
    }


def generate_market_summary(data):
    """Use Claude to write the market narrative for today"""
    sectors = data['top_sectors']
    bottom  = data['bottom_sector']
    uw      = data['uw_flow']
    dp      = ', '.join(uw.get('top_darkpool_tickers', [])) or 'none detected'
    flow    = ', '.join(uw.get('top_flow_tickers', []))    or 'none detected'

    prompt = f"""You are writing the market summary section of a daily trading report for Plotus, an AI trading system documenting its paper trading experiment.

Today's data:
TOP SECTORS:
{chr(10).join(f"  #{s['rank']} {s['ticker']} ({s['name']}) composite: {s['composite_score']:+.2f}%  1w:{s['return_1w']:+.1f}%  1m:{s['return_1m']:+.1f}%  3m:{s['return_3m']:+.1f}%" for s in sectors)}

WEAKEST SECTOR: {bottom.get('ticker','?')} ({bottom.get('name','?')}) {bottom.get('composite_score',0):+.2f}%

UNUSUAL FLOW TODAY:
Dark pool activity: {dp}
Options flow activity: {flow}
Large dark pool prints (>$1M): {uw.get('large_darkpool_prints', 0)}
Significant options flow (>$500k): {uw.get('significant_options_flow', 0)}

Write 3-4 sentences covering:
1. Which sectors are showing strength and what that signals about market sentiment
2. Notable dark pool or options flow observations
3. What the sector rotation pattern suggests for the next session

Tone: professional, data-driven, intermediate-to-advanced audience. No hype. No ITPM references.
Write in plain text, no markdown. First person plural ("we", "the system")."""

    try:
        msg = claude.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=10000,
            messages=[{'role': 'user', 'content': prompt}]
        )
        return msg.content[0].text.strip()
    except Exception as e:
        return f"Market data processed. Top sector: {sectors[0]['ticker']} ({sectors[0]['name']}) at {sectors[0]['composite_score']:+.2f}%."


def generate_watchlist(data):
    """Use Claude to generate next day watchlist"""
    sectors  = data['top_sectors']
    open_pos = data['open']
    theses   = data['theses']

    prompt = f"""You are generating the next trading day watchlist for Plotus, an AI trading system.

CURRENT CONTEXT:
Top sectors: {', '.join(f"{s['ticker']} ({s['name']})" for s in sectors)}
Open positions: {', '.join(f"{p.get('ticker')} {p.get('direction')}" for p in open_pos) or 'None'}
Today's thesis tickers considered: {', '.join(t.get('ticker','') for t in theses)}

Generate a watchlist of 3-4 items for the next trading session.
For each item include:
- Ticker and what to watch for
- Why it's on the radar (sector leadership, flow, upcoming catalyst)
- What signal would confirm a trade setup

Format as a simple numbered list. Plain text, no markdown symbols.
Professional tone. No ITPM references. No buy/sell recommendations."""

    try:
        msg = claude.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=10000,
            messages=[{'role': 'user', 'content': prompt}]
        )
        return msg.content[0].text.strip()
    except Exception as e:
        return "Watchlist generation failed — check data files."


def generate_content_angles(data):
    """Suggest 3 content angles for today's Instagram posts"""
    opened  = data['opened_today']
    closed  = data['closed_today']
    sectors = data['top_sectors']
    stats   = data['overall']

    prompt = f"""You are the content strategist for Plotus, an AI trading experiment Instagram account.

TODAY'S PLOTUS ACTIVITY:
Trades opened today: {len(opened)} — {', '.join(f"{p.get('ticker')} {p.get('direction')}" for p in opened) or 'none'}
Trades closed today: {len(closed)} — {', '.join(f"{p.get('ticker')} {p.get('direction')} {p.get('pnl_pct',0):+.1f}%" for p in closed) or 'none'}
Open positions: {stats['open_count']}
Overall win rate: {stats['win_rate']}%
Top sector: {sectors[0]['ticker']} ({sectors[0]['name']}) {sectors[0]['composite_score']:+.2f}% if sectors else 'N/A'

CONTENT MIX RULE: 80% documentary (experiment updates, trade logs, system insights), 20% educational (Plotus Method concepts, options education, sector rotation).

Suggest exactly 3 Instagram post angles for today. Each should be different.
Choose from these types:
- Market brief (what moved today, sector flows)
- Trade log (entry or exit breakdown with reasoning)
- Experiment update (A/B test progress, win rate milestone, system observation)
- Behind the system (how a specific part of Plotus works)
- Plotus Method concept (one principle explained with today's real example)
- Options education (IV, DTE, structure — tied to today's data)
- Sector rotation (how to read the rankings, what it means)

For each post give:
POST 1:
Type: [type]
Hook: [opening line that stops the scroll]
Key points: [3 bullet points of what the post covers]
Content source: [which data from today's report feeds this post]

Repeat for POST 2 and POST 3.

IMPORTANT: Never mention ITPM, Anton Kreil, or any third party methodology. Use "Plotus Method" for any methodology references."""

    try:
        msg = claude.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=10000,
            messages=[{'role': 'user', 'content': prompt}]
        )
        return msg.content[0].text.strip()
    except Exception as e:
        return "Content angle generation failed."


def build_report(data, market_summary, watchlist, content_angles):
    """Assemble the full daily report document"""
    today    = datetime.now().strftime('%A, %d %B %Y')
    now_sgt  = datetime.now().strftime('%H:%M SGT')

    # Sector rankings
    sector_lines = []
    for s in data['sectors'][:11]:
        bar     = '▓' * int(abs(s['composite_score']) / 2) if s['composite_score'] else ''
        color   = '+' if s['composite_score'] >= 0 else ''
        sector_lines.append(
            f"  #{s['rank']:2} {s['ticker']:<5} {s['name']:<25} "
            f"{color}{s['composite_score']:.2f}%  {bar}"
        )

    # Open positions
    open_lines = []
    for p in data['open']:
        days = (datetime.now() - datetime.strptime(
            p.get('entry_date', datetime.now().strftime('%Y-%m-%d')), '%Y-%m-%d')).days
        acct = 'A' if 'A_' in p.get('account', '') else 'B' if 'B_' in p.get('account', '') else '?'
        open_lines.append(
            f"  [{acct}] {p.get('ticker'):<6} {p.get('direction'):<5} "
            f"[{p.get('conviction')}]  Entry ${p.get('entry_price')}  "
            f"Day {days}  Deadline: {p.get('deadline_date','?')}"
        )

    # Trades today
    opened_lines = [
        f"  OPENED: {p.get('ticker')} {p.get('direction')} [{p.get('conviction')}] "
        f"@ ${p.get('entry_price')} — {p.get('core_thesis','')[:100]}"
        for p in data['opened_today']
    ] or ['  No new positions opened today']

    closed_lines = [
        f"  CLOSED: {p.get('ticker')} {p.get('direction')} "
        f"P&L: {float(p.get('pnl_pct',0)):+.1f}% — {p.get('exit_reason','')[:80]}"
        for p in data['closed_today']
    ] or ['  No positions closed today']

    stats    = data['overall']
    stats_a  = data['stats_a']
    stats_b  = data['stats_b']

    report = f"""
╔══════════════════════════════════════════════════════════════╗
  PLOTUS DAILY REPORT
  {today}
  Generated: {now_sgt}
╚══════════════════════════════════════════════════════════════╝

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 1 — MARKET SUMMARY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{market_summary}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 2 — SECTOR RANKINGS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{chr(10).join(sector_lines)}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 3 — PLOTUS TRADE ACTIVITY TODAY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{chr(10).join(opened_lines)}
{chr(10).join(closed_lines)}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 4 — OPEN POSITIONS ({stats['open_count']} active)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{chr(10).join(open_lines) if open_lines else '  No open positions'}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 5 — PERFORMANCE SNAPSHOT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OVERALL
  Total trades:  {stats['total_trades']}
  Win rate:      {stats['win_rate']}%
  Avg P&L:       {stats['avg_pnl']:+.2f}%

A/B TEST
  Account A (Baseline):  {stats_a['trades']} trades | {stats_a['win_rate']}% WR | avg {stats_a['avg_pnl']:+.2f}%
  Account B (Learning):  {stats_b['trades']} trades | {stats_b['win_rate']}% WR | avg {stats_b['avg_pnl']:+.2f}%
  Verdict: {'Learning ahead' if stats_b['win_rate'] > stats_a['win_rate'] + 2 else 'Baseline ahead' if stats_a['win_rate'] > stats_b['win_rate'] + 2 else 'Inconclusive — more data needed'}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 6 — NEXT SESSION WATCHLIST
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{watchlist}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 7 — CONTENT ANGLES FOR TODAY'S POSTS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{content_angles}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
END OF REPORT
Paper trading only. Not financial advice.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
    return report.strip()


def push_to_gdrive(report_text, filename):
    """Save report locally then push to Google Drive"""
    os.makedirs(REPORTS_DIR, exist_ok=True)
    local_path = os.path.join(REPORTS_DIR, filename)

    with open(local_path, 'w') as f:
        f.write(report_text)
    print(f"  Report saved locally: {local_path}")

    # Push to Google Drive
    try:
        result = subprocess.run(
            ['rclone', 'copy', local_path, GDRIVE_PATH],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode == 0:
            print(f"  Pushed to Google Drive: {GDRIVE_PATH}/{filename}")
            return True
        else:
            print(f"  Google Drive push failed: {result.stderr}")
            return False
    except Exception as e:
        print(f"  Google Drive push error: {e}")
        return False


def run():
    print("=" * 60)
    print(f"  PLOTUS DAILY REPORT GENERATOR")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M SGT')}")
    print("=" * 60)

    print("\n[1/4] Loading Plotus data...")
    data = get_all_data()
    print(f"  Sectors: {len(data['sectors'])} | Open: {len(data['open'])} | "
          f"Closed: {len(data['closed'])} | "
          f"Opened today: {len(data['opened_today'])} | Closed today: {len(data['closed_today'])}")

    print("\n[2/4] Generating market summary...")
    market_summary = generate_market_summary(data)
    print("  Done")

    print("\n[3/4] Generating watchlist and content angles...")
    watchlist      = generate_watchlist(data)
    content_angles = generate_content_angles(data)
    print("  Done")

    print("\n[4/4] Assembling and pushing report...")
    date_str  = datetime.now().strftime('%Y-%m-%d')
    filename  = f"plotus_daily_{date_str}.txt"
    report    = build_report(data, market_summary, watchlist, content_angles)
    pushed    = push_to_gdrive(report, filename)

    print(f"\n{'=' * 60}")
    print(f"  REPORT COMPLETE")
    print(f"  File: {filename}")
    print(f"  Google Drive: {'✓ Pushed' if pushed else '✗ Failed — check rclone'}")
    print(f"  Content VPS will pick this up at 5am SGT")
    print(f"{'=' * 60}\n")

    return report


if __name__ == '__main__':
    run()
