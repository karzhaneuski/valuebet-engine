"""Tests for job_refresh_history error handling, filtering, and Result population."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from core.config import load_config
from core.ingest.understat import UnderstatLoader, UnderstatUnavailableError
from core.orm import Base, Match, MatchStat, Result, Team, TeamAlias
from core.scheduler.tasks import job_refresh_history

FIXED_NOW = dt.datetime(2026, 11, 15, 12, 0, tzinfo=dt.UTC)


def make_test_db(tmp_path: Path) -> tuple[str, sessionmaker[Session]]:
    db_file = tmp_path / "test_sources.db"
    url = f"sqlite:///{db_file}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    return url, sessionmaker(bind=engine)


def seed_teams(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        t1 = Team(name="Barcelona", league="la_liga")
        t2 = Team(name="Real Madrid", league="la_liga")
        t3 = Team(name="Arsenal", league="premier_league")
        t4 = Team(name="Chelsea", league="premier_league")
        session.add_all([t1, t2, t3, t4])
        session.flush()

        for t in (t1, t2, t3, t4):
            session.add(TeamAlias(team_id=t.id, source="understat", alias=t.name))
            session.add(TeamAlias(team_id=t.id, source="football_data", alias=t.name))
        session.commit()


def test_first_league_json_decode_error_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """a) first league raises a JSON decode error -> 'source_unavailable', zero rows written,
    football-data loader never constructed (spy), later leagues' readers never called."""
    db_url, sm = make_test_db(tmp_path)
    seed_teams(sm)
    cfg = load_config()

    called_leagues: list[str] = []

    class ExplodingReader:
        def __init__(self, league: str, season: int) -> None:
            self.league = league
            self.season = season

        def read_schedule(self) -> pd.DataFrame:
            called_leagues.append(self.league)
            raise ValueError("Expecting value: line 1 column 1 (char 0) - JSON decode error")

    loader = UnderstatLoader(
        db_url=db_url,
        config=cfg,
        reader_factory=lambda lg, se: ExplodingReader(lg, se),
    )

    fd_constructed = []

    import core.ingest.football_data as fd_module

    orig_fd = fd_module.FootballDataLoader

    def fd_spy(*args: Any, **kwargs: Any) -> Any:
        fd_constructed.append(True)
        return orig_fd(*args, **kwargs)

    monkeypatch.setattr(fd_module, "FootballDataLoader", fd_spy)

    res = job_refresh_history(
        session_factory=sm,
        understat_loader=loader,
        config=cfg,
        now=FIXED_NOW,
        on_source_error="stop",
    )

    assert res["status"] == "source_unavailable"
    assert res["source"] == "understat"
    assert res["league"] == cfg.leagues[0].slug
    assert "error" in res
    assert len(fd_constructed) == 0, "football-data loader should never be constructed on stop"
    assert len(called_leagues) == 1, "later leagues' readers should never be called"

    with sm() as session:
        matches = list(session.scalars(select(Match)).all())
        match_stats = list(session.scalars(select(MatchStat)).all())
        results = list(session.scalars(select(Result)).all())
        assert len(matches) == 0
        assert len(match_stats) == 0
        assert len(results) == 0


def test_empty_dataframe_success(tmp_path: Path) -> None:
    """b) empty DataFrame with correct columns -> not a failure, 'success'."""
    db_url, sm = make_test_db(tmp_path)
    seed_teams(sm)
    cfg = load_config()

    cols = [
        "game_id",
        "home_team",
        "away_team",
        "date",
        "is_result",
        "home_goals",
        "away_goals",
        "home_xg",
        "away_xg",
    ]

    class EmptyReader:
        def __init__(self, league: str, season: int) -> None:
            pass

        def read_schedule(self) -> pd.DataFrame:
            return pd.DataFrame(columns=cols)

    loader = UnderstatLoader(
        db_url=db_url,
        config=cfg,
        reader_factory=lambda lg, se: EmptyReader(lg, se),
    )

    res = job_refresh_history(
        session_factory=sm,
        understat_loader=loader,
        config=cfg,
        now=FIXED_NOW,
        on_source_error="stop",
    )

    assert res["status"] == "success"
    assert res["updated_results"] == 0
    assert res["results_inserted"] == 0
    assert res["results_updated"] == 0


