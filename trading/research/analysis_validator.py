"""
Pure-function validator for analysis_agent output.

Lives in its own module so unit tests don't need to import the Anthropic SDK
or dotenv. analysis_agent.py imports from here.

NOTE: the _STOP_PATTERNS regex set is intentionally mirrored from
trading/risk/risk_manager.extract_stop_price — if you change one, change both.
The downstream risk manager is the source of truth for sizing logic; this
validator just confirms what the AI produced is parseable.
"""
import re
from datetime import date
from typing import Optional


_STOP_PATTERNS = [
    r'\$(\d+\.?\d*)',          # $118 or $118.50
    r'below\s+(\d+\.?\d*)',    # below 118
    r'under\s+(\d+\.?\d*)',    # under 118
    r'(\d+\.?\d*)\s+support',  # 118 support
    r'breaks?\s+(\d+\.?\d*)',  # breaks 118
]

_BLOCKED_ETFS = frozenset({
    'XLK', 'XLF', 'XLV', 'XLE', 'XLI', 'XLB', 'XLU', 'XLRE',
    'XLY', 'XLP', 'XLC',
    'QQQ', 'SPY', 'IWM', 'DIA', 'VTI',
})


def extract_stop_price(invalidation_text: str, entry_price: float) -> Optional[float]:
    """Return a parseable stop price within 30% of entry, or None."""
    if not invalidation_text or not entry_price:
        return None
    candidates = []
    for pattern in _STOP_PATTERNS:
        for m in re.findall(pattern, str(invalidation_text), re.IGNORECASE):
            try:
                price = float(m)
                if 0.7 * entry_price <= price <= 1.3 * entry_price:
                    candidates.append(price)
            except ValueError:
                continue
    return min(candidates, key=lambda x: abs(x - entry_price)) if candidates else None


def validate_thesis(thesis: dict, prices: dict, today: Optional[date] = None) -> tuple[bool, str]:
    """
    Validate a Claude-generated thesis against the live data we actually fetched.
    Returns (is_valid, reason). Drop the thesis if is_valid is False.

    For options-instrument theses, also validates:
      - options_structure parses cleanly via options_builder
      - structure type is not in the blocked set (naked shorts, iron condors, ...)
      - expiry DTE falls in the allowed window for the structure
      - reward_to_risk (if provided by the model) is >= 2.0
    """
    if not isinstance(thesis, dict):
        return False, "thesis is not a dict"

    ticker = (thesis.get('ticker') or '').upper().strip()
    if not ticker:
        return False, "missing ticker"

    if ticker in _BLOCKED_ETFS:
        return False, f"{ticker} is a sector or market ETF — single-name only"

    try:
        entry = float(thesis.get('entry_price', 0))
    except (TypeError, ValueError):
        return False, f"entry_price not numeric for {ticker}"
    if entry <= 0:
        return False, f"entry_price is 0 or negative for {ticker}"

    live_data  = prices.get(ticker) or prices.get(ticker.lower())
    live_price = (live_data or {}).get('price') if live_data else None
    if not live_price:
        return False, f"{ticker} not present in LIVE PRICES fetch"

    drift_pct = abs(entry - live_price) / live_price * 100
    if drift_pct > 1.0:
        return False, (f"{ticker} entry_price ${entry:.2f} drifts "
                       f"{drift_pct:.2f}% from live ${live_price:.2f}")

    inv  = thesis.get('invalidation_conditions', '')
    stop = extract_stop_price(inv, entry)
    if stop is None:
        snippet = (inv or '')[:80]
        return False, f"{ticker} no parseable stop in invalidation_conditions: {snippet!r}"

    # ── Options-specific validation ────────────────────────────────────────
    instrument = (thesis.get('instrument') or 'stock').lower()
    if instrument == 'options':
        ok, reason = _validate_options_structure(thesis, ticker, today)
        if not ok:
            return False, reason

    return True, "ok"


def _validate_options_structure(thesis: dict, ticker: str,
                                today: Optional[date]) -> tuple[bool, str]:
    """
    Validate the options_structure JSON object embedded in an options thesis.
    Delegates structure parsing to options_builder and adds the
    reward-to-risk gate that's specific to ITPM doctrine.
    """
    import os, sys
    # Allow this module to find options_builder regardless of how it was imported.
    _here = os.path.dirname(os.path.abspath(__file__))
    _exec = os.path.join(os.path.dirname(_here), 'execution')
    if _exec not in sys.path:
        sys.path.insert(0, _exec)
    from options_builder import parse_options_structure, check_dte_bounds

    structure_dict = thesis.get('options_structure')
    if not structure_dict:
        return False, f"{ticker} instrument=options but options_structure missing"

    if not isinstance(structure_dict, dict):
        return False, (f"{ticker} options_structure must be a dict, "
                       f"got {type(structure_dict).__name__}")

    # If underlying not specified, inject from ticker
    structure_dict.setdefault('underlying', ticker)

    if (structure_dict.get('underlying') or '').upper() != ticker:
        return False, (f"{ticker} options_structure.underlying mismatch "
                       f"({structure_dict.get('underlying')!r})")

    try:
        structure = parse_options_structure(structure_dict)
    except ValueError as e:
        return False, f"{ticker} options_structure invalid: {e}"

    ok, reason = check_dte_bounds(structure, today)
    if not ok:
        return False, f"{ticker} {reason}"

    # Reward-to-risk gate (ITPM 3:1 target; we accept 2.0+ as floor)
    rr = thesis.get('reward_to_risk')
    if rr is not None:
        try:
            rr_f = float(rr)
            if rr_f < 2.0:
                return False, (f"{ticker} reward_to_risk {rr_f:.2f} below 2.0 "
                               f"(ITPM target 3:1, floor 2:1)")
        except (TypeError, ValueError):
            return False, f"{ticker} reward_to_risk not numeric: {rr!r}"

    return True, "ok"
