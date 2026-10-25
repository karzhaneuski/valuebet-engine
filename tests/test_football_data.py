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
    m = seed_understat_side(url, first.home, first.away,
                            canonical_home=first.home,
                            canonical_away=first.away,
                            understat_home=first.home,
                            understat_away=first.away,
                            commence=dt.datetime(2026, 1, 31, 15, 0,
                                                 tzinfo=dt.UTC))
    # the far-date row is only bound when the understat score agrees;
    # give the match matching stats so the row IS written
    from core.orm import MatchStat

    assert m is not None
    with session_scope(url) as s:
        s.add(MatchStat(match_id=m.id, source="understat",
                        home_goals=first.fthg, away_goals=first.ftag))
    report = loader.upsert([first])
    assert report.matched == 1  # pair matched despite the date difference
    assert report.date_mismatches  # and the >±1 day delta is reported
    assert report.quarantined == [] and report.inverted_pairs == []


def test_unmatched_team_reported_not_skipped_silently(tmp_path) -> None:
    url = make_db(tmp_path)
    config = load_config()
    loader = FootballDataLoader(url, config, cache_dir=tmp_path / "fd")
    rows = loader.parse("premier_league", 2025, raw=FIXTURE.read_bytes())
    first = rows[0]
    report = loader.upsert([first])  # empty DB: no teams at all
    assert set([first.home, first.away]) <= set(report.unmatched_teams)
    assert report.matched == 0


# --- 0.2: classification of far-date rows and report fields ---------------


def _fd_row(date: dt.date, home: str, away: str, hg: int, ag: int):
    from decimal import Decimal

    from core.ingest.football_data import FdRow

    return FdRow(league_slug="premier_league", season=2026, date=date,
                 time=dt.time(12, 30), home=home, away=away,
                 fthg=hg, ftag=ag, avgc_over=Decimal("1.95"),
                 avgc_under=Decimal("1.85"), maxc_over=None,
                 maxc_under=None, pc_over=None, pc_under=None)


def _seed_two_matches(url: str) -> None:
    """The 10559 scene: an exact pairing scheduled months away with no
    stats, and the inverted (already played) pairing on the fd date."""
    import datetime as dtx

    from core.orm import MatchStat

    with session_scope(url) as s:
        rennes = Team(name="Rennes", league="premier_league")
        psg = Team(name="Paris Saint Germain", league="premier_league")
        s.add_all([rennes, psg])
        s.flush()
        for t, a in ((rennes, "Rennes"), (psg, "Paris Saint Germain"),
                     (psg, "PSG")):
            s.add(TeamAlias(team_id=t.id, source="football_data", alias=a))
        future = Match(league="premier_league", season=2026,
                       date_utc=dtx.date(2027, 3, 6),
                       commence_time_utc=dtx.datetime(2027, 3, 6, 20, 0,
                                                      tzinfo=dt.UTC),
                       kickoff_source="understat_placeholder",
                       status="scheduled", home_team_id=rennes.id,
                       away_team_id=psg.id)
        played = Match(league="premier_league", season=2026,
                       date_utc=dtx.date(2026, 8, 23),
                       commence_time_utc=dtx.datetime(2026, 8, 23, 19, 0,
                                                      tzinfo=dt.UTC),
                       kickoff_source="understat_placeholder",
                       status="finished", home_team_id=psg.id,
                       away_team_id=rennes.id)
        s.add_all([future, played])
        s.flush()
        s.add(MatchStat(match_id=played.id, source="understat",
                        home_goals=3, away_goals=1))


def test_far_date_inverted_row_classified_once(tmp_path) -> None:
    """A far-date fd row whose same-day INVERTED fixture exists is an
    inverted pair - and must not also dangle in date_mismatches."""
    url = make_db(tmp_path)
    loader = FootballDataLoader(url, load_config(),
                                cache_dir=tmp_path / "fd")
    _seed_two_matches(url)
    row = _fd_row(dt.date(2026, 8, 23), "Rennes", "PSG", 3, 1)
    report = loader.upsert([row])
    assert report.matched == 1
    assert len(report.inverted_pairs) == 1
    assert report.date_mismatches == []
    assert report.quarantined == []


def test_far_date_without_stats_is_quarantined_only(tmp_path) -> None:
    """Same far-date row but no inverted fixture and no understat score:
    quarantined, not date_mismatches."""
    url = make_db(tmp_path)
    loader = FootballDataLoader(url, load_config(),
                                cache_dir=tmp_path / "fd")
    # exact pairing far in the future, no stats, no inverted leg
    with session_scope(url) as s:
        home = Team(name="Rennes", league="premier_league")
        away = Team(name="Paris Saint Germain", league="premier_league")
        s.add_all([home, away])
        s.flush()
        for t, a in ((home, "Rennes"), (away, "PSG")):
            s.add(TeamAlias(team_id=t.id, source="football_data", alias=a))
        s.add(Match(league="premier_league", season=2026,
                    date_utc=dt.date(2027, 3, 6),
                    commence_time_utc=dt.datetime(2027, 3, 6, 20, 0,
                                                  tzinfo=dt.UTC),
                    kickoff_source="understat_placeholder",
                    status="scheduled", home_team_id=home.id,
                    away_team_id=away.id))
    row = _fd_row(dt.date(2026, 8, 23), "Rennes", "PSG", 0, 0)
    report = loader.upsert([row])
    assert len(report.quarantined) == 1
    assert report.date_mismatches == []
    assert report.inverted_pairs == []


# --- 4b B1: pre-match totals odds -----------------------------------------


def test_prematch_odds_parsed_and_stored(tmp_path) -> None:
    url = make_db(tmp_path)
    loader = FootballDataLoader(url, load_config(),
                                cache_dir=tmp_path / "fd")
    rows = loader.parse("premier_league", 2025, raw=FIXTURE.read_bytes())
    first = rows[0]
    assert first.avg_over is not None and first.p_over is not None
    m = seed_understat_side(url, first.home, first.away,
                            canonical_home=first.home,
                            canonical_away=first.away,
                            understat_home=first.home,
                            understat_away=first.away,
                            commence=dt.datetime(2026, 1, 31, 15, 0,
                                                 tzinfo=dt.UTC))
    assert m is not None
    from core.orm import MatchStat

    with session_scope(url) as s:
        s.add(MatchStat(match_id=m.id, source="understat",
                        home_goals=first.fthg, away_goals=first.ftag))
    report = loader.upsert([first])
    assert report.prematch_written == 3   # Avg, P, Max
    with session_scope(url) as s:
        from core.orm import PrematchOdds

        books = {b.bookmaker: (b.over_price, b.under_price)
                 for b in s.scalars(select(PrematchOdds)).all()}
    assert set(books) == {"Avg", "P", "Max"}
    # idempotent rerun: no second write, no phantom update
    report2 = loader.upsert([first])
    assert report2.prematch_written == 0 and report2.prematch_updated == 0
