"""football-data loader: parse fixture, pair-rule upsert, closing odds."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from sqlalchemy import select

from core.config import load_config
from core.db import get_engine, session_scope
from core.ingest.football_data import FootballDataLoader
from core.orm import Base, ClosingOdds, Match, MatchStat, Team, TeamAlias

FIXTURE = Path(__file__).parent / "fixtures" / "fd_e0_2526.csv"


def make_db(tmp_path):
    url = f"sqlite:///{tmp_path}/fd.db"
    Base.metadata.create_all(get_engine(url))
    return url


def seed_understat_side(url: str, home_fd: str, away_fd: str,
                        canonical_home: str, canonical_away: str,
                        understat_home: str, understat_away: str,
                        commence: dt.datetime) -> Match | None:
    """Simulate that the Understat loader already created the match."""
    with session_scope(url) as s:
        home = Team(name=canonical_home, league="premier_league")
        away = Team(name=canonical_away, league="premier_league")
        s.add_all([home, away])
        s.flush()
        for team, aliases in ((home, ("toa", canonical_home,
                                      "understat", understat_home)),
                              (away, ("toa", canonical_away,
                                      "understat", understat_away))):
            for i in range(0, len(aliases), 2):
                s.add(TeamAlias(team_id=team.id, source=aliases[i],
                                alias=aliases[i + 1]))
        m = Match(league="premier_league", season=2025,
                  date_utc=commence.date(), commence_time_utc=commence,
                  kickoff_source="understat_placeholder", status="finished",
                  home_team_id=home.id, away_team_id=away.id,
                  understat_id=999999)
        s.add(m)
        s.flush()
        match_id = m.id
    with session_scope(url) as s:
        return s.get(Match, match_id)


def test_parse_fixture_and_upsert(tmp_path) -> None:
    url = make_db(tmp_path)
    config = load_config()
    loader = FootballDataLoader(url, config, cache_dir=tmp_path / "fd")

    rows = loader.parse("premier_league", 2025, raw=FIXTURE.read_bytes())
    assert rows, "fixture must parse to rows"
    first = rows[0]
    assert first.time is not None  # 2025-26 files carry Time
    assert first.avgc_over is not None

    # seed the DB side for ONE fixture row: the first pairing
    fd_home, fd_away = first.home, first.away
    seed_understat_side(url, fd_home, fd_away,
                        canonical_home=fd_home, canonical_away=fd_away,
                        understat_home=fd_home, understat_away=fd_away,
                        commence=dt.datetime.combine(
                            first.date, dt.time(14, 0), tzinfo=dt.UTC))
    report = loader.upsert([first])
    assert report.matched == 1
    assert not report.unmatched_teams
    with session_scope(url) as s:
        stats = s.scalars(select(MatchStat).where(
            MatchStat.source == "football_data")).all()
        assert len(stats) == 1
        closing = s.scalars(select(ClosingOdds)).all()
        assert closing, "closing odds must be written"
        assert any(c.bookmaker == "AvgC" for c in closing)

    # idempotent re-run writes nothing new
    report2 = loader.upsert([first])
    assert report2.stats_written == 0 and report2.closing_written == 0


def test_pair_not_date_is_the_join_key(tmp_path) -> None:
    """The fd row matches the season pairing even when its date differs by
    months from the stored match (postponed fixture); >±1 day is reported."""
    url = make_db(tmp_path)
    config = load_config()
    loader = FootballDataLoader(url, config, cache_dir=tmp_path / "fd")
    rows = loader.parse("premier_league", 2025, raw=FIXTURE.read_bytes())
    first = rows[0]
    seed_understat_side(url, first.home, first.away,
                        canonical_home=first.home,
                        canonical_away=first.away,
                        understat_home=first.home,
                        understat_away=first.away,
                        commence=dt.datetime(2026, 1, 31, 15, 0,
                                             tzinfo=dt.UTC))
    report = loader.upsert([first])
    assert report.matched == 1  # pair matched despite the date difference
    assert report.date_mismatches  # and the >±1 day delta is reported


def test_unmatched_team_reported_not_skipped_silently(tmp_path) -> None:
    url = make_db(tmp_path)
    config = load_config()
    loader = FootballDataLoader(url, config, cache_dir=tmp_path / "fd")
    rows = loader.parse("premier_league", 2025, raw=FIXTURE.read_bytes())
    first = rows[0]
    report = loader.upsert([first])  # empty DB: no teams at all
    assert set([first.home, first.away]) <= set(report.unmatched_teams)
    assert report.matched == 0
