"""S6: backfill_lineups behaviour with a substituted loader."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from core.config import load_config
from core.db import get_engine, session_scope
from core.ingest.__main__ import backfill_lineups
from core.ingest.understat import UnderstatLoader
from core.orm import Base, Match, MatchLineup, Team

UTC = dt.UTC


def make_db(tmp_path):
    url = f"sqlite:///{tmp_path}/lineups.db"
    Base.metadata.create_all(get_engine(url))
    return url


def seed_finished(url: str, count: int = 3, understat_from: int = 700) -> None:
    with session_scope(url) as s:
        teams = [Team(name=name) for name in
                 ("Alpha", "Beta", "Gamma", "Delta", "Epsilon", "Zeta")]
        s.add_all(teams)
        s.flush()
        for i in range(count):
            home, away = teams[2 * i], teams[2 * i + 1]
            s.add(Match(league="premier_league", season=2026,
                        date_utc=dt.date(2026, 10, 20 + i),
                        commence_time_utc=dt.datetime(2026, 10, 20 + i, 15, 0,
                                                      tzinfo=UTC),
                        kickoff_source="understat_placeholder",
                        status="finished", home_team_id=home.id,
                        away_team_id=away.id,
                        understat_id=understat_from + i))


class FakeLoader:
    """Substitutes UnderstatLoader in backfill_lineups."""

    def __init__(self, url: str, fail_ids: set[int] | None = None,
                 fail_once_ids: set[int] | None = None):
        self.fail_ids = fail_ids or set()
        self.fail_once_ids = fail_once_ids or set()
        self.downloaded: list[int] = []
        self.attempts: dict[int, int] = {}
        self._url = url
        self._real = UnderstatLoader(url, load_config())
        # lineup_rows must return team names the DB actually has; resolved
        # lazily from the match's teams via understat_id ordering
        self._pairs = self._pair_names()

    def _pair_names(self) -> dict[int, tuple[str, str]]:
        with session_scope(self._url) as s:
            names = {}
            for m in s.scalars(select(Match).where(
                    Match.understat_id.isnot(None))).all():
                uid = m.understat_id
                assert uid is not None  # filtered in the query
                names[int(uid)] = (m.home_team.name, m.away_team.name)
        return names

    def lineup_rows(self, understat_id: int, league_slug: str,
                    season: int) -> list[dict]:
        self.attempts[understat_id] = self.attempts.get(understat_id, 0) + 1
        if understat_id in self.fail_ids or \
                (understat_id in self.fail_once_ids
                 and self.attempts[understat_id] == 1):
            raise RuntimeError("429 too many requests")
        self.downloaded.append(understat_id)
        home, away = self._pairs[understat_id]
        return [{"team": home, "player": f"P{understat_id}-1",
                 "understat_player_id": understat_id * 100,
                 "started": True, "minutes": 90},
                {"team": away, "player": f"P{understat_id}-2",
                 "understat_player_id": understat_id * 100 + 1,
                 "started": False, "minutes": 12}]

    def upsert_lineups(self, match, rows: list[dict]) -> int:
        return self._real.upsert_lineups(match, rows)



def test_s6_skips_loaded_and_continues_after_failure(tmp_path) -> None:
    url = make_db(tmp_path)
    seed_finished(url, count=3)
    loader = FakeLoader(url, fail_ids={701})

    report = backfill_lineups(url, load_config(), "2026-10-01",
                              loader=loader)

    assert 701 in report["failed_matches"][0].values() or \
        any(f["understat_id"] == 701 for f in report["failed_matches"])
    assert report["matches"] == 3
    assert report["rows_written"] == 4  # 2 matches x 2 rows
    assert sorted(loader.downloaded) == [700, 702]  # 701 failed and skipped
    with session_scope(url) as s:
        rows = s.scalars(select(MatchLineup)).all()
        assert len(rows) == 4


def test_s6_resume_does_not_redownload(tmp_path) -> None:
    url = make_db(tmp_path)
    seed_finished(url, count=3)
    # first run: everything succeeds
    first = FakeLoader(url)
    backfill_lineups(url, load_config(), "2026-10-01", loader=first)
    assert len(first.downloaded) == 3
    # second run: nothing is downloaded (all matches have lineups)
    second = FakeLoader(url)
    report = backfill_lineups(url, load_config(), "2026-10-01",
                              loader=second)
    assert second.downloaded == []
    assert report["matches"] == 0 and report["rows_written"] == 0


def test_s6_retry_then_success_and_report(tmp_path) -> None:
    url = make_db(tmp_path)
    seed_finished(url, count=2, understat_from=800)
    # 800 fails once then succeeds; 801 fails permanently
    loader = FakeLoader(url, fail_ids={801}, fail_once_ids={800})
    config = load_config().model_copy(deep=True)
    config.ingest.retry_attempts = 2
    config.ingest.retry_backoff_s = 0.0  # no sleeping in tests

    report = backfill_lineups(url, config, "2026-10-01", loader=loader)

    assert loader.attempts[800] == 2  # retried after the 429
    assert loader.attempts[801] == 2  # exhausted the attempts
    assert any(f["understat_id"] == 801 for f in report["failed_matches"])
    assert report["rows_written"] == 2  # only match 800 settled


# --- 0.1/0.3: duplicate roster rows, error text, one reader per season ----


class FakeReader:
    """Mimics soccerdata.Understat: read_schedule once, then per-match
    player stats (with duplicates like the real match 30804 data)."""

    def __init__(self, frames: dict[int, object], calls: dict):
        self._frames = frames
        self._calls = calls
        self.schedule_calls = 0

    def read_schedule(self, **_: object):
        self.schedule_calls += 1
        self._calls["readers"] = self._calls.get("readers", 0)
        import pandas as pd

        return pd.DataFrame()

    def read_player_match_stats(self, match_id: int):
        return self._frames[int(match_id)]


def _dup_frame():
    """Rows like understat match 30804: each player duplicated with
    different surrogate ids / roster_in references."""
    import pandas as pd

    rows = []
    for pid, player, team, minutes in [
        (1, "Alpha One", "Alpha", 90), (2, "Alpha Two", "Alpha", 64),
        (3, "Alpha Sub", "Alpha", 26), (11, "Beta One", "Beta", 90),
    ]:
        for _ in range(2):  # the understat glitch: every row twice
            rows.append({"team": team, "player": player,
                         "player_id": pid, "position": "Sub" if minutes < 30
                         else "MC", "minutes": minutes})
    return pd.DataFrame(rows)


def test_lineup_rows_deduplicates_understat_glitch(tmp_path) -> None:
    frame = _dup_frame()
    calls: dict = {}
    loader = UnderstatLoader(
        f"sqlite:///{tmp_path}/x.db", load_config(),
        reader_factory=lambda lg, se: FakeReader({700: frame}, calls))
    rows = loader.lineup_rows(700, "premier_league", 2026)
    assert len(rows) == 4                       # 8 rows -> 4 unique players
    assert {r["understat_player_id"] for r in rows} == {1, 2, 3, 11}
    assert rows[0]["started"] is True and rows[2]["started"] is False


def test_one_reader_and_one_schedule_read_per_league_season(tmp_path) -> None:
    frame = _dup_frame()
    calls: dict = {"factories": 0}
    readers: list[FakeReader] = []

    def factory(lg: str, se: int) -> FakeReader:
        calls["factories"] += 1
        r = FakeReader({700 + i: frame for i in range(3)}, calls)
        readers.append(r)
        return r

    loader = UnderstatLoader(f"sqlite:///{tmp_path}/x.db", load_config(),
                             reader_factory=factory)
    for i in range(3):
        loader.lineup_rows(700 + i, "premier_league", 2026)
    assert calls["factories"] == 1              # one reader per season
    assert readers[0].schedule_calls == 1       # schedule read once, pinned
    loader.lineup_rows(700, "la_liga", 2026)    # other league -> new reader
    assert calls["factories"] == 2


def test_failed_matches_carry_error_text(tmp_path) -> None:
    url = make_db(tmp_path)
    seed_finished(url, count=1, understat_from=900)

    class ExplodingLoader(FakeLoader):
        def lineup_rows(self, understat_id: int, league_slug: str,
                        season: int) -> list[dict]:
            raise ValueError("boom boom")

    config = load_config().model_copy(deep=True)
    config.ingest.retry_attempts = 1
    report = backfill_lineups(url, config, "2026-10-01",
                              loader=ExplodingLoader(url))
    assert len(report["failed_matches"]) == 1
    error = report["failed_matches"][0]["error"]
    assert "ValueError" in error and "boom boom" in error
