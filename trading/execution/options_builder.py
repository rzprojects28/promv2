"""
Options structure parser and IBKR contract builder.

The analysis_agent emits a structured `options_structure` JSON dict like:

    {
      "type": "bull_call_spread",
      "underlying": "AAPL",
      "legs": [
        {"action":"BUY",  "right":"C", "strike":450.0, "expiry":"2026-07-18", "ratio":1},
        {"action":"SELL", "right":"C", "strike":470.0, "expiry":"2026-07-18", "ratio":1}
      ],
      "earnings_catalyst": true
    }

This module:
  - validates the structure against the type's allowed shape
  - converts ISO expiries to IBKR yyyymmdd format
  - builds the list of ib_insync.Option contracts (one per leg)

Pure functions — no ib_insync dependency in normal validation paths. ib_insync
is only imported inside the build_ibkr_contracts() helper, lazily, so unit
tests can exercise everything else without the SDK.

Per ITPM principles (and per the user's explicit spec):
  - Naked short calls/puts are BLOCKED — defined-risk only
  - Iron condors, butterflies, naked straddles/strangles deferred to v2
  - Strip/Strap variants deferred (Anton teaches them but they're rarely
    the workhorse in his actual ITPM Flash trade examples)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional


# ── Allowed structure types ───────────────────────────────────────────────

# Maps canonical type → (n_legs, expected leg signature checker)
# Each checker takes the sorted list of legs and returns (ok, reason).

ALLOWED_TYPES = {
    'long_call',
    'long_put',
    'bull_call_spread',
    'bear_put_spread',
    'bull_put_spread',
    'bear_call_spread',
    'calendar_call_spread',
    'calendar_put_spread',
    'diagonal_call_spread',
    'diagonal_put_spread',
    'calendar_ratio_call_spread',
    'calendar_ratio_put_spread',
    'covered_call',
}

BLOCKED_TYPES = {
    # Defined-risk only — these are undefined risk or not workhorses
    'naked_short_call',
    'naked_short_put',
    'iron_condor',
    'iron_butterfly',
    'short_strangle',
    'short_straddle',
    'long_strangle',
    'long_straddle',
    'butterfly_call_spread',
    'butterfly_put_spread',
    'strip_straddle',
    'strap_strangle',
    'strip_strangle',
}


@dataclass
class OptionLeg:
    action: str       # "BUY" or "SELL"
    right: str        # "C" or "P"
    strike: float
    expiry: str       # ISO date YYYY-MM-DD
    ratio: int = 1    # number of contracts in this leg

    def to_ibkr_expiry(self) -> str:
        """IBKR Option contract wants expiry as YYYYMMDD."""
        return self.expiry.replace('-', '')


@dataclass
class OptionsStructure:
    type: str
    underlying: str
    legs: list[OptionLeg]
    earnings_catalyst: bool = False

    def net_action(self) -> str:
        """Overall direction of the structure: 'debit' or 'credit'."""
        if self.type in {'long_call', 'long_put',
                         'bull_call_spread', 'bear_put_spread',
                         'calendar_call_spread', 'calendar_put_spread',
                         'diagonal_call_spread', 'diagonal_put_spread'}:
            return 'debit'
        if self.type in {'bull_put_spread', 'bear_call_spread', 'covered_call'}:
            return 'credit'
        if self.type in {'calendar_ratio_call_spread', 'calendar_ratio_put_spread'}:
            return 'debit'   # generally net debit at entry
        return 'debit'   # safe default

    def is_single_leg(self) -> bool:
        return self.type in {'long_call', 'long_put'}

    def max_loss_uncapped_long(self) -> bool:
        """True for long-only structures where max loss is premium paid."""
        return self.is_single_leg()


# ── Parse + validate JSON structure ────────────────────────────────────────

def parse_options_structure(d: dict) -> OptionsStructure:
    """
    Parse the JSON dict the analysis_agent emits into an OptionsStructure.
    Raises ValueError with a specific reason on any structural problem.
    """
    if not isinstance(d, dict):
        raise ValueError("options_structure must be a dict")

    t = (d.get('type') or '').strip().lower().replace(' ', '_').replace('-', '_')
    if not t:
        raise ValueError("options_structure.type missing")
    if t in BLOCKED_TYPES:
        raise ValueError(f"options_structure type '{t}' is blocked (defined-risk only)")
    if t not in ALLOWED_TYPES:
        raise ValueError(f"options_structure type '{t}' is unknown; allowed: {sorted(ALLOWED_TYPES)}")

    underlying = (d.get('underlying') or '').upper().strip()
    if not underlying:
        raise ValueError("options_structure.underlying missing")

    raw_legs = d.get('legs') or []
    if not isinstance(raw_legs, list) or not raw_legs:
        raise ValueError("options_structure.legs must be a non-empty list")

    legs = []
    for i, raw in enumerate(raw_legs):
        if not isinstance(raw, dict):
            raise ValueError(f"leg {i} not a dict")
        action = (raw.get('action') or '').upper().strip()
        if action not in ('BUY', 'SELL'):
            raise ValueError(f"leg {i} action must be BUY or SELL (got {action!r})")
        right = (raw.get('right') or '').upper().strip()
        if right not in ('C', 'P'):
            raise ValueError(f"leg {i} right must be C or P (got {right!r})")
        try:
            strike = float(raw.get('strike'))
        except (TypeError, ValueError):
            raise ValueError(f"leg {i} strike not numeric")
        if strike <= 0:
            raise ValueError(f"leg {i} strike must be > 0")
        expiry = (raw.get('expiry') or '').strip()
        try:
            datetime.strptime(expiry, '%Y-%m-%d')
        except ValueError:
            raise ValueError(f"leg {i} expiry must be YYYY-MM-DD (got {expiry!r})")
        ratio = int(raw.get('ratio', 1))
        if ratio < 1:
            raise ValueError(f"leg {i} ratio must be >= 1")
        legs.append(OptionLeg(action=action, right=right, strike=strike,
                              expiry=expiry, ratio=ratio))

    structure = OptionsStructure(
        type=t,
        underlying=underlying,
        legs=legs,
        earnings_catalyst=bool(d.get('earnings_catalyst', False)),
    )

    _validate_shape(structure)
    return structure


def _validate_shape(s: OptionsStructure) -> None:
    """Validate that legs match the declared type's structural requirements."""
    t = s.type
    legs = s.legs

    def err(msg):
        raise ValueError(f"{t}: {msg}")

    if t == 'long_call':
        if len(legs) != 1: err("expected 1 leg")
        l = legs[0]
        if l.action != 'BUY' or l.right != 'C': err("must be BUY C")

    elif t == 'long_put':
        if len(legs) != 1: err("expected 1 leg")
        l = legs[0]
        if l.action != 'BUY' or l.right != 'P': err("must be BUY P")

    elif t == 'bull_call_spread':
        if len(legs) != 2: err("expected 2 legs")
        buy = [l for l in legs if l.action == 'BUY']
        sell = [l for l in legs if l.action == 'SELL']
        if len(buy) != 1 or len(sell) != 1: err("need 1 BUY + 1 SELL")
        if buy[0].right != 'C' or sell[0].right != 'C': err("both legs must be CALL")
        if buy[0].expiry != sell[0].expiry: err("both legs same expiry")
        if buy[0].strike >= sell[0].strike: err("BUY strike must be lower than SELL strike (bullish)")

    elif t == 'bear_put_spread':
        if len(legs) != 2: err("expected 2 legs")
        buy = [l for l in legs if l.action == 'BUY']
        sell = [l for l in legs if l.action == 'SELL']
        if len(buy) != 1 or len(sell) != 1: err("need 1 BUY + 1 SELL")
        if buy[0].right != 'P' or sell[0].right != 'P': err("both legs must be PUT")
        if buy[0].expiry != sell[0].expiry: err("both legs same expiry")
        if buy[0].strike <= sell[0].strike: err("BUY strike must be higher than SELL strike (bearish)")

    elif t == 'bull_put_spread':
        if len(legs) != 2: err("expected 2 legs")
        buy = [l for l in legs if l.action == 'BUY']
        sell = [l for l in legs if l.action == 'SELL']
        if len(buy) != 1 or len(sell) != 1: err("need 1 BUY + 1 SELL")
        if buy[0].right != 'P' or sell[0].right != 'P': err("both legs must be PUT")
        if buy[0].expiry != sell[0].expiry: err("both legs same expiry")
        if sell[0].strike <= buy[0].strike: err("SELL strike must be higher than BUY strike (bullish credit)")

    elif t == 'bear_call_spread':
        if len(legs) != 2: err("expected 2 legs")
        buy = [l for l in legs if l.action == 'BUY']
        sell = [l for l in legs if l.action == 'SELL']
        if len(buy) != 1 or len(sell) != 1: err("need 1 BUY + 1 SELL")
        if buy[0].right != 'C' or sell[0].right != 'C': err("both legs must be CALL")
        if buy[0].expiry != sell[0].expiry: err("both legs same expiry")
        if buy[0].strike <= sell[0].strike: err("BUY strike must be higher than SELL strike (bearish credit)")

    elif t in ('calendar_call_spread', 'calendar_put_spread'):
        right = 'C' if 'call' in t else 'P'
        if len(legs) != 2: err("expected 2 legs")
        if any(l.right != right for l in legs):
            err(f"both legs must be {right}")
        if legs[0].strike != legs[1].strike: err("both legs same strike (calendar)")
        if legs[0].expiry == legs[1].expiry: err("legs must have different expiries")
        # Standard calendar: short near, long far
        sells = [l for l in legs if l.action == 'SELL']
        buys  = [l for l in legs if l.action == 'BUY']
        if len(sells) != 1 or len(buys) != 1: err("need 1 BUY + 1 SELL")
        if sells[0].expiry >= buys[0].expiry: err("SELL must be the near-dated leg")

    elif t in ('diagonal_call_spread', 'diagonal_put_spread'):
        right = 'C' if 'call' in t else 'P'
        if len(legs) != 2: err("expected 2 legs")
        if any(l.right != right for l in legs):
            err(f"both legs must be {right}")
        if legs[0].strike == legs[1].strike: err("strikes must differ (diagonal)")
        if legs[0].expiry == legs[1].expiry: err("expiries must differ (diagonal)")
        sells = [l for l in legs if l.action == 'SELL']
        buys  = [l for l in legs if l.action == 'BUY']
        if len(sells) != 1 or len(buys) != 1: err("need 1 BUY + 1 SELL")
        if sells[0].expiry >= buys[0].expiry: err("SELL must be the near-dated leg")

    elif t in ('calendar_ratio_call_spread', 'calendar_ratio_put_spread'):
        right = 'C' if 'call' in t else 'P'
        if len(legs) != 2: err("expected 2 legs")
        if any(l.right != right for l in legs):
            err(f"both legs must be {right}")
        if legs[0].expiry == legs[1].expiry: err("legs must have different expiries (calendar)")
        sells = [l for l in legs if l.action == 'SELL']
        buys  = [l for l in legs if l.action == 'BUY']
        if len(sells) != 1 or len(buys) != 1: err("need 1 BUY + 1 SELL")
        if sells[0].expiry >= buys[0].expiry: err("SELL must be the near-dated leg")
        if sells[0].ratio == buys[0].ratio:
            err("calendar_ratio requires unequal leg ratios (use 'calendar_spread' for 1:1)")

    elif t == 'covered_call':
        if len(legs) != 1: err("expected 1 option leg (assumes 100+ shares already held)")
        l = legs[0]
        if l.action != 'SELL' or l.right != 'C': err("must be SELL C (covered by existing long stock)")