def test_on_source_error_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """c) on_source_error='fallback' with failing league -> 'partial', names fallback source."""
    db_url, sm = make_test_db(tmp_path)
    seed_teams(sm)
    cfg = load_config()

    cols = [
        "game_id",
        "home_team",
        "away_team",
        "date",
        "is_result",
        "home_goals",
        "away_goals",
        "home_xg",
        "away_xg",
    ]

    class MixedReader:
        def __init__(self, league: str, season: int) -> None:
            self.league = league

        def read_schedule(self) -> pd.DataFrame:
            if "La Liga" in self.league:
                raise UnderstatUnavailableError("La Liga Understat down")
            return pd.DataFrame(columns=cols)

    loader = UnderstatLoader(
        db_url=db_url,
        config=cfg,
        reader_factory=lambda lg, se: MixedReader(lg, se),
    )

    import core.ingest.football_data as fd_module

    class FakeFDLoader:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def download(self, slug: str, season: int) -> None:
            pass

        def parse(self, slug: str, season: int) -> list[Any]:
            return []

        def upsert(self, rows: list[Any]) -> fd_module.FdReport:
            return fd_module.FdReport()

    monkeypatch.setattr(fd_module, "FootballDataLoader", FakeFDLoader)

    res = job_refresh_history(
        session_factory=sm,
        understat_loader=loader,
        config=cfg,
        now=FIXED_NOW,
        on_source_error="fallback",
    )

    assert res["status"] == "partial"
    assert res["sources_used"]["la_liga"] == "football-data"
    assert "la_liga" in res["failed_sources"]


def test_filters_leagues_and_unknown_slug(tmp_path: Path) -> None:
    """d) leagues=['la_liga'] -> only that reader called; unknown slug -> ValueError."""
    db_url, sm = make_test_db(tmp_path)
    seed_teams(sm)
    cfg = load_config()

    called_leagues: list[str] = []
    cols = [
        "game_id",
        "home_team",
        "away_team",
        "date",
        "is_result",
        "home_goals",
        "away_goals",
        "home_xg",
        "away_xg",
    ]

    class TrackingReader:
        def __init__(self, league: str, season: int) -> None:
            self.league = league

        def read_schedule(self) -> pd.DataFrame:
            called_leagues.append(self.league)
            return pd.DataFrame(columns=cols)

    loader = UnderstatLoader(
        db_url=db_url,
        config=cfg,
        reader_factory=lambda lg, se: TrackingReader(lg, se),
    )

    # Test unknown slug -> ValueError
    with pytest.raises(ValueError, match="Unknown league slug"):
        job_refresh_history(
            session_factory=sm,
            understat_loader=loader,
            config=cfg,
            now=FIXED_NOW,
            leagues=["unknown_slug_xyz"],
        )

    # Test leagues=['la_liga']
    res = job_refresh_history(
        session_factory=sm,
        understat_loader=loader,
        config=cfg,
        now=FIXED_NOW,
        leagues=["la_liga"],
    )
    assert res["status"] == "success"
    assert called_leagues == ["ESP-La Liga"]
    assert res["leagues_done"] == ["la_liga"]


def test_since_filter_date(tmp_path: Path) -> None:
    """e) since=date -> older rows not upserted."""
    db_url, sm = make_test_db(tmp_path)
    seed_teams(sm)
    cfg = load_config()

    df = pd.DataFrame([
        {
            "game_id": 101,
            "home_team": "Barcelona",
            "away_team": "Real Madrid",
            "date": pd.Timestamp("2026-11-01 20:00:00"),
            "is_result": True,
            "home_goals": 2,
            "away_goals": 1,
            "home_xg": 2.1,
            "away_xg": 1.0,
        },
        {
            "game_id": 102,
            "home_team": "Barcelona",
            "away_team": "Real Madrid",
            "date": pd.Timestamp("2026-11-10 20:00:00"),
            "is_result": True,
            "home_goals": 3,
            "away_goals": 0,
            "home_xg": 3.0,
            "away_xg": 0.5,
        },
    ])

    class StaticReader:
        def __init__(self, league: str, season: int) -> None:
            pass

        def read_schedule(self) -> pd.DataFrame:
            return df

    loader = UnderstatLoader(
        db_url=db_url,
        config=cfg,
        reader_factory=lambda lg, se: StaticReader(lg, se),
    )

    since_date = dt.date(2026, 11, 5)
    res = job_refresh_history(
        session_factory=sm,
        understat_loader=loader,
        config=cfg,
        now=FIXED_NOW,
        leagues=["la_liga"],
        since=since_date,
    )

    assert res["status"] == "success"
    with sm() as session:
        matches = list(session.scalars(select(Match)).all())
        assert len(matches) == 1
        assert matches[0].understat_id == 102


