"""Quota guard for paid API calls (The Odds API first) — review B1.

Semantics:
- ``begin(endpoint, credits)`` reserves atomically BEFORE the call in one
  ``BEGIN IMMEDIATE`` SQLite transaction: ``INSERT OR IGNORE`` the month
  row, then ``UPDATE ... WHERE credits_used + :n <= :hard_stop``; rowcount
  0 -> QuotaExceededError. On success an ``api_quota_usage`` row is opened
  with status=reserved.
- ``settle(usage_id, actual, remaining_header)`` stores the ACTUAL cost from
  provider headers (``x-requests-last``) and reconciles the counter:
  ``credits_used = clamp(credits_used + max(actual - reserved, 0),
  quota - remaining, hard_stop)``.
- ``fail(usage_id)``: the call died without a response — the reservation
  stays spent (conservative), the row is marked failed.
- ``pacing_state()`` returns ok/degrade/stop; only the hard stop refuses.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

from sqlalchemy import select

from core.config import AppConfig
from core.db import session_scope
from core.logging import get_logger
from core.orm import ApiQuotaUsage, QuotaCounter

logger = get_logger("ingest.quota")


class QuotaExceededError(RuntimeError):
    """Hard stop refused the reservation."""


def _sqlite_path(db_url: str) -> Path:
    if not db_url.startswith("sqlite:///"):
        raise ValueError("atomic quota reserve is implemented for SQLite "
                         f"file databases only, got {db_url!r}")
    # keep absolute paths absolute (sqlite:////abs/path has 4 slashes)
    return Path(db_url[len("sqlite:///"):])


def month_period(now: dt.datetime) -> str:
    return now.strftime("%Y-%m")


def _days_in_month(now: dt.datetime) -> int:
    if now.month == 12:
        first_next = dt.datetime(now.year + 1, 1, 1, tzinfo=dt.UTC)
    else:
        first_next = dt.datetime(now.year, now.month + 1, 1, tzinfo=dt.UTC)
    first = dt.datetime(now.year, now.month, 1, tzinfo=dt.UTC)
    return (first_next - first).days


class QuotaGuard:
    def __init__(self, db_url: str, config: AppConfig,
                 provider: str = "toa") -> None:
        self._db_url = db_url
        self._cfg = config.odds_policy
        self._provider = provider

    # --- atomic reservation (SQLite BEGIN IMMEDIATE) ------------------------

    def reserve(self, credits: int, now: dt.datetime | None = None) -> int:
        """Reserve credits before the call; returns the new used total."""
        if credits <= 0:
            return self.monthly_used(now)
        now = now or dt.datetime.now(dt.UTC)
        period = month_period(now)
        stop = self._cfg.hard_stop_credits
        conn = sqlite3.connect(_sqlite_path(self._db_url), timeout=30.0,
                               isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT OR IGNORE INTO quota_counters "
                "(provider, period, credits_used, updated_at_utc) "
                "VALUES (?, ?, 0, ?)",
                (self._provider, period, now.isoformat()))
            cur = conn.execute(
                "UPDATE quota_counters SET credits_used = credits_used + ?, "
                "updated_at_utc = ? WHERE provider = ? AND period = ? "
                "AND credits_used + ? <= ?",
                (credits, now.isoformat(), self._provider, period, credits,
                 stop))
            if cur.rowcount == 0:
                conn.execute("ROLLBACK")
                used = self.monthly_used(now)
                raise QuotaExceededError(
                    f"hard stop: cannot reserve {credits} "
                    f"({used}/{self._cfg.monthly_quota} used, stop {stop})")
            conn.execute("COMMIT")
            row = conn.execute(
                "SELECT credits_used FROM quota_counters "
                "WHERE provider=? AND period=?",
                (self._provider, period)).fetchone()
            return int(row[0])
        finally:
            conn.close()

    def check(self, credits: int = 1) -> None:
        """Compatibility wrapper: reserve performs the refusing check."""
        self.reserve(credits)

    def monthly_used(self, now: dt.datetime | None = None) -> int:
        now = now or dt.datetime.now(dt.UTC)
        with session_scope(self._db_url) as s:
            row = s.scalars(select(QuotaCounter).where(
                QuotaCounter.provider == self._provider,
                QuotaCounter.period == month_period(now))).first()
            return row.credits_used if row else 0

    # --- usage lifecycle: reserved -> settled / failed ------------------------

    def begin(self, endpoint: str, credits: int,
              now: dt.datetime | None = None) -> int:
        """Reserve + open a usage row; returns the usage row id."""
        self.reserve(credits, now)
        now = now or dt.datetime.now(dt.UTC)
        with session_scope(self._db_url) as s:
            row = ApiQuotaUsage(provider=self._provider, endpoint=endpoint,
                                credits=credits, remaining_header=None,
                                status="reserved", created_at_utc=now)
            s.add(row)
            s.flush()
            return int(row.id)

    def settle(self, usage_id: int, actual_credits: int,
               remaining_header: int | None = None,
               now: dt.datetime | None = None) -> None:
        """Store the actual header-reported cost and reconcile the counter.

        counter = clamp(counter + max(actual - reserved, 0),
                        quota - remaining, hard_stop)
        """
        now = now or dt.datetime.now(dt.UTC)
        period = month_period(now)
        stop = self._cfg.hard_stop_credits
        floor = (self._cfg.monthly_quota - remaining_header
                 if remaining_header is not None else None)
        with session_scope(self._db_url) as s:
            row = s.get(ApiQuotaUsage, usage_id)
            if row is None:
                return
            reserved = row.credits
            row.credits = actual_credits
            row.status = "settled"
            row.remaining_header = remaining_header
            counter = s.scalars(select(QuotaCounter).where(
                QuotaCounter.provider == self._provider,
                QuotaCounter.period == period)).first()
            if counter is None:
                return
            value = counter.credits_used + max(actual_credits - reserved, 0)
            if floor is not None:
                value = max(value, floor)
            counter.credits_used = min(value, stop)
            counter.updated_at_utc = now

    def fail(self, usage_id: int) -> None:
        """No response: the reservation stays spent (conservative)."""
        with session_scope(self._db_url) as s:
            row = s.get(ApiQuotaUsage, usage_id)
            if row is not None:
                row.status = "failed"

    # --- import / free-call record ----------------------------------------------

    def record(self, endpoint: str, credits: int,
               remaining_header: int | None = None,
               status: str = "settled", add_counter: bool = True,
               now: dt.datetime | None = None) -> int:
        """Log a usage row; add_counter=False when the counter was already
        bumped atomically by reserve()."""
        now = now or dt.datetime.now(dt.UTC)
        period = month_period(now)
        with session_scope(self._db_url) as s:
            s.add(ApiQuotaUsage(provider=self._provider, endpoint=endpoint,
                                credits=credits,
                                remaining_header=remaining_header,
                                status=status, created_at_utc=now))
            row = s.scalars(select(QuotaCounter).where(
                QuotaCounter.provider == self._provider,
                QuotaCounter.period == period)).first()
            if row is None:
                row = QuotaCounter(provider=self._provider, period=period,
                                   credits_used=0, updated_at_utc=now)
                s.add(row)
            if add_counter:
                row.credits_used += credits
                row.updated_at_utc = now
            s.flush()
            return row.credits_used

    def reconcile(self, remaining_header: int,
                  now: dt.datetime | None = None) -> None:
        """credits_used = max(credits_used, quota - remaining)."""
        now = now or dt.datetime.now(dt.UTC)
        floor = self._cfg.monthly_quota - remaining_header
        with session_scope(self._db_url) as s:
            row = s.scalars(select(QuotaCounter).where(
                QuotaCounter.provider == self._provider,
                QuotaCounter.period == month_period(now))).first()
            if row is not None and row.credits_used < floor:
                row.credits_used = floor
                row.updated_at_utc = now

    # --- pacing (informational; only the hard stop refuses) ---------------------

    def pacing_state(self, now: dt.datetime | None = None) -> str:
        """ok | degrade | stop (decisions.md; degrade never refuses)."""
        now = now or dt.datetime.now(dt.UTC)
        used = self.monthly_used(now)
        if used >= self._cfg.hard_stop_credits:
            return "stop"
        if not self._cfg.pacing.enabled:
            return "ok"
        projection = used / max(now.day, 1) * _days_in_month(now)
        limit = self._cfg.hard_stop_credits + \
            self._cfg.pacing.safety_margin_credits
        return "degrade" if projection > limit else "ok"

    def guard_call(self, endpoint: str, credits: int,
                   remaining_header: int | None = None) -> int:
        """Legacy one-shot path (imports): atomic reserve + log row."""
        total = self.reserve(credits)
        self.record(endpoint, credits, remaining_header, add_counter=False)
        return total