# ── DTE bounds ─────────────────────────────────────────────────────────────

def days_to_expiry(expiry: str, today: Optional[date] = None) -> int:
    """Calendar days from today to the expiry date."""
    today = today or date.today()
    return (datetime.strptime(expiry, '%Y-%m-%d').date() - today).days


def check_dte_bounds(structure: OptionsStructure,
                     today: Optional[date] = None) -> tuple[bool, str]:
    """
    Enforce ITPM's 30-60 DTE entry window on the relevant leg.

    For verticals and singles: ALL legs in the 30-60 range.
    For calendars and diagonals: short leg 7-45 DTE, long leg 45-120 DTE.
    """
    today = today or date.today()
    is_time_structure = structure.type.startswith('calendar') or structure.type.startswith('diagonal')

    if is_time_structure:
        sell_leg = next((l for l in structure.legs if l.action == 'SELL'), None)
        buy_leg  = next((l for l in structure.legs if l.action == 'BUY'),  None)
        if sell_leg:
            dte_short = days_to_expiry(sell_leg.expiry, today)
            if not (7 <= dte_short <= 45):
                return False, f"short leg DTE {dte_short} outside 7-45 range"
        if buy_leg:
            dte_long = days_to_expiry(buy_leg.expiry, today)
            if not (45 <= dte_long <= 120):
                return False, f"long leg DTE {dte_long} outside 45-120 range"
    else:
        for leg in structure.legs:
            dte = days_to_expiry(leg.expiry, today)
            if not (30 <= dte <= 60):
                return False, f"leg expiry {leg.expiry} → DTE {dte} outside 30-60 range"

    return True, "ok"


