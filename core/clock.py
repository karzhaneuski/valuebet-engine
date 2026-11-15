"""System clock abstraction for deterministic testing and scheduling."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable

Clock = Callable[[], dt.datetime]


def system_clock() -> dt.datetime:
    """Return the current time as an aware UTC datetime."""
    return dt.datetime.now(dt.UTC)


def today_date(clock: Clock | None = None) -> dt.date:
    """Return today's date in UTC according to the given clock (or system clock)."""
    clk = clock or system_clock
    return clk().date()
