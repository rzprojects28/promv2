"""
Prometheus Trading — Research Orchestrator (was phase2/run_research.py).

Runs all 4 research scripts in sequence and generates trade theses.
Each script writes its JSON output to trading/research/data/ (relative to
this file). Called by trading/run.py at the start of each trading day.
"""
import sys
import os
from datetime import datetime
 
# Ensure scripts in same directory are importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
 
 
def run_all():
    start = datetime.now()
    print("=" * 60)
    print(f"  PROMETHEUS RESEARCH RUN — {start.strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)
 
    results = {}
 
    # ── 1: Sector ranking ──────────────────────────────────────
    print("\n[1/4] Sector ranking...")
    try:
        import sector_ranking
        results['sectors'] = sector_ranking.run()
    except Exception as e:
        print(f"  FAILED: {e}")
        results['sectors'] = None
 
    # ── 2: Institutional flow ──────────────────────────────────
    print("\n[2/4] Institutional flow (SEC EDGAR)...")
    try:
        import institutional_flow
        results['institutional'] = institutional_flow.run()
    except Exception as e:
        print(f"  FAILED: {e}")
        results['institutional'] = None
 
    # ── 3: Unusual Whales ─────────────────────────────────────
    print("\n[3/4] Unusual Whales flow...")
    try:
        import unusual_whales
        results['uw_flow'] = unusual_whales.run()
    except Exception as e:
        print(f"  FAILED: {e}")
        results['uw_flow'] = None
 
    # ── 4: Analysis agent ─────────────────────────────────────
    print("\n[4/4] Analysis Agent (Claude API)...")
    try:
        import analysis_agent
        results['theses'] = analysis_agent.run()
    except Exception as e:
        print(f"  FAILED: {e}")
        results['theses'] = None
 
    # ── Summary ───────────────────────────────────────────────
    elapsed = (datetime.now() - start).seconds
    theses  = results.get('theses') or {}
    count   = len(theses.get('theses', [])) if isinstance(theses, dict) else 0
 
    print(f"\n{'=' * 60}")
    print(f"  RESEARCH RUN COMPLETE — {elapsed}s elapsed")
    print(f"  Trade ideas generated: {count}")
    print(f"  Results → ~/promv2/trading/research/data/trade_theses.json")
    print(f"{'=' * 60}\n")
 
 
if __name__ == '__main__':
    run_all()