# ── IBKR contract builders (lazy import of ib_insync) ──────────────────────

def build_ibkr_legs(structure: OptionsStructure, exchange: str = 'SMART'):
    """
    Build the list of (ib_insync.Option, action, ratio) tuples for execution.
    Lazy-imports ib_insync so tests don't need the SDK.
    """
    from ib_insync import Option
    legs = []
    for leg in structure.legs:
        contract = Option(
            symbol=structure.underlying,
            lastTradeDateOrContractMonth=leg.to_ibkr_expiry(),
            strike=leg.strike,
            right=leg.right,
            exchange=exchange,
            currency='USD',
            tradingClass=structure.underlying,
        )
        legs.append((contract, leg.action, leg.ratio))
    return legs


def build_ibkr_combo(structure: OptionsStructure, ib, exchange: str = 'SMART'):
    """
    For multi-leg structures, build a Bag (combo) contract with ComboLegs
    suitable for a single net debit/credit order.

    Returns (combo_contract, signed_net_action: 'BUY'|'SELL') where BUY = pay
    net debit, SELL = receive net credit.
    """
    from ib_insync import Bag, ComboLeg

    if structure.is_single_leg():
        raise ValueError("use build_ibkr_legs() for single-leg structures")

    underlying = structure.underlying

    # Qualify each individual option to get conId — required for ComboLeg.
    raw_options = []
    for leg in structure.legs:
        from ib_insync import Option
        c = Option(
            symbol=underlying,
            lastTradeDateOrContractMonth=leg.to_ibkr_expiry(),
            strike=leg.strike,
            right=leg.right,
            exchange=exchange,
            currency='USD',
            tradingClass=underlying,
        )
        raw_options.append((c, leg))
    qualified = ib.qualifyContracts(*[c for c, _ in raw_options])
    if any(not q for q in qualified):
        raise RuntimeError(f"Could not qualify all option contracts for {underlying}")

    combo_legs = []
    for (qcontract, leg) in zip(qualified, [l for _, l in raw_options]):
        combo_legs.append(ComboLeg(
            conId=qcontract.conId,
            ratio=leg.ratio,
            action=leg.action,
            exchange=exchange,
        ))

    bag = Bag(
        symbol=underlying,
        exchange=exchange,
        currency='USD',
        comboLegs=combo_legs,
    )
    # By convention we always submit as BUY action on the combo with a net price.
    # The signs of the underlying legs (BUY vs SELL) determine the net.
    return bag, 'BUY'


