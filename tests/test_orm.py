"""ORM schema behaviour: enums, constraints, indexes, relationships."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError, StatementError

from core.db import get_engine, session_scope
from core.orm import Base, Match, OddsSnapshot, Recommendation, Team, TeamAlias


@pytest.fixture()
def db_url(tmp_path):
    url = f"sqlite:///{tmp_path}/test.db"
    Base.metadata.create_all(get_engine(url))
    return url


def test_match_roundtrip_and_index(db_url) -> None:
    with session_scope(db_url) as s:
        home = Team(name="Arsenal", league="premier_league")
        away = Team(name="Everton", league="premier_league")
        s.add_all([home, away])
        s.flush()
        m = Match(
            league="premier_league", season=2026,
            date_utc=dt.date(2026, 10, 24),
            commence_time_utc=dt.datetime(2026, 10, 24, 14, 0,
                                          tzinfo=dt.UTC),
            kickoff_source="toa", status="scheduled",
            home_team_id=home.id, away_team_id=away.id,
            toa_event_id="evt-1", understat_id="123",
        )
        s.add(m)
    with session_scope(db_url) as s:
        m = s.scalars(select(Match).where(Match.toa_event_id == "evt-1")).one()
        assert m.status == "scheduled"
        assert m.home_team.name == "Arsenal"

    indexes = {i["name"] for i in inspect(get_engine(db_url)).get_indexes("matches")}
    assert "ix_matches_date_league" in indexes


def test_match_status_vocabulary_enforced(db_url) -> None:
    from core.db import get_session_factory

    factory = get_session_factory(db_url)
    session = factory()
    t1, t2 = Team(name="A", league=None), Team(name="B", league=None)
    session.add_all([t1, t2])
    session.flush()
    session.add(Match(league="ucl", season=2026, date_utc=dt.date(2026, 11, 3),
                      kickoff_source="toa", status="not_a_status",
                      home_team_id=t1.id, away_team_id=t2.id))
    # validate_strings rejects at bind time (wrapped in StatementError);
    # the DB CHECK constraint is the second line of defence
    with pytest.raises((StatementError, LookupError, IntegrityError)):
        session.flush()
    session.rollback()
    session.close()


def test_team_alias_unique_per_source(db_url) -> None:
    with session_scope(db_url) as s:
        t = Team(name="Nottingham Forest")
        s.add(t)
        s.flush()
        s.add(TeamAlias(team_id=t.id, source="toa", alias="Nottingham Forest"))
    from core.db import get_session_factory
    factory = get_session_factory(db_url)
    session = factory()
    with pytest.raises(IntegrityError):
        t = session.scalars(select(Team).where(
            Team.name == "Nottingham Forest")).one()
        session.add(TeamAlias(team_id=t.id, source="toa",
                              alias="Nottingham Forest"))
        session.flush()
    session.rollback()
    session.close()


def test_odds_snapshot_fields(db_url) -> None:
    with session_scope(db_url) as s:
        t1, t2 = Team(name="X"), Team(name="Y")
        s.add_all([t1, t2])
        s.flush()
        m = Match(league="premier_league", season=2026,
                  date_utc=dt.date(2026, 10, 24), kickoff_source="toa",
                  home_team_id=t1.id, away_team_id=t2.id, toa_event_id="e1")
        s.add(m)
        s.flush()
        s.add(OddsSnapshot(
            toa_sport_key="soccer_epl", toa_event_id="e1", match_id=m.id,
            bookmaker="Fortuna", market="totals", line=Decimal("2.5"),
            outcome="Over", price=Decimal("1.95"), tier="T-3",
            planned_offset_min=180, actual_offset_min=175,
            source="manual_fortuna",
            captured_at_utc=dt.datetime(2026, 10, 24, 11, 5,
                                        tzinfo=dt.UTC)))
    with session_scope(db_url) as s:
        snap = s.scalars(select(OddsSnapshot)).one()
        assert snap.tier == "T-3"
        assert float(snap.price) == 1.95


def test_recommendation_clv_fields(db_url) -> None:
    with session_scope(db_url) as s:
        t1, t2 = Team(name="P"), Team(name="Q")
        s.add_all([t1, t2])
        s.flush()
        m = Match(league="premier_league", season=2026,
                  date_utc=dt.date(2026, 10, 24), kickoff_source="toa",
                  home_team_id=t1.id, away_team_id=t2.id)
        s.add(m)
        s.flush()
        s.add(Recommendation(
            match_id=m.id, market="totals", line=Decimal("2.5"),
            outcome="Over", odds=Decimal("1.95"), p_model=Decimal("0.5500"),
            edge=Decimal("0.0725"), variant="base",
            clv=Decimal("0.0123"), clv_source="avgc",
            final_odds_source="fortuna_manual",
            created_at_utc=dt.datetime(2026, 10, 24, 12, 0,
                                       tzinfo=dt.UTC)))
    with session_scope(db_url) as s:
        r = s.scalars(select(Recommendation)).one()
        assert r.clv_source == "avgc"
        assert r.variant == "base"
        assert r.final_odds_source == "fortuna_manual"