def test_second_run_zero_result_rows_updated(tmp_path: Path) -> None:
    """f) second run with identical data -> zero Result rows updated."""
    db_url, sm = make_test_db(tmp_path)
    seed_teams(sm)
    cfg = load_config()

    df = pd.DataFrame([
        {
            "game_id": 201,
            "home_team": "Barcelona",
            "away_team": "Real Madrid",
            "date": pd.Timestamp("2026-11-08 20:00:00"),
            "is_result": True,
            "home_goals": 2,
            "away_goals": 1,
            "home_xg": 1.9,
            "away_xg": 1.2,
        }
    ])

    class StaticReader:
        def __init__(self, league: str, season: int) -> None:
            pass

        def read_schedule(self) -> pd.DataFrame:
            return df

    loader = UnderstatLoader(
        db_url=db_url,
        config=cfg,
        reader_factory=lambda lg, se: StaticReader(lg, se),
    )

    res1 = job_refresh_history(
        session_factory=sm,
        understat_loader=loader,
        config=cfg,
        now=FIXED_NOW,
        leagues=["la_liga"],
    )
    assert res1["status"] == "success"
    assert res1["results_inserted"] == 1
    assert res1["results_updated"] == 0

    res2 = job_refresh_history(
        session_factory=sm,
        understat_loader=loader,
        config=cfg,
        now=FIXED_NOW,
        leagues=["la_liga"],
    )
    assert res2["status"] == "success"
    assert res2["results_inserted"] == 0
    assert res2["results_updated"] == 0


def test_unrelated_error_propagates_not_masked_as_outage(tmp_path: Path) -> None:
    """Unrelated error (e.g. AttributeError or ZeroDivisionError) makes job_refresh_history raise,
    not return 'source_unavailable'."""
    db_url, sm = make_test_db(tmp_path)
    seed_teams(sm)
    cfg = load_config()

    class BuggyReader:
        def __init__(self, league: str, season: int) -> None:
            pass

        def read_schedule(self) -> pd.DataFrame:
            raise ZeroDivisionError("division by zero in custom code")

    loader = UnderstatLoader(
        db_url=db_url,
        config=cfg,
        reader_factory=lambda lg, se: BuggyReader(lg, se),
    )

    with pytest.raises(ZeroDivisionError, match="division by zero"):
        job_refresh_history(
            session_factory=sm,
            understat_loader=loader,
            config=cfg,
            now=FIXED_NOW,
            on_source_error="stop",
        )


def test_schedule_rows_keyerror_after_required_columns_propagates(tmp_path: Path) -> None:
    """KeyError raised while mapping a row after REQUIRED_COLUMNS check stays a KeyError."""
    db_url, _ = make_test_db(tmp_path)
    cfg = load_config()

    cols = [
        "game_id",
        "home_team",
        "away_team",
        "date",
        "is_result",
        "home_goals",
        "away_goals",
        "home_xg",
        "away_xg",
    ]

    class ExplodingSeries(pd.Series):
        def __getitem__(self, key):
            if key == "home_team":
                raise KeyError("unrelated_missing_item_lookup")
            return super().__getitem__(key)

    class CustomDf(pd.DataFrame):
        def reset_index(self, **kwargs):
            return self

        def iterrows(self):
            row_data = {
                "game_id": 1,
                "home_team": "Home",
                "away_team": "Away",
                "date": pd.Timestamp("2026-11-01 20:00:00"),
                "is_result": False,
                "home_goals": None,
                "away_goals": None,
                "home_xg": None,
                "away_xg": None,
            }
            yield 0, ExplodingSeries(row_data)

    df = CustomDf(columns=cols)

    class ReaderWithBadRow:
        def __init__(self, league: str, season: int) -> None:
            pass

        def read_schedule(self) -> pd.DataFrame:
            return df

    loader = UnderstatLoader(
        db_url=db_url,
        config=cfg,
        reader_factory=lambda lg, se: ReaderWithBadRow(lg, se),
    )

    with pytest.raises(KeyError, match="unrelated_missing_item_lookup"):
        loader.schedule_rows("la_liga", 2026)


