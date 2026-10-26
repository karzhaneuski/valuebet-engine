"""Tests for scheduler pacing, tasks, job execution, and FastAPI endpoints (SPEC §9)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from core.config import load_config
from core.orm import Base, Match, QuotaCounter, Result, Team, UserBet
from core.scheduler.main import app
from core.scheduler.pacing import evaluate_degradation_state
from core.scheduler.tasks import (
    job_settle_matches_and_clv,
    run_all_jobs_sync,
)


@pytest.fixture
def db_session_factory() -> sessionmaker[Session]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


# --- Pacing and Degradation Tests --------------------------------------------


def test_pacing_normal_budget(db_session_factory: sessionmaker[Session]) -> None:
    with db_session_factory() as session:
        # 100 credits used on day 15 -> ~200 projected burn (well under 425 threshold)
        test_now = dt.datetime(2026, 10, 15, 12, 0, tzinfo=dt.UTC)
        counter = QuotaCounter(
            provider="toa",
            period="2026-10",
            credits_used=100,
            updated_at_utc=test_now,
        )
        session.add(counter)
        session.commit()

        state = evaluate_degradation_state(session, now=test_now)
        assert state["hard_stop_active"] is False
        assert state["active_degradations"] == []
        assert state["effective_pinnacle_cs_cap"] == 10
        assert state["disable_t3_h2h"] is False
        assert state["t3_granularity"] == "slot"


def test_pacing_degradation_ladder_trigger(db_session_factory: sessionmaker[Session]) -> None:
    with db_session_factory() as session:
        # 250 credits used on day 15 (projected ~516 credits -> exceeds hard_stop 450)
        test_now = dt.datetime(2026, 10, 15, 12, 0, tzinfo=dt.UTC)
        counter = QuotaCounter(
            provider="toa",
            period="2026-10",
            credits_used=250,
            updated_at_utc=test_now,
        )
        session.add(counter)
        session.commit()

        state = evaluate_degradation_state(session, now=test_now)
        active_deg = state.get("active_degradations")
        assert isinstance(active_deg, list)
        assert state["hard_stop_active"] is False
        assert "cap_correct_score_5_week" in active_deg
        assert state["effective_pinnacle_cs_cap"] == 5
        assert state["disable_t3_h2h"] is True
        assert state["t3_granularity"] == "day"


def test_pacing_emergency_hard_stop(db_session_factory: sessionmaker[Session]) -> None:
    with db_session_factory() as session:
        # Reached hard stop limit 450 credits
        test_now = dt.datetime(2026, 10, 20, 12, 0, tzinfo=dt.UTC)
        counter = QuotaCounter(
            provider="toa",
            period="2026-10",
            credits_used=450,
            updated_at_utc=test_now,
        )
        session.add(counter)
        session.commit()

        state = evaluate_degradation_state(session, now=test_now)
        assert state["hard_stop_active"] is True
        assert state["effective_pinnacle_cs_cap"] == 0
        assert state["disable_t3_h2h"] is True


# --- Scheduler Task & Dry Run Tests ------------------------------------------


def test_run_all_jobs_sync_dry_run(db_session_factory: sessionmaker[Session]) -> None:
    cfg = load_config()
    out = run_all_jobs_sync(db_session_factory, cfg, dry_run=True)

    assert "refresh_history" in out
    assert out["refresh_history"]["status"] == "dry_run"
    assert "poll_odds" in out
    assert out["poll_odds"]["status"] == "dry_run"
    assert "prefilter" in out
    assert out["prefilter"]["status"] == "dry_run"
    assert "news_search" in out
    assert out["news_search"]["status"] == "dry_run"
    assert "settle_clv" in out
    assert out["settle_clv"]["status"] == "dry_run"


def test_job_settle_matches_and_clv(db_session_factory: sessionmaker[Session]) -> None:
    now = dt.datetime.now(dt.UTC)
    with db_session_factory() as session:
        t1 = Team(name="Home Team")
        t2 = Team(name="Away Team")
        session.add_all([t1, t2])
        session.flush()

        match = Match(
            league="epl",
            season="2026-2027",
            kickoff_source="toa",
            home_team_id=t1.id,
            away_team_id=t2.id,
            date_utc=now.date(),
            commence_time_utc=now - dt.timedelta(hours=3),
            status="finished",
        )
        session.add(match)
        session.flush()

        # Official score: 2 - 1
        res = Result(
            match_id=match.id,
            home_goals=2,
            away_goals=1,
            source="manual",
            settled_at_utc=now,
        )
        session.add(res)

        # Open bet on 2-1
        bet = UserBet(
            match_id=match.id,
            market="exact_score",
            selection_key="2-1",
            odds=Decimal("8.000"),
            stake_units=Decimal("1.00"),
            status="open",
            placed_at_utc=now - dt.timedelta(hours=5),
            created_at_utc=now - dt.timedelta(hours=5),
        )
        session.add(bet)
        session.commit()

    # Execute settlement job (live execution, not dry run)
    result = job_settle_matches_and_clv(db_session_factory, dry_run=False)
    assert result["status"] == "success"
    assert result["settled_bets"] == 1

    # Verify database state was updated
    with db_session_factory() as session:
        updated_bet = session.get(UserBet, 1)
        assert updated_bet is not None
        assert updated_bet.status == "won"
        assert updated_bet.payout_units == Decimal("8.000")
        assert updated_bet.pnl_units == Decimal("7.000")


# --- FastAPI Scheduler Endpoints Tests ---------------------------------------


def test_scheduler_api_endpoints(db_session_factory: sessionmaker[Session]) -> None:
    client = TestClient(app)

    # Health endpoint
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    # Status endpoint
    resp = client.get("/status")
    assert resp.status_code == 200
    assert "service" in resp.json()

    # Synchronous run endpoint - unauthorized without token
    resp_unauth = client.post("/run-sync?dry_run=true")
    assert resp_unauth.status_code == 401

    # Synchronous run endpoint - authorized with Bearer token
    resp = client.post(
        "/run-sync?dry_run=true",
        headers={"Authorization": "Bearer test-sync-token"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "refresh_history" in data
    assert "settle_clv" in data
