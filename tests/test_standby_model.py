"""Tests for Standby Dixon-Coles Model and Lineup Risk Checks (Phase 5)."""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from core.db import session_scope
from core.model.dataset import load_training_data
from core.orm import Base, Match, MatchLineup, MatchStat, Team
from core.value.standby_model import (
    get_key_players_for_team,
    get_model_home_share_and_model,
)

REAL_DB_URL = os.environ.get("VALUEBET_DB_URL", "sqlite:////tmp/p5.db")


def _create_synthetic_standby_db(db_path: Path) -> str:
    db_url = f"sqlite:///{db_path}"
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        t1 = Team(id=1, name="Arsenal", league="premier_league")
        session.add(t1)
        for opp_idx in range(8):
            session.add(Team(id=10 + opp_idx, name=f"Opponent {opp_idx}", league="premier_league"))
        session.flush()

        # Create 8 historical matches for key player testing
        base_date = dt.date(2026, 9, 1)
        for i in range(8):
            m_date = base_date + dt.timedelta(days=i * 3)
            m_kickoff = dt.datetime(m_date.year, m_date.month, m_date.day, 15, 0, tzinfo=dt.UTC)
            m = Match(
                id=100 + i,
                league="premier_league",
                season=2026,
                date_utc=m_date,
                commence_time_utc=m_kickoff,
                kickoff_source="toa",
                status="finished",
                home_team_id=1,
                away_team_id=10 + i,
            )
            session.add(m)
            session.flush()
            session.add(
                MatchStat(
                    match_id=m.id,
                    source="football_data",
                    home_goals=2,
                    away_goals=1,
                )
            )

            # Player A starts in 7 out of 8 matches (>= 70%)
            if i < 7:
                session.add(
                    MatchLineup(
                        id=1000 + i * 2,
                        match_id=m.id,
                        team_id=1,
                        player="Key Striker",
                        started=True,
                    )
                )
            # Player B starts in only 2 matches (< 70%)
            if i < 2:
                session.add(
                    MatchLineup(
                        id=1000 + i * 2 + 1,
                        match_id=m.id,
                        team_id=1,
                        player="Bench Player",
                        started=True,
                    )
                )

        session.commit()

    return db_url


def test_synthetic_key_players_detection(tmp_path: Path) -> None:
    db_file = tmp_path / "standby_test.db"
    db_url = _create_synthetic_standby_db(db_file)
    engine = create_engine(db_url)

    with Session(engine) as session:
        kp = get_key_players_for_team(
            session=session,
            team_id=1,
            before_date=dt.date(2026, 10, 1),
            last_n=8,
            min_starts_share=0.70,
        )
        assert "Key Striker" in kp
        assert "Bench Player" not in kp

        # Non-existent team
        kp_none = get_key_players_for_team(
            session=session,
            team_id=999,
            before_date=dt.date(2026, 10, 1),
        )
        assert len(kp_none) == 0


