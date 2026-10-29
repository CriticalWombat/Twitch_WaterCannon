"""
reveal.py
---------
When to reveal tonight's winner. Pure functions: no Twitch, GPIO or asyncio,
so the schedule can be tested on its own. main.py runs the loop around it.
"""

from datetime import datetime, timedelta
from typing import Optional, Tuple


def parse_hhmm(value: str) -> Tuple[int, int]:
    """'21:30' -> (21, 30). Raises ValueError on anything else."""
    hh, mm = (int(part) for part in value.strip().split(":"))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError(f"expected HH:MM in 24h time, got {value!r}")
    return hh, mm


def window_start(now: datetime, end: Tuple[int, int], minutes_before: float,
                 window_min: float) -> Optional[datetime]:
    """Start of the reveal window containing now, or None.

    The window opens minutes_before minutes before the stream's planned end
    and stays open for window_min minutes, so a restart shortly after the
    reveal time can still reveal, while a restart long after cannot. Both
    today's and tomorrow's end time are checked so a stream that ends after
    midnight works.
    """
    for days in (0, 1):
        end_at = now.replace(hour=end[0], minute=end[1], second=0, microsecond=0) + timedelta(days=days)
        start = end_at - timedelta(minutes=minutes_before)
        if start <= now < start + timedelta(minutes=window_min):
            return start
    return None


def next_start(now: datetime, end: Tuple[int, int], minutes_before: float,
               grace_sec: float = 60) -> datetime:
    """The reveal the countdown should aim at: the earliest reveal start that is still
    upcoming, or that began less than grace_sec ago (so the overlay can say
    "revealing" for a moment instead of jumping a day ahead).

    Yesterday's through the day after tomorrow's end times are checked: a reveal can
    fall on the day before its stream end (an end just after midnight), so tomorrow's
    alone is not always enough.
    """
    best = None
    for days in (-1, 0, 1, 2):
        end_at = now.replace(hour=end[0], minute=end[1], second=0, microsecond=0) + timedelta(days=days)
        start = end_at - timedelta(minutes=minutes_before)
        if start + timedelta(seconds=grace_sec) > now and (best is None or start < best):
            best = start
    return best