# ── Position sizing for options ────────────────────────────────────────────

def max_loss_per_contract_usd(structure: OptionsStructure,
                              leg_prices: dict) -> float:
    """
    Compute the dollar max loss per single contract of the structure,
    given current per-share leg mid prices.

    leg_prices: {leg_key: mid_price_per_share}
        leg_key = (action, right, strike, expiry)

    Long single options: max loss = premium_paid × 100
    Debit verticals: max loss = (debit_paid) × 100
    Credit verticals: max loss = (width − net_credit) × 100
    Calendars/diagonals/ratios: approximate at debit_paid × 100 (the long-leg
        cost minus the short-leg credit; can't lose more than that since the
        long leg outlives the short leg by definition).
    """
    def key(l: OptionLeg):
        return (l.action, l.right, l.strike, l.expiry)

    leg_costs = []
    for l in structure.legs:
        mid = leg_prices.get(key(l))
        if mid is None:
            raise ValueError(f"missing price for leg {key(l)}")
        # BUY → cost positive, SELL → cost negative (credit received)
        sign = +1 if l.action == 'BUY' else -1
        leg_costs.append(sign * mid * l.ratio)

    net_debit = sum(leg_costs)  # positive = debit, negative = credit

    # Vertical credit spread: max loss = (width − net_credit) × 100
    if structure.type in ('bull_put_spread', 'bear_call_spread'):
        sells = [l for l in structure.legs if l.action == 'SELL']
        buys  = [l for l in structure.legs if l.action == 'BUY']
        width = abs(buys[0].strike - sells[0].strike)
        net_credit = max(-net_debit, 0)
        return (width - net_credit) * 100

    # All other defined-risk structures: max loss = debit paid
    return max(net_debit, 0) * 100


def position_size_contracts(max_loss_per_contract: float,
                            account_value: float,
                            risk_pct: float = 0.5) -> int:
    """
    How many contracts to buy such that total max loss ≤ risk_pct of NAV.
    Always rounds down. Minimum 0 (skip the trade) if even 1 contract exceeds budget.
    """
    if max_loss_per_contract <= 0:
        return 0
    budget = account_value * (risk_pct / 100.0)
    contracts = int(budget // max_loss_per_contract)
    return max(contracts, 0)