def test_standby_model_no_leakage_and_lag_buffer(tmp_path: Path) -> None:
    """Verify that matches within [as_of - 150min, as_of) and after as_of are excluded."""
    db_file = tmp_path / "leakage_test.db"
    db_url = f"sqlite:///{db_file}"
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)

    as_of = dt.datetime(2026, 10, 20, 18, 0, tzinfo=dt.UTC)

    with Session(engine) as session:
        t1 = Team(id=1, name="Arsenal", league="premier_league")
        t2 = Team(id=2, name="Chelsea", league="premier_league")
        t3 = Team(id=3, name="Liverpool", league="premier_league")
        t4 = Team(id=4, name="Everton", league="premier_league")
        t5 = Team(id=5, name="Fulham", league="premier_league")
        t6 = Team(id=6, name="Brighton", league="premier_league")
        session.add_all([t1, t2, t3, t4, t5, t6])
        session.flush()

        # Match 1: kickoff 4 hours before as_of (finished > 150min ago) -> MUST be trained
        m1 = Match(
            id=1,
            league="premier_league",
            season=2026,
            date_utc=as_of.date(),
            commence_time_utc=as_of - dt.timedelta(hours=4),
            kickoff_source="toa",
            status="finished",
            home_team_id=1,
            away_team_id=2,
        )
        # Match 2: kickoff 60 min before as_of (in [as_of - 150min, as_of)) -> MUST NOT be trained
        m2 = Match(
            id=2,
            league="premier_league",
            season=2026,
            date_utc=as_of.date(),
            commence_time_utc=as_of - dt.timedelta(minutes=60),
            kickoff_source="toa",
            status="finished",
            home_team_id=3,
            away_team_id=4,
        )
        # Match 3: kickoff after as_of -> MUST NOT be trained
        m3 = Match(
            id=3,
            league="premier_league",
            season=2026,
            date_utc=as_of.date(),
            commence_time_utc=as_of + dt.timedelta(hours=2),
            kickoff_source="toa",
            status="scheduled",
            home_team_id=5,
            away_team_id=6,
        )
        session.add_all([m1, m2, m3])
        session.flush()
        s1 = MatchStat(match_id=m1.id, source="football_data", home_goals=1, away_goals=0)
        s2 = MatchStat(match_id=m2.id, source="football_data", home_goals=2, away_goals=2)
        session.add_all([s1, s2])
        session.commit()

    # Load training data via core.model.dataset
    tdata = load_training_data(
        db_url=db_url,
        league="premier_league",
        as_of=as_of,
        result_lag_minutes=150,
    )
    # Only Match 1 is included
    assert len(tdata.matches) == 1
    assert tdata.matches[0].match_id == 1
    # Match 2 was excluded by the 150-minute result lag buffer
    assert tdata.stats.n_waiting_result == 1


def test_get_model_home_share_invalid_team_graceful_fallback(tmp_path: Path) -> None:
    db_file = tmp_path / "fallback_test.db"
    db_url = _create_synthetic_standby_db(db_file)
    as_of = dt.datetime(2026, 10, 20, 12, 0, tzinfo=dt.UTC)
    share, model = get_model_home_share_and_model(
        db_url=db_url,
        league="premier_league",
        as_of=as_of,
        home_team="NonExistentFC",
        away_team="Arsenal",
    )
    assert share is None
    assert model is None


@pytest.mark.skipif(
    "VALUEBET_REAL_DB" not in os.environ,
    reason="Requires real database with VALUEBET_REAL_DB=1",
)
def test_get_model_home_share_and_model_on_real_db() -> None:
    as_of = dt.datetime(2024, 10, 20, 12, 0, tzinfo=dt.UTC)
    share, model = get_model_home_share_and_model(
        db_url=REAL_DB_URL,
        league="premier_league",
        as_of=as_of,
        home_team="Arsenal",
        away_team="Liverpool",
    )
    assert share is not None
    assert 0.40 < share < 0.70
    assert model is not None
    m = model.score_matrix("Arsenal", "Liverpool")
    assert m.shape == (11, 11)
    assert abs(float(m.sum()) - 1.0) < 1e-4


@pytest.mark.skipif(
    "VALUEBET_REAL_DB" not in os.environ,
    reason="Requires real database with VALUEBET_REAL_DB=1",
)
def test_get_key_players_for_team_on_real_db() -> None:
    with session_scope(REAL_DB_URL) as session:
        # Team 1 (1. FC Köln) has lineups in DB
        kp = get_key_players_for_team(
            session=session,
            team_id=1,
            before_date=dt.date(2026, 10, 25),
            last_n=8,
            min_starts_share=0.70,
        )
        assert len(kp) > 0
        assert "Linton Maina" in kp or "Marvin Schwäbe" in kp

        kp_empty = get_key_players_for_team(
            session=session,
            team_id=99999,
            before_date=dt.date(2026, 10, 25),
        )
        assert len(kp_empty) == 0
