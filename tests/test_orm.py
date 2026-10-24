"""ORM schema behaviour: enums, constraints, indexes, relationships."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError, StatementError

from core.db import get_engine, session_scope
from core.orm import Base, LlmCall, Match, OddsSnapshot, Recommendation, Team, TeamAlias


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


def test_manual_fortuna_snapshot_without_toa_ids(db_url) -> None:
    with session_scope(db_url) as s:
        s.add(OddsSnapshot(
            toa_sport_key=None, toa_event_id=None, bookmaker="fortuna",
            market="correct_score", line=None, outcome="H1|A0",
            price=Decimal("8.5"), tier="manual", source="manual_fortuna",
            captured_at_utc=dt.datetime(2026, 10, 24, 10, 0, tzinfo=dt.UTC)))


def test_toa_snapshot_requires_toa_ids(db_url) -> None:
    from core.db import get_session_factory
    factory = get_session_factory(db_url)
    session = factory()
    session.add(OddsSnapshot(
        toa_sport_key=None, toa_event_id=None, bookmaker="pinnacle",
        market="totals", line=Decimal("2.5"), outcome="Over",
        price=Decimal("1.95"), tier="T-3", source="toa",
        captured_at_utc=dt.datetime(2026, 10, 24, 11, 0, tzinfo=dt.UTC)))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()
    session.close()


def test_understat_id_unique(db_url) -> None:
    from core.db import get_session_factory
    with session_scope(db_url) as s:
        t1, t2 = Team(name="U1"), Team(name="U2")
        s.add_all([t1, t2])
        s.flush()
        s.add(Match(league="premier_league", season=2026,
                    date_utc=dt.date(2026, 10, 24), kickoff_source="toa",
                    understat_id=12345, home_team_id=t1.id,
                    away_team_id=t2.id))
    session = get_session_factory(db_url)()
    t1 = session.scalars(select(Team).where(Team.name == "U1")).one()
    t2 = session.scalars(select(Team).where(Team.name == "U2")).one()
    # same understat_id on a different date must fail (unique)
    session.add(Match(league="premier_league", season=2026,
                      date_utc=dt.date(2026, 10, 26), kickoff_source="toa",
                      understat_id=12345, home_team_id=t1.id,
                      away_team_id=t2.id))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()
    session.close()


def test_league_pairing_unique_and_ucl_stage(db_url) -> None:
    from core.db import get_session_factory
    with session_scope(db_url) as s:
        t1, t2 = Team(name="P1"), Team(name="P2")
        s.add_all([t1, t2])
        s.flush()
        s.add(Match(league="premier_league", season=2026,
                    date_utc=dt.date(2026, 10, 24), kickoff_source="toa",
                    home_team_id=t1.id, away_team_id=t2.id))
    factory = get_session_factory(db_url)
    s2 = factory()
    t1 = s2.scalars(select(Team).where(Team.name == "P1")).one()
    t2 = s2.scalars(select(Team).where(Team.name == "P2")).one()
    # same pairing again -> unique violation (league, stage IS NULL)
    s2.add(Match(league="premier_league", season=2026,
                 date_utc=dt.date(2026, 10, 25), kickoff_source="toa",
                 home_team_id=t1.id, away_team_id=t2.id))
    with pytest.raises(IntegrityError):
        s2.flush()
    s2.rollback()
    # same pairing in UCL with DIFFERENT stages -> allowed
    s2.add(Match(league="ucl", season=2026, stage="league_phase",
                 date_utc=dt.date(2026, 11, 3), kickoff_source="toa",
                 home_team_id=t1.id, away_team_id=t2.id))
    s2.add(Match(league="ucl", season=2026, stage="final",
                 date_utc=dt.date(2027, 5, 29), kickoff_source="toa",
                 home_team_id=t1.id, away_team_id=t2.id))
    s2.commit()
    s2.close()


def test_shadow_llm_requires_llm_call(db_url) -> None:
    from core.db import get_session_factory
    factory = get_session_factory(db_url)
    session = factory()
    t1, t2 = Team(name="S1"), Team(name="S2")
    session.add_all([t1, t2])
    session.flush()
    m = Match(league="premier_league", season=2026,
              date_utc=dt.date(2026, 10, 24), kickoff_source="toa",
              home_team_id=t1.id, away_team_id=t2.id)
    session.add(m)
    session.flush()
    session.add(Recommendation(
        match_id=m.id, market="multiscore", outcome="1:0,2:0,2:1",
        scores_set="[[1,0],[2,0],[2,1]]", bookmaker="fortuna",
        odds=Decimal("3.4"), p_model=Decimal("0.3300"),
        p_market_devig=Decimal("0.2900"), edge=Decimal("0.1220"),
        variant="shadow_llm",
        created_at_utc=dt.datetime(2026, 10, 24, 9, 0, tzinfo=dt.UTC)))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()
    session.close()
    # valid case on a FRESH session (the rollback above wiped the
    # un-committed match too — with FK enforcement that matters)
    session = factory()
    t1, t2 = Team(name="S1"), Team(name="S2")
    session.add_all([t1, t2])
    session.flush()
    m2 = Match(league="premier_league", season=2026,
               date_utc=dt.date(2026, 10, 24), kickoff_source="toa",
               home_team_id=t1.id, away_team_id=t2.id)
    session.add(m2)
    session.flush()
    call = LlmCall(purpose="shadow", prompt="...", model="gemini-3.8-flash",
                   model_version="3.0",
                   thinking_config='{"thinkingBudget": 4096}',
                   created_at_utc=dt.datetime(2026, 10, 24, 9, 0,
                                              tzinfo=dt.UTC))
    session.add(call)
    session.flush()
    session.add(Recommendation(
        match_id=m2.id, market="goals_range", outcome="2-4",
        goals_range_lo=2, goals_range_hi=4, bookmaker="fortuna",
        odds=Decimal("1.8"), p_model=Decimal("0.6100"), edge=Decimal("0.0980"),
        variant="shadow_llm", llm_call_id=call.id,
        created_at_utc=dt.datetime(2026, 10, 24, 9, 0, tzinfo=dt.UTC)))
    session.commit()
    session.close()


def test_snapshot_t3_day_degraded_default_false(db_url) -> None:
    with session_scope(db_url) as s:
        s.add(OddsSnapshot(
            toa_sport_key=None, toa_event_id=None, bookmaker="fortuna",
            market="totals", line=Decimal("2.5"), outcome="Under",
            price=Decimal("2.1"), tier="manual", source="manual_fortuna",
            captured_at_utc=dt.datetime(2026, 10, 24, 9, 0, tzinfo=dt.UTC)))
    with session_scope(db_url) as s:
        snap = s.scalars(select(OddsSnapshot)).one()
        assert snap.t3_day_degraded is False
    col = {c["name"]: c for c in
           inspect(get_engine(db_url)).get_columns("odds_snapshots")}[
        "t3_day_degraded"]
    assert col["nullable"] is False


def test_alias_fk_enforced(db_url) -> None:
    """B1: with PRAGMA foreign_keys=ON an alias for a missing team fails."""
    from core.db import get_session_factory
    factory = get_session_factory(db_url)
    session = factory()
    session.add(TeamAlias(team_id=999999, source="toa", alias="Ghost"))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()
    session.close()


def test_utcdatetime_binds_non_utc_aware_to_utc(db_url) -> None:
    """NIT: an aware value in a non-UTC offset is stored as UTC."""
    import datetime as dt_
    from zoneinfo import ZoneInfo

    kyiv = dt_.datetime(2026, 10, 24, 22, 30, tzinfo=ZoneInfo("Europe/Kyiv"))
    with session_scope(db_url) as s:
        t1, t2 = Team(name="N1"), Team(name="N2")
        s.add_all([t1, t2])
        s.flush()
        s.add(Match(league="premier_league", season=2026,
                    date_utc=kyiv.date(), commence_time_utc=kyiv,
                    kickoff_source="toa", status="scheduled",
                    home_team_id=t1.id, away_team_id=t2.id))
    with session_scope(db_url) as s:
        m = s.scalars(select(Match)).one()
        assert m.commence_time_utc.utcoffset() == dt_.UTC.utcoffset(None)
        assert m.commence_time_utc.hour == 19  # 22:30+03 == 19:30Z? no: 22:30+3=19:30Z
        assert m.commence_time_utc.minute == 30
