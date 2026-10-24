"""Training-data selection rules (phase 3): statuses, as_of, quarantine.

The loader-level rules are tested on a throwaway SQLite database; the
bit-for-bit leak test lives here too (fit_league end to end).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select

from core.config import ModelConfig
from core.db import get_engine, session_scope
from core.model.dataset import (
    load_training_data,
    quarantined_match_ids,
)
from core.model.dixon_coles import fit_league
from core.orm import Base, Match, MatchStat, Team

AS_OF = dt.datetime(2026, 10, 24, 12, 0, tzinfo=dt.UTC)


@pytest.fixture()
def db_url(tmp_path):
    url = f"sqlite:///{tmp_path}/model-test.db"
    Base.metadata.create_all(get_engine(url))
    return url


def add_match(
    session, *, home: str, away: str, kickoff: dt.datetime, status: str = "finished",
    season: int = 2026, goals: tuple[int, int] | None = (1, 1),
    xg: tuple[float, float] | None = (1.2, 0.8), fd_goals: tuple[int, int] | None = None,
    match_id: int | None = None,
) -> Match:
    h = session.scalars(select(Team).where(Team.name == home)).first()
    if h is None:
        h = Team(name=home, league="premier_league")
        session.add(h)
        session.flush()
    a = session.scalars(select(Team).where(Team.name == away)).first()
    if a is None:
        a = Team(name=away, league="premier_league")
        session.add(a)
        session.flush()
    m = Match(
        id=match_id,
        league="premier_league", season=season,
        date_utc=kickoff.date(), commence_time_utc=kickoff,
        kickoff_source="understat_placeholder", status=status,
        home_team_id=h.id, away_team_id=a.id,
    )
    session.add(m)
    session.flush()
    if goals is not None or xg is not None:
        session.add(MatchStat(
            match_id=m.id, source="understat",
            home_goals=goals[0] if goals else None,
            away_goals=goals[1] if goals else None,
            home_xg=Decimal(str(xg[0])) if xg else None,
            away_xg=Decimal(str(xg[1])) if xg else None,
        ))
    if fd_goals is not None:
        session.add(MatchStat(
            match_id=m.id, source="football_data",
            home_goals=fd_goals[0], away_goals=fd_goals[1],
        ))
    return m


def seed_league(db_url: str) -> None:
    """Six finished matches, one of each special case."""
    with session_scope(db_url) as s:
        # normal matches over 3 days
        for i, (h, a) in enumerate([
            ("Arsenal", "Chelsea"),
            ("Arsenal", "Liverpool"),
            ("Chelsea", "Liverpool"),
            ("Liverpool", "Arsenal"),
        ]):
            add_match(s, home=h, away=a, kickoff=AS_OF - dt.timedelta(days=i + 1),
                      match_id=100 + i)
        # postponed: excluded despite a finished-like score
        add_match(s, home="Arsenal", away="Everton",
                  kickoff=AS_OF - dt.timedelta(days=2), status="postponed",
                  match_id=200)
        # scheduled future match: excluded (also feeds the team universe)
        add_match(s, home="Everton", away="Arsenal",
                  kickoff=AS_OF + dt.timedelta(days=2), status="scheduled",
                  goals=None, xg=None, match_id=201)
        # finished match exactly AT as_of: excluded (strict <)
        add_match(s, home="Chelsea", away="Everton", kickoff=AS_OF, match_id=202)
        # finished before as_of but no score: excluded
        add_match(s, home="Everton", away="Chelsea",
                  kickoff=AS_OF - dt.timedelta(days=3), goals=None, xg=None,
                  match_id=203)
        # finished, no xG: kept, trained on goals alone
        add_match(s, home="Everton", away="Liverpool",
                  kickoff=AS_OF - dt.timedelta(days=4), xg=None, match_id=204)
        # finished, understat stats missing the score: football-data fallback
        add_match(s, home="Liverpool", away="Everton",
                  kickoff=AS_OF - dt.timedelta(days=5), goals=None,
                  fd_goals=(1, 0), match_id=205)


def test_loader_selects_only_finished_scored_pre_as_of_matches(db_url) -> None:
    seed_league(db_url)
    data = load_training_data(db_url, "premier_league", AS_OF)
    ids = {m.match_id for m in data.matches}
    assert ids == {100, 101, 102, 103, 204, 205}
    m204 = next(m for m in data.matches if m.match_id == 204)
    assert m204.home_xg is None and m204.away_xg is None
    m205 = next(m for m in data.matches if m.match_id == 205)
    assert (m205.home_goals, m205.away_goals) == (1, 0)
    assert data.stats.n_selected == 6
    assert data.stats.n_skipped_no_score == 1
    assert data.stats.n_goals_from_football_data == 1
    assert data.stats.n_missing_xg == 1
    # universe covers every team seen in any league match, incl. future ones
    assert data.team_universe == {
        "Arsenal", "Chelsea", "Liverpool", "Everton"}


def test_loader_excludes_quarantined_match_ids(db_url) -> None:
    seed_league(db_url)
    data = load_training_data(db_url, "premier_league", AS_OF,
                              excluded_match_ids={101, 204})
    assert {m.match_id for m in data.matches} == {100, 102, 103, 205}
    assert data.stats.n_excluded_quarantine == 2


def test_loader_skips_finished_matches_without_kickoff(db_url) -> None:
    with session_scope(db_url) as s:
        add_match(s, home="A", away="B", kickoff=AS_OF - dt.timedelta(days=1))
        m = add_match(s, home="C", away="D", kickoff=AS_OF - dt.timedelta(days=1))
        m.commence_time_utc = None  # cannot prove kickoff < as_of
    data = load_training_data(db_url, "premier_league", AS_OF)
    assert {m.match_id for m in data.matches} == {1}
    assert data.stats.n_skipped_no_kickoff == 1


def test_loader_requires_tzaware_as_of(db_url) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        load_training_data(db_url, "premier_league",
                           dt.datetime(2026, 10, 24, 12, 0))


def test_future_match_never_changes_fit_bit_for_bit(db_url) -> None:
    """Adding a match with kickoff >= as_of must not move any parameter."""
    with session_scope(db_url) as s:
        fixtures = [
            ("Arsenal", "Chelsea", 2, 1), ("Chelsea", "Arsenal", 1, 2),
            ("Arsenal", "Liverpool", 3, 0), ("Liverpool", "Arsenal", 0, 1),
            ("Chelsea", "Liverpool", 2, 2), ("Liverpool", "Chelsea", 1, 0),
        ] * 4
        for i, (h, a, gh, ga) in enumerate(fixtures):
            # one home/away pairing per league season (unique index)
            add_match(s, home=h, away=a, season=2023 + i // 6,
                      kickoff=AS_OF - dt.timedelta(days=i % 20 + 1),
                      goals=(gh, ga), xg=(gh + 0.3, ga + 0.2), match_id=i + 1)
    config = ModelConfig(xi=0.005, weight_xg=0.5)
    first, stats1 = fit_league(db_url, "premier_league", AS_OF, config)
    assert stats1.n_selected == 24

    with session_scope(db_url) as s:
        # finished matches with kickoff exactly at / after as_of: the SQL
        # filter must keep them out of training however suspicious they look
        add_match(s, home="Arsenal", away="Chelsea", season=2027,
                  kickoff=AS_OF, goals=(5, 0), xg=(5.0, 0.0), match_id=999)
        add_match(s, home="Chelsea", away="Arsenal", season=2027,
                  kickoff=AS_OF + dt.timedelta(hours=3),
                  goals=(0, 5), xg=(0.0, 5.0), match_id=1000)

    second, stats2 = fit_league(db_url, "premier_league", AS_OF, config)
    assert stats2.n_selected == stats1.n_selected
    assert second.to_json() == first.to_json()


def test_quarantined_match_ids_parses_b5_entries() -> None:
    report = {
        "football_data": [
            {"league": "ligue_1", "quarantined": [
                "match#10559: fd 2026-08-23 vs db 2027-03-06, score 1:2",
                "match#12: fd 2026-02-06 vs db 2026-02-08, score 0:0",
            ], "inverted_pairs": [
                "fd X v Y (2026-01-01) -> inverted db match#77 2026-01-01",
            ]},
            {"league": "serie_a", "quarantined": []},
        ],
    }
    ids = quarantined_match_ids(report)
    assert ids == {10559, 12}  # inverted-pair matches stay valid

    assert quarantined_match_ids({"football_data": []}) == set()
    assert quarantined_match_ids({"other": 1}) == set()
