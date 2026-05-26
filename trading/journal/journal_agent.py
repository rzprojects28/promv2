"""
Prometheus — Journal Agent
Runs weekly. Reviews all closed trades, generates structured reviews via Claude,
extracts winning/losing patterns, and saves performance statistics.
Output: data/trade_journal.json  +  data/performance_stats.json
"""
import json
import os
import sys
from datetime import datetime
import anthropic
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.expanduser('~/promv2/.env'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import telegram_alerts as tg

claude = anthropic.Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'prometheus_config.json')


def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)


def review_trade_with_claude(trade):
    """
    Generate a structured review of a single closed trade using Claude.

    Scope: text-quality and PnL-shape judgments only. We do NOT ask the AI
    to evaluate whether the real-world catalyst fired — there is no
    post-trade market data injected here, so any such judgement would be a
    guess from training memory.
    """
    system_prompt = (
        "You are the Journal Agent for Prometheus, an AI prop trading system. "
        "Critically: you have ONLY the trade record in front of you. You do NOT have "
        "post-trade news, sector data, earnings results, or charts. Restrict your "
        "judgments to things you can derive from the record itself (PnL sign and "
        "magnitude, text quality of the thesis/invalidation, exit reason wording). "
        "Do NOT use training memory to infer what happened in the market. "
        "For anything outside what the record supports, use UNKNOWN."
    )

    prompt = f"""Review this closed paper trade. Return only valid JSON.

TRADE RECORD (this is everything you have — do not infer beyond it):
Ticker:       {trade.get('ticker')}
Direction:    {trade.get('direction')}
Conviction:   {trade.get('conviction')}
Sector:       {trade.get('sector')}
Entry date:   {trade.get('entry_date')}
Exit date:    {trade.get('exit_date')}
Entry price:  ${trade.get('entry_price')}
Exit price:   ${trade.get('exit_price')}
P&L:          {trade.get('pnl_pct')}%
Exit reason:  {trade.get('exit_reason')}

ORIGINAL THESIS (verbatim):
{trade.get('core_thesis', 'Not available')}

CATALYST (verbatim, as set at entry — we do NOT have post-trade verification):
{trade.get('catalyst', 'Not available')}

INVALIDATION CONDITIONS (verbatim):
{trade.get('invalidation_conditions', 'Not available')}

TASK:
1. thesis_accurate — derive from PnL sign + direction. (YES if PnL agrees with direction, NO if opposite, PARTIAL if marginal)
2. invalidation_quality — judge the TEXT of the invalidation condition. (CLEAR if it contains a specific price level; VAGUE if narrative-only; NOT_APPLICABLE if missing)
3. verdict — bucket the outcome by PnL magnitude. (STRONG_WIN >5%, WEAK_WIN 0-5%, WEAK_LOSS -5-0%, STRONG_LOSS <-5%; NEUTRAL only if flat)
4. key_lesson — ONE sentence drawn from the exit_reason and PnL. Do not invent a market narrative.
5. pattern_tags — choose ONLY from this fixed set:
   sector_leadership, earnings_catalyst, dark_pool_confirmation,
   institutional_accumulation, high_conviction_win, high_conviction_loss,
   medium_conviction_win, medium_conviction_loss, early_exit, late_exit,
   good_timing, bad_timing, thesis_too_vague, thesis_well_defined,
   stop_worked, stop_missed

Respond ONLY with valid JSON, no markdown:
{{
  "thesis_accurate": "YES" or "NO" or "PARTIAL",
  "invalidation_quality": "CLEAR" or "VAGUE" or "NOT_APPLICABLE",
  "verdict": "STRONG_WIN" or "WEAK_WIN" or "NEUTRAL" or "WEAK_LOSS" or "STRONG_LOSS",
  "key_lesson": "one sentence maximum",
  "what_worked": "one sentence or null",
  "what_failed": "one sentence or null",
  "pattern_tags": ["tag1", "tag2"],
  "repeat_this_setup": true or false
}}"""

    try:
        msg = claude.messages.create(
            model='claude-opus-4-7',
            max_tokens=500,
            system=system_prompt,
            messages=[{'role': 'user', 'content': prompt}],
        )
        return json.loads(msg.content[0].text.strip())
    except Exception as e:
        print(f"    Claude review error: {e}")
        return {
            'thesis_accurate': 'UNKNOWN',
            'invalidation_quality': 'UNKNOWN',
            'verdict': 'NEUTRAL',
            'key_lesson': f'Review failed: {e}',
            'what_worked': None,
            'what_failed': None,
            'pattern_tags': [],
            'repeat_this_setup': False
        }


