"""Quota guard for paid API calls (The Odds API first).

Every paid call must go through :class:`QuotaGuard`:
- the call is refused when the monthly counter reached the hard stop
  (450 of the 500-credit quota, docs/decisions.md final round S3);
- the pacing rule (remaining / days_left_in_month) refuses the call when the
  projected monthly burn would cross the stop with the safety margin;
- every attempt (also free calls with a cost of 0) is logged to
  api_quota_usage; the monthly counter lives in quota_counters.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from core.config import AppConfig
from core.db import session_scope
from core.logging import get_logger
from core.orm import ApiQuotaUsage, QuotaCounter

logger = get_logger("ingest.quota")


class QuotaExceededError(RuntimeError):
    """Hard stop or pacing projection refused the call."""


def _month_period(now: dt.datetime) -> str:
    return now.strftime("%Y-%m")


def _days_left_in_month(now: dt.datetime) -> int:
    if now.month == 12:
        last = dt.datetime(now.year + 1, 1, 1, tzinfo=now.tzinfo)
    else:
        last = dt.datetime(now.year, now.month + 1, 1, tzinfo=now.tzinfo)
    return max((last - now).days, 1)


class QuotaGuard:
    def __init__(self, db_url: str, config: AppConfig,
                 provider: str = "toa") -> None:
        self._db_url = db_url
        self._cfg = config.odds_policy
        self._provider = provider

    def monthly_used(self) -> int:
        now = dt.datetime.now(dt.UTC)
        with session_scope(self._db_url) as s:
            row = s.scalars(
                select(QuotaCounter).where(
                    QuotaCounter.provider == self._provider,
                    QuotaCounter.period == _month_period(now))).first()
            return row.credits_used if row else 0

    def check(self, credits: int = 1) -> None:
        """Raise QuotaExceededError when the call must not happen."""
        used = self.monthly_used()
        if used >= self._cfg.hard_stop_credits:
            raise QuotaExceededError(
                f"hard stop: {used}/{self._cfg.monthly_quota} credits used "
                f"(stop at {self._cfg.hard_stop_credits})")
        if not self._cfg.pacing.enabled:
            return
        now = dt.datetime.now(dt.UTC)
        day = now.day
        # burn-rate projection: used so far extrapolated to the whole month
        projected_full_month = (used + credits) / max(day, 1) * 31
        limit = self._cfg.hard_stop_credits + self._cfg.pacing.safety_margin_credits
        if projected_full_month > limit:
            raise QuotaExceededError(
                f"pacing: projected {projected_full_month:.0f} credits/month "
                f"exceeds {limit} (used {used} by day {day})")

    def record(self, endpoint: str, credits: int,
               remaining_header: int | None = None) -> int:
        """Log the call and bump the monthly counter; returns total used."""
        now = dt.datetime.now(dt.UTC)
        period = _month_period(now)
        with session_scope(self._db_url) as s:
            s.add(ApiQuotaUsage(provider=self._provider, endpoint=endpoint,
                                credits=credits,
                                remaining_header=remaining_header,
                                created_at_utc=now))
            row = s.scalars(select(QuotaCounter).where(
                QuotaCounter.provider == self._provider,
                QuotaCounter.period == period)).first()
            if row is None:
                row = QuotaCounter(provider=self._provider, period=period,
                                   credits_used=0, updated_at_utc=now)
                s.add(row)
            row.credits_used += credits
            row.updated_at_utc = now
            s.flush()
            return row.credits_used

    def guard_call(self, endpoint: str, credits: int,
                   remaining_header: int | None = None) -> int:
        """check -> (caller performs the call) -> record."""
        self.check(credits)
        return self.record(endpoint, credits, remaining_header)
