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

    def lineup_rows(self, understat_id: int) -> list[dict]:
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
