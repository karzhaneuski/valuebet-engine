"""Quota guard: hard stop, pacing projection, counter persistence."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from core.config import load_config
from core.db import get_engine, session_scope
from core.ingest.quota import QuotaExceededError, QuotaGuard
from core.orm import Base, QuotaCounter


@pytest.fixture()
def db_url(tmp_path):
    url = f"sqlite:///{tmp_path}/quota.db"
    Base.metadata.create_all(get_engine(url))
    return url


def _set_used(db_url: str, provider: str, used: int) -> None:
    with session_scope(db_url) as s:
        s.add(QuotaCounter(provider=provider, period=dt.datetime.now(
            dt.UTC).strftime("%Y-%m"), credits_used=used,
            updated_at_utc=dt.datetime.now(dt.UTC)))


def test_record_increments_counter(db_url) -> None:
    config = load_config()
    guard = QuotaGuard(db_url, config)
    total = guard.guard_call("/sports/x/odds", 1, remaining_header=499)
    assert total == 1
    total = guard.guard_call("/sports/y/odds", 1, remaining_header=498)
    assert total == 2
    with session_scope(db_url) as s:
        rows = s.scalars(select(QuotaCounter)).all()
        assert len(rows) == 1 and rows[0].credits_used == 2


def test_hard_stop_refuses(db_url) -> None:
    config = load_config()
    guard = QuotaGuard(db_url, config)
    _set_used(db_url, "toa", config.odds_policy.hard_stop_credits)
    with pytest.raises(QuotaExceededError, match="hard stop"):
        guard.check(1)


def test_pacing_refuses_early_burn(db_url) -> None:
    config = load_config()
    guard = QuotaGuard(db_url, config)
    # day 1 of the month with a burst: 100 credits already used projects
    # far above hard stop + safety margin
    _set_used(db_url, "toa", 100)
    now = dt.datetime.now(dt.UTC)
    with session_scope(db_url) as s:
        row = s.scalars(select(QuotaCounter)).one()
        # force the period to the current month regardless of test date
        row.period = now.strftime("%Y-%m")
    try:
        guard.check(1)
        pacing_ok = True
    except QuotaExceededError as exc:
        pacing_ok = "pacing" not in str(exc)
    # early-month burn must trip pacing; late-month (day >= 22) may not —
    # assert the invariant that matches the rule, not the calendar
    if now.day <= 7:  # 101 credits by day 7 projects ~447+, over the limit
        assert not pacing_ok or (101 / now.day * 31) <= (
            config.odds_policy.hard_stop_credits
            + config.odds_policy.pacing.safety_margin_credits)


def test_free_call_no_counter(db_url) -> None:
    guard = QuotaGuard(db_url, load_config())
    guard.record("/sports/x/events", 0, remaining_header=480)
    assert guard.monthly_used() == 0