def calculate_performance_stats(journal_entries):
    """Calculate aggregate performance statistics from all journal entries"""
    if not journal_entries:
        return {}

    stats = {
        'generated_at': datetime.now().isoformat(),
        'total_reviewed': len(journal_entries),
        'by_conviction': {},
        'by_sector': {},
        'by_direction': {},
        'by_learning_mode': {},
        'winning_patterns': {},
        'losing_patterns': {},
        'ab_comparison': {},
        'top_lessons': [],
        'repeat_setups': [],
    }

    for entry in journal_entries:
        pnl    = float(entry.get('pnl_pct', 0))
        conv   = entry.get('conviction', 'UNKNOWN')
        sector = entry.get('sector', 'UNKNOWN').split('(')[0].strip()
        direc  = entry.get('direction', 'UNKNOWN')
        mode   = entry.get('learning_mode', 'no_learning')
        tags   = entry.get('review', {}).get('pattern_tags', [])
        win    = pnl > 0

        # By conviction
        if conv not in stats['by_conviction']:
            stats['by_conviction'][conv] = {'trades':0,'wins':0,'total_pnl':0,'avg_pnl':0}
        stats['by_conviction'][conv]['trades'] += 1
        stats['by_conviction'][conv]['total_pnl'] += pnl
        if win: stats['by_conviction'][conv]['wins'] += 1

        # By sector
        if sector not in stats['by_sector']:
            stats['by_sector'][sector] = {'trades':0,'wins':0,'total_pnl':0,'avg_pnl':0}
        stats['by_sector'][sector]['trades'] += 1
        stats['by_sector'][sector]['total_pnl'] += pnl
        if win: stats['by_sector'][sector]['wins'] += 1

        # By direction
        if direc not in stats['by_direction']:
            stats['by_direction'][direc] = {'trades':0,'wins':0,'total_pnl':0}
        stats['by_direction'][direc]['trades'] += 1
        stats['by_direction'][direc]['total_pnl'] += pnl
        if win: stats['by_direction'][direc]['wins'] += 1

        # By learning mode (A/B test)
        if mode not in stats['by_learning_mode']:
            stats['by_learning_mode'][mode] = {'trades':0,'wins':0,'total_pnl':0,'avg_pnl':0}
        stats['by_learning_mode'][mode]['trades'] += 1
        stats['by_learning_mode'][mode]['total_pnl'] += pnl
        if win: stats['by_learning_mode'][mode]['wins'] += 1

        # Pattern tracking
        for tag in tags:
            bucket = stats['winning_patterns'] if win else stats['losing_patterns']
            bucket[tag] = bucket.get(tag, 0) + 1

    # Calculate averages
    for group in [stats['by_conviction'], stats['by_sector'],
                  stats['by_direction'], stats['by_learning_mode']]:
        for key, data in group.items():
            t = data['trades']
            data['win_rate']  = round(data['wins'] / t * 100, 1) if t else 0
            data['avg_pnl']   = round(data['total_pnl'] / t, 2) if t else 0
            data['total_pnl'] = round(data['total_pnl'], 2)

    # A/B comparison summary
    bl = stats['by_learning_mode'].get('no_learning', {})
    lm = stats['by_learning_mode'].get('with_learning', {})
    if bl and lm:
        stats['ab_comparison'] = {
            'baseline_win_rate':  bl.get('win_rate', 0),
            'learning_win_rate':  lm.get('win_rate', 0),
            'baseline_avg_pnl':   bl.get('avg_pnl', 0),
            'learning_avg_pnl':   lm.get('avg_pnl', 0),
            'win_rate_delta':     round(lm.get('win_rate',0) - bl.get('win_rate',0), 1),
            'avg_pnl_delta':      round(lm.get('avg_pnl',0) - bl.get('avg_pnl',0), 2),
            'verdict':            'LEARNING_BETTER' if lm.get('win_rate',0) > bl.get('win_rate',0) else 'BASELINE_BETTER' if bl.get('win_rate',0) > lm.get('win_rate',0) else 'INCONCLUSIVE'
        }

    # Top lessons
    stats['top_lessons'] = list(set(
        e.get('review', {}).get('key_lesson', '')
        for e in journal_entries
        if e.get('review', {}).get('key_lesson')
    ))[:10]

    # Repeat setups
    stats['repeat_setups'] = [
        {'ticker': e.get('ticker'), 'tags': e.get('review', {}).get('pattern_tags', [])}
        for e in journal_entries
        if e.get('review', {}).get('repeat_this_setup')
    ]

    return stats


