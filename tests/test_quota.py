"""Quota guard (review B1): atomic reserve, header settle, pacing state."""

from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select

from core.config import load_config
from core.db import get_engine, session_scope
from core.ingest.quota import QuotaExceededError, QuotaGuard
from core.orm import ApiQuotaUsage, Base, QuotaCounter


@pytest.fixture()
def db_url(tmp_path):
    url = f"sqlite:///{tmp_path}/quota.db"
    Base.metadata.create_all(get_engine(url))
    return url


def _set_used(db_url: str, used: int) -> None:
    with session_scope(db_url) as s:
        period = dt.datetime.now(dt.UTC).strftime("%Y-%m")
        row = s.scalars(select(QuotaCounter).where(
            QuotaCounter.provider == "toa",
            QuotaCounter.period == period)).first()
        if row is None:
            s.add(QuotaCounter(provider="toa", period=period,
                               credits_used=used,
                               updated_at_utc=dt.datetime.now(dt.UTC)))
        else:
            row.credits_used = used


def test_reserve_bumps_counter_once(db_url) -> None:
    guard = QuotaGuard(db_url, load_config())
    assert guard.reserve(1) == 1
    assert guard.reserve(1) == 2
    assert guard.monthly_used() == 2


def test_reserve_race_at_449_exactly_one_wins(db_url) -> None:
    config = load_config()
    stop = config.odds_policy.hard_stop_credits
    _set_used(db_url, stop - 1)  # 449 of 450

    guard = QuotaGuard(db_url, config)

    def attempt():
        try:
            guard.reserve(1)
            return "ok"
        except QuotaExceededError:
            return "refused"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert sorted(results) == ["ok", "refused"]
    assert guard.monthly_used() == stop


def test_reserve_3_at_448_refused(db_url) -> None:
    config = load_config()
    _set_used(db_url, 448)
    guard = QuotaGuard(db_url, config)
    with pytest.raises(QuotaExceededError, match="hard stop"):
        guard.reserve(3)
    assert guard.monthly_used() == 448  # nothing spent on refusal


def test_begin_settle_by_header(db_url) -> None:
    guard = QuotaGuard(db_url, load_config())
    usage_id = guard.begin("/sports/x/odds", 1)
    with session_scope(db_url) as s:
        row = s.get(ApiQuotaUsage, usage_id)
        assert row is not None
        assert row.status == "reserved" and row.credits == 1
    # provider actually charged 1, 499 remaining (header is the truth)
    guard.settle(usage_id, actual_credits=1, remaining_header=499)
    with session_scope(db_url) as s:
        row = s.get(ApiQuotaUsage, usage_id)
        assert row is not None
        assert row.status == "settled" and row.credits == 1
    assert guard.monthly_used() == 1


def test_settle_more_than_reserved_grows_counter(db_url) -> None:
    guard = QuotaGuard(db_url, load_config())
    usage_id = guard.begin("/x", 1)
    guard.settle(usage_id, actual_credits=2, remaining_header=498)
    assert guard.monthly_used() == 2  # reserved 1, actual 2


def test_settle_reconciles_with_remaining_header(db_url) -> None:
    config = load_config()
    guard = QuotaGuard(db_url, config)
    usage_id = guard.begin("/x", 1)
    # provider says 470 remaining -> used must be at least 30
    guard.settle(usage_id, actual_credits=1, remaining_header=470)
    assert guard.monthly_used() == max(
        2, config.odds_policy.monthly_quota - 470)


def test_fail_keeps_reservation_conservative(db_url) -> None:
    guard = QuotaGuard(db_url, load_config())
    usage_id = guard.begin("/x", 1)
    guard.fail(usage_id)  # timeout after send: no response
    assert guard.monthly_used() == 1  # credit stays spent
    with session_scope(db_url) as s:
        row = s.get(ApiQuotaUsage, usage_id)
        assert row is not None
        assert row.status == "failed"


def test_pacing_state_injectable_clock(db_url) -> None:
    config = load_config()
    guard = QuotaGuard(db_url, config)
    _set_used(db_url, 100)
    # day 2 of a 31-day month: projection 100/2*31 = 1550 > 475 -> degrade
    assert guard.pacing_state(now=dt.datetime(2026, 10, 2, 12,
                                              tzinfo=dt.UTC)) == "degrade"
    # same usage late in the month: projection ~103 -> ok
    assert guard.pacing_state(now=dt.datetime(2026, 10, 30, 12,
                                              tzinfo=dt.UTC)) == "ok"
    _set_used(db_url, 0)
    with session_scope(db_url) as s:
        row = s.scalars(select(QuotaCounter)).one()
        row.credits_used = config.odds_policy.hard_stop_credits
    assert guard.pacing_state(now=dt.datetime(2026, 10, 30, 12,
                                              tzinfo=dt.UTC)) == "stop"


def test_guard_call_no_double_count(db_url) -> None:
    guard = QuotaGuard(db_url, load_config())
    total = guard.guard_call("/import", 20, remaining_header=480)
    assert total == 20
    assert guard.monthly_used() == 20