def test_manual_ingest_path_creates_result_and_second_run_updates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Manual ingest creates Result for finished match; second run updates nothing."""
    import argparse

    from core.db import session_scope
    from core.ingest import __main__ as ingest_main
    from core.ingest.results import populate_results

    db_url, sm = make_test_db(tmp_path)
    seed_teams(sm)
    cfg = load_config()

    monkeypatch.setattr(ingest_main, "_db_url", lambda: db_url)
    monkeypatch.setattr(ingest_main, "load_config", lambda: cfg)

    df = pd.DataFrame([
        {
            "game_id": 301,
            "home_team": "Barcelona",
            "away_team": "Real Madrid",
            "date": pd.Timestamp("2026-11-08 20:00:00"),
            "is_result": True,
            "home_goals": 2,
            "away_goals": 1,
            "home_xg": 1.5,
            "away_xg": 0.8,
        }
    ])

    class StaticReader:
        def __init__(self, league: str, season: int) -> None:
            pass

        def read_schedule(self) -> pd.DataFrame:
            return df

    # We also mock UnderstatLoader inside ingest_main to use StaticReader
    class MockUnderstatLoader(UnderstatLoader):
        def __init__(self, db_url_arg: str, config_arg: Any, **kwargs: Any) -> None:
            super().__init__(
                db_url=db_url_arg,
                config=config_arg,
                reader_factory=lambda lg, se: StaticReader(lg, se),
                **kwargs,
            )

    monkeypatch.setattr(ingest_main, "UnderstatLoader", MockUnderstatLoader)

    import core.ingest.football_data as fd_module

    class FakeFDLoader:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def download(self, slug: str, season: int) -> None:
            pass

        def parse(self, slug: str, season: int) -> list[Any]:
            return []

        def upsert(self, rows: list[Any]) -> fd_module.FdReport:
            return fd_module.FdReport()

    monkeypatch.setattr(ingest_main, "FootballDataLoader", FakeFDLoader)
    monkeypatch.setattr(ingest_main, "TOP5", ["la_liga"])

    report_path = tmp_path / "backfill_report.json"

    def fake_path(p: Any) -> Path:
        return report_path if "backfill_report.json" in str(p) else tmp_path / p

    monkeypatch.setattr("core.ingest.__main__.Path", fake_path)

    # Run 1
    args = argparse.Namespace(seasons=[2026], lineups_since=None, lineups_only=False)
    ingest_main.cmd_backfill(args)

    with session_scope(db_url) as session:
        m = session.scalar(select(Match).where(Match.understat_id == 301))
        assert m is not None
        assert m.status == "finished"
        r = session.scalar(select(Result).where(Result.match_id == m.id))
        assert r is not None
        assert r.home_goals == 2
        assert r.away_goals == 1

        # Second run of populate_results updates nothing
        ins, upd = populate_results(session)
        assert ins == 0
        assert upd == 0


def test_poisoned_cache_cleaned_on_invalid_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When schedule_rows raises UnderstatUnavailableError because of invalid JSON,
    remove cache files in loader's cache dir that are not valid JSON (.json suffix only)."""
    db_url, _ = make_test_db(tmp_path)
    cfg = load_config()

    cache_dir = tmp_path / "understat_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    valid_file = cache_dir / "valid_data.json"
    valid_file.write_text('{"status": "ok"}', encoding="utf-8")

    html_file = cache_dir / "leagues.json"
    html_file.write_text(
        "<html><head><title>503 Service Unavailable</title></head></html>",
        encoding="utf-8",
    )

    # In UnderstatLoader, if reader_factory is None, creates soccerdata.Understat
    # We mock soccerdata.Understat so read_schedule raises ValueError with "json" in message
    import sys
    import types

    fake_soccerdata = types.ModuleType("soccerdata")

    class FakeUnderstatSD:
        def __init__(self, leagues: Any = None, seasons: Any = None, data_dir: Any = None) -> None:
            pass

        def read_schedule(self) -> pd.DataFrame:
            raise ValueError("Expecting value: line 1 column 1 (char 0) JSONDecodeError")

    fake_soccerdata.Understat = FakeUnderstatSD  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "soccerdata", fake_soccerdata)

    loader = UnderstatLoader(
        db_url=db_url,
        config=cfg,
        cache_dir=cache_dir,
        reader_factory=None,
    )

    with pytest.raises(UnderstatUnavailableError):
        loader.schedule_rows("la_liga", 2026)

    # After failure, html file must be deleted, valid file must remain
    assert not html_file.exists()
    assert valid_file.exists()