def run():
    print("[Journal Agent] Starting weekly trade review...")

    config        = load_json(CONFIG_PATH, {})
    closed_trades = load_json('data/closed_positions.json', [])
    existing_journal = load_json('data/trade_journal.json', [])

    if not closed_trades:
        print("[Journal Agent] No closed trades to review yet.")
        return

    # Find trades not yet reviewed
    reviewed_ids = {e.get('trade_id') for e in existing_journal}
    new_trades   = [
        t for t in closed_trades
        if f"{t.get('ticker')}_{t.get('entry_date')}" not in reviewed_ids
    ]

    if not new_trades:
        print(f"[Journal Agent] All {len(closed_trades)} trades already reviewed.")
    else:
        print(f"[Journal Agent] {len(new_trades)} new trade(s) to review...")

    journal = list(existing_journal)

    for trade in new_trades:
        ticker     = trade.get('ticker', '')
        entry_date = trade.get('entry_date', '')
        pnl        = float(trade.get('pnl_pct', 0))
        trade_id   = f"{ticker}_{entry_date}"

        print(f"\n  Reviewing {ticker} ({'+' if pnl >= 0 else ''}{pnl}%)...")
        review = review_trade_with_claude(trade)

        entry = {
            'trade_id':      trade_id,
            'reviewed_at':   datetime.now().isoformat(),
            'ticker':        ticker,
            'direction':     trade.get('direction'),
            'conviction':    trade.get('conviction'),
            'sector':        trade.get('sector'),
            'entry_date':    entry_date,
            'exit_date':     trade.get('exit_date'),
            'pnl_pct':       pnl,
            'exit_reason':   trade.get('exit_reason'),
            'learning_mode': trade.get('learning_mode', 'no_learning'),
            'review':        review,
        }
        journal.append(entry)

        verdict = review.get('verdict', '')
        lesson  = review.get('key_lesson', '')
        print(f"    Verdict: {verdict}")
        print(f"    Lesson:  {lesson}")

    # Calculate stats
    print("\n[Journal Agent] Calculating performance statistics...")
    stats = calculate_performance_stats(journal)

    # Save
    os.makedirs('data', exist_ok=True)
    save_json('data/trade_journal.json', journal)
    save_json('data/performance_stats.json', stats)

    # Update config last run time
    config['journal']['last_run'] = datetime.now().strftime('%Y-%m-%d')
    save_json(CONFIG_PATH, config)

    # Telegram summary
    ab = stats.get('ab_comparison', {})
    conv_summary = '\n'.join(
        f"  {k}: {v['trades']} trades | win rate {v['win_rate']}% | avg {v['avg_pnl']:+.1f}%"
        for k, v in stats.get('by_conviction', {}).items()
    )
    ab_summary = ''
    if ab:
        ab_summary = (
            f"\n\nA/B TEST:\n"
            f"  Baseline (no learning): {stats['by_learning_mode'].get('no_learning',{}).get('win_rate',0)}% win rate\n"
            f"  With learning: {stats['by_learning_mode'].get('with_learning',{}).get('win_rate',0)}% win rate\n"
            f"  Verdict: {ab.get('verdict','INCONCLUSIVE')}"
        )

    tg.send(
        f"📓 <b>PROMETHEUS WEEKLY JOURNAL</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Trades reviewed: {len(journal)}\n"
        f"New reviews: {len(new_trades)}\n\n"
        f"<b>BY CONVICTION:</b>\n{conv_summary}"
        f"{ab_summary}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>Full data: data/performance_stats.json</i>"
    )

    print(f"\n[Journal Agent] Complete.")
    print(f"  {len(journal)} total reviews saved → data/trade_journal.json")
    print(f"  Performance stats → data/performance_stats.json")
    if ab:
        print(f"  A/B verdict: {ab.get('verdict', 'INCONCLUSIVE')}")
    return {'journal': journal, 'stats': stats}


if __name__ == '__main__':
    run()
