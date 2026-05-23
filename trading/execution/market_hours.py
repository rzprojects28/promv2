"""
US equity / options market hours checker.

The trading run is scheduled in SGT (Singapore time, UTC+8). The US session
shifts by one hour twice a year because the US observes daylight saving time
while Singapore doesn't. This module owns the timezone math so callers never
have to think about it.

Used by:
  - execution_agent.run() — refuse to submit orders when market is closed
  - monitor_agent.run() — refuse to submit close orders when market is closed

Anything that can be evaluated without IBKR (research, risk validation,
stat reporting) keeps running regardless of market hours.
"""
from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover  -- Python 3.8 fallback
    from backports.zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


# US equity / options market holidays (NYSE / CBOE). Update annually.
# Source: https://www.nyse.com/markets/hours-calendars
US_MARKET_HOLIDAYS = {
    # 2026
    "2026-01-01",   # New Year's Day
    "2026-01-19",   # MLK Day
    "2026-02-16",   # Presidents Day
    "2026-04-03",   # Good Friday
    "2026-05-25",   # Memorial Day
    "2026-06-19",   # Juneteenth
    "2026-07-03",   # Independence Day (observed)
    "2026-09-07",   # Labor Day
    "2026-11-26",   # Thanksgiving
    "2026-12-25",   # Christmas
    # 2027
    "2027-01-01",
    "2027-01-18",
    "2027-02-15",
    "2027-03-26",
    "2027-05-31",
    "2027-06-18",   # Juneteenth (observed Friday since 6/19 is Saturday)
    "2027-07-05",   # July 4 observed Monday
    "2027-09-06",
    "2027-11-25",
    "2027-12-24",   # Christmas observed Friday
}

# Early-close days (1:00pm ET). Treated as open if checked before 13:00 ET.
US_MARKET_EARLY_CLOSE = {
    # 2026
    "2026-07-02",   # day before Independence Day (Independence Day observed Friday)
    "2026-11-27",   # day after Thanksgiving
    "2026-12-24",   # Christmas Eve
    # 2027
    "2027-07-02",
    "2027-11-26",
    "2027-12-23",
}

REGULAR_OPEN  = time(9, 30)
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE   = time(13, 0)


def now_et(now_utc: Optional[datetime] = None) -> datetime:
    """Return current time in US Eastern. Defaults to system now()."""
    if now_utc is None:
        now_utc = datetime.now(tz=UTC)
    elif now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=UTC)
    return now_utc.astimezone(ET)


def is_us_market_open(now_utc: Optional[datetime] = None) -> tuple[bool, str]:
    """
    Returns (is_open, reason).

    is_open=True only when:
      - Day is Mon-Fri
      - Day is not in US_MARKET_HOLIDAYS
      - Time is between 09:30 ET and the day's close (16:00 normally, 13:00 on early-close days)
    """
    n = now_et(now_utc)
    day_str = n.strftime('%Y-%m-%d')

    # Weekend
    if n.weekday() >= 5:
        return False, f"weekend ({n.strftime('%A %Y-%m-%d %H:%M ET')})"

    # Holiday
    if day_str in US_MARKET_HOLIDAYS:
        return False, f"US market holiday ({day_str})"

    # Time-of-day
    close = EARLY_CLOSE if day_str in US_MARKET_EARLY_CLOSE else REGULAR_CLOSE
    if not (REGULAR_OPEN <= n.time() < close):
        which = "(early close)" if day_str in US_MARKET_EARLY_CLOSE else ""
        return False, (f"outside RTH ({n.strftime('%Y-%m-%d %H:%M ET')}) — "
                       f"market is {REGULAR_OPEN.isoformat()} to {close.isoformat()} ET {which}".strip())

    return True, f"market open ({n.strftime('%H:%M ET')})"


def minutes_until_open(now_utc: Optional[datetime] = None) -> Optional[int]:
    """
    If market is currently CLOSED and the same trading day's open is still
    ahead, return minutes until then. Otherwise None.
    Useful to decide whether to "wait a few minutes and retry" vs "skip entirely".
    """
    n = now_et(now_utc)
    day_str = n.strftime('%Y-%m-%d')
    if n.weekday() >= 5 or day_str in US_MARKET_HOLIDAYS:
        return None
    if n.time() >= REGULAR_OPEN:
        return None
    open_dt = datetime.combine(n.date(), REGULAR_OPEN, tzinfo=ET)
    delta_s = (open_dt - n).total_seconds()
    return int(delta_s // 60)
