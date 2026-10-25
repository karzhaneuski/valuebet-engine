"""Backtest period splitting and the test-period lock (phase 4).

Periods are defined by LEAGUE SEASONS (2023 = 2023/24):
- warmup 2021-2022: training history only, never scored;
- validation 2023-2024: parameter tuning happens here and ONLY here;
- test 2025-2026: scored exactly once, after FROZEN_CONFIG.json exists.

The lock is a file-existence gate: any code path that would compute or
print test-period metrics must call :func:`ensure_period_allowed`, which
raises until the frozen config has been committed. This is organisational
leak protection - the tuning code cannot even be pointed at the test
seasons by accident.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

WARMUP_SEASONS = frozenset({2021, 2022})
VALIDATION_SEASONS = frozenset({2023, 2024})
TEST_SEASONS = frozenset({2025, 2026})

FROZEN_CONFIG_PATH = Path("reports/backtest/FROZEN_CONFIG.json")
FROZEN_CONFIG_4B_PATH = Path("reports/backtest/FROZEN_CONFIG_4b.json")

PERIODS = {
    "warmup": WARMUP_SEASONS,
    "validation": VALIDATION_SEASONS,
    "test": TEST_SEASONS,
}


TEST_START = dt.datetime(2025, 7, 1, tzinfo=dt.UTC)
"""Earliest moment considered test period for the DATE lock (S2): the
2025/26 season window. Anything ending after this needs a frozen config."""


class TestPeriodLockedError(RuntimeError):
    """Raised on any attempt to score the test period before the config
    is frozen."""


def period_of_season(season: int) -> str | None:
    for name, seasons in PERIODS.items():
        if season in seasons:
            return name
    return None


def ensure_period_allowed(period: str,
                          frozen_config: Path = FROZEN_CONFIG_PATH) -> None:
    """Refuse test-period scoring unless FROZEN_CONFIG.json exists."""
    if period == "test" and not frozen_config.exists():
        raise TestPeriodLockedError(
            "the test period is locked: reports/backtest/FROZEN_CONFIG.json "
            "does not exist; tune on the validation seasons only"
        )


def ensure_period_allowed_4b(period: str,
                             frozen_config: Path = FROZEN_CONFIG_4B_PATH) -> None:
    """Refuse test-period scoring for Phase 4b unless canonical FROZEN_CONFIG_4b.json exists."""
    if period == "test" and (
            frozen_config.name != "FROZEN_CONFIG_4b.json" or
            not frozen_config.is_file() or
            frozen_config.resolve() != FROZEN_CONFIG_4B_PATH.resolve()):
        raise TestPeriodLockedError(
            f"the test period for phase 4b is locked: requires canonical "
            f"{FROZEN_CONFIG_4B_PATH}, got {frozen_config}"
        )



def season_range(period: str) -> tuple[int, int]:
    seasons = PERIODS[period]
    return min(seasons), max(seasons)


def ensure_window_allowed(start: dt.datetime, end: dt.datetime,
                          frozen_config: Path = FROZEN_CONFIG_PATH) -> None:
    """DATE lock (review S2): any walk-forward window that reaches into
    the test dates is refused without a frozen config, regardless of the
    seasons of the matches inside it."""
    if end >= TEST_START and not frozen_config.exists():
        raise TestPeriodLockedError(
            f"walk-forward window ends at {end.isoformat()}, inside the "
            "test period (>= 2025-07-01); refusing without "
            "reports/backtest/FROZEN_CONFIG.json"
        )
