"""Phase 4 infra: test-period lock, walk-forward lag safety, model cache."""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

import pytest
from sqlalchemy import select

from core.backtest.metrics import score_records
from core.backtest.periods import (
    PeriodLockedError,
    TestPeriodLockedError,
    ensure_period_allowed,
    period_of_season,
)
from core.backtest.runner import fitted_model, predictions
from core.config import ModelConfig
from core.db import get_engine, session_scope
from core.orm import Base, Match, MatchStat, Team

UTC = dt.UTC
AS_OF = dt.datetime(2026, 3, 6, 12, 0, tzinfo=UTC)


@pytest.fixture()
def db_url(tmp_path):
    url = f"sqlite:///{tmp_path}/bt.db"
    Base.metadata.create_all(get_engine(url))
    with session_scope(url) as s:
        teams = {n: Team(name=n, league="premier_league") for n in
                 ("Alpha", "Beta", "Gamma", "Delta", "Epsilon", "Zeta")}
        s.add_all(teams.values())
        s.flush()
        mid = 0
        teams_names = ("Alpha", "Beta", "Gamma", "Delta")
        for season, day0 in ((2023, 400), (2024, 200), (2025, 30),
                             (2026, 10)):
            pairings = [(h, a) for h in teams_names for a in teams_names
                        if h != a]   # 12 ordered pairs, unique per season
            for k, (h, a) in enumerate(pairings):
                mid += 1
                kickoff = AS_OF - dt.timedelta(days=day0 - k % 40)
                if kickoff >= AS_OF:
                    kickoff = AS_OF - dt.timedelta(days=3)
                s.add(Match(
                    league="premier_league", season=season,
                    date_utc=kickoff.date(), commence_time_utc=kickoff,
                    kickoff_source="understat_placeholder", status="finished",
                    home_team_id=teams[h].id, away_team_id=teams[a].id,
                    id=mid))
                s.add(MatchStat(match_id=mid, source="understat",
                                home_goals=k % 3, away_goals=(k + 1) % 3,
                                home_xg=Decimal("1.1"), away_xg=Decimal("0.9")))
        # prediction-week targets: finished a few days AFTER the first as_of
        # fresh teams (prior-only) + repeat pairings for depth
        for k, (h, a) in enumerate([("Epsilon", "Zeta"), ("Zeta", "Epsilon"),
                                    ("Epsilon", "Alpha"), ("Zeta", "Beta")]):
            mid += 1
            kickoff = AS_OF + dt.timedelta(days=1 + k)
            s.add(Match(
                league="premier_league", season=2026,
                date_utc=kickoff.date(), commence_time_utc=kickoff,
                kickoff_source="understat_placeholder", status="finished",
                home_team_id=teams[h].id, away_team_id=teams[a].id,
                id=mid))
            s.add(MatchStat(match_id=mid, source="understat",
                            home_goals=k % 3, away_goals=(k + 1) % 3,
                            home_xg=Decimal("1.1"), away_xg=Decimal("0.9")))
    return url


# --- test-period lock -------------------------------------------------------


def test_period_of_season_mapping() -> None:
    assert period_of_season(2021) == "warmup"
    assert period_of_season(2023) == "validation"
    assert period_of_season(2024) == "validation"
    assert period_of_season(2025) == "test"
    assert period_of_season(2026) == "test"
    assert period_of_season(2030) is None


def test_test_period_locked_without_frozen_config(tmp_path, db_url) -> None:
    assert issubclass(TestPeriodLockedError, PeriodLockedError)
    frozen = tmp_path / "FROZEN_CONFIG.json"
    with pytest.raises(PeriodLockedError):
        ensure_period_allowed("test", frozen_config=frozen)
    # validation/warmup are always allowed
    ensure_period_allowed("validation", frozen_config=frozen)
    ensure_period_allowed("warmup", frozen_config=frozen)
    # creating the file unlocks the test period
    frozen.write_text(json.dumps({"xi": 0.005}))
    ensure_period_allowed("test", frozen_config=frozen)


def test_score_records_gates_and_filters(tmp_path) -> None:
    frozen = tmp_path / "FROZEN_CONFIG.json"
    records = [{"season": 2023, "league": "l", "depth_bin": ">15",
                "log_p": -1.0, "home_goals": 1, "away_goals": 1},
               {"season": 2025, "league": "l", "depth_bin": ">15",
                "log_p": -2.0, "home_goals": 1, "away_goals": 1}]
    val = score_records(records, "validation")
    assert len(val) == 1 and val[0]["season"] == 2023
    # the lock applies even when no test records would be returned; use a
    # dedicated absent path so a repo-level FROZEN_CONFIG.json (created in
    # part 2.3) does not satisfy the lock for this test
    with pytest.raises(PeriodLockedError):
        score_records([{"season": 2023}], "test",
                      frozen_config=tmp_path / "absent.json")
    frozen.write_text("{}")
    assert score_records(records, "test", frozen_config=frozen) \
        == [records[1]]


# --- walk-forward lag safety ------------------------------------------------


def test_predictions_never_train_on_the_predicted_week(db_url, tmp_path) -> None:
    """Adding a match whose result cannot be known at the first as_of
    must not change the first week's fitted parameters bit-for-bit."""
    cache = tmp_path / "cache"
    config = ModelConfig(xi=0.005, weight_xg=0.5)
    start = AS_OF
    end = start + dt.timedelta(days=8)
    first = predictions(db_url, config, start, end, leagues=("premier_league",),
                        cache_dir=cache)
    assert first, "fixture must yield predictions"
    # snapshot the cached model for the first week
    from core.backtest.runner import _cache_path
    from core.model.dixon_coles import config_hash

    path = _cache_path(cache, "premier_league", start, config_hash(config))
    before = path.read_text()

    # a match INSIDE the first prediction week (kickoff = as_of + 1 day),
    # already marked finished with stats: must not enter training
    with session_scope(db_url) as s:
        gamma = s.scalars(select(Team).where(Team.name == "Gamma")).one()
        eps = s.scalars(select(Team).where(Team.name == "Epsilon")).one()
        m = Match(league="premier_league", season=2026,
                  date_utc=(start + dt.timedelta(days=2)).date(),
                  commence_time_utc=start + dt.timedelta(days=2),
                  kickoff_source="understat_placeholder", status="finished",
                  home_team_id=gamma.id, away_team_id=eps.id, id=99001)
        s.add(m)
        s.add(MatchStat(match_id=99001, source="understat", home_goals=5,
                        away_goals=0, home_xg=Decimal("5.0"),
                        away_xg=Decimal("0.0")))

    second = predictions(db_url, config, start, end,
                         leagues=("premier_league",), cache_dir=tmp_path / "c2")
    # identical parameters: the week's own results never train it
    assert _cache_path(tmp_path / "c2", "premier_league", start,
                       config_hash(config)).read_text() == before
    # and the leaked-looking match is only ever a prediction target
    assert {r["match_id"] for r in second} >= {99001}


# --- model cache ------------------------------------------------------------


def test_cache_invalidates_on_config_change(db_url, tmp_path) -> None:
    cache = tmp_path / "cache"
    c1 = ModelConfig(xi=0.005, weight_xg=0.5)
    m1, cached1 = fitted_model(db_url, "premier_league", AS_OF, c1,
                               cache_dir=cache)
    assert cached1 is False
    files1 = list(cache.iterdir())
    assert len(files1) == 1
    m1b, cached1b = fitted_model(db_url, "premier_league", AS_OF, c1,
                                 cache_dir=cache)
    assert cached1b is True
    assert m1b.to_json() == m1.to_json()
    # a different config writes a NEW cache file and refits
    c2 = ModelConfig(xi=0.0075, weight_xg=0.5)
    m2, cached2 = fitted_model(db_url, "premier_league", AS_OF, c2,
                               cache_dir=cache)
    assert cached2 is False
    assert len(list(cache.iterdir())) == 2
    assert m2.to_json() != m1.to_json()


def test_week_models_common_rho_single_value(db_url, tmp_path) -> None:
    from core.backtest.runner import week_models

    config = ModelConfig(xi=0.005, weight_xg=0.5, common_rho=True)
    models = week_models(db_url, ("premier_league",), AS_OF,
                         config, tmp_path / "cache", frozenset())
    m = models["premier_league"]
    # single league: the common rho is its own pass-A rho (weighted mean
    # of one model), and pass B pins it exactly
    assert -1.0 < m.rho < 1.0
    # pass A and pass B produce distinct cache entries
    assert len(list((tmp_path / "cache").iterdir())) == 2


def test_date_lock_refuses_windows_reaching_into_test(tmp_path, db_url) -> None:
    """Review S2: a walk-forward window ENDING in the test dates is
    refused without a frozen config even if it would predict no matches."""

    from core.backtest.periods import (
        TEST_START,
        PeriodLockedError,
        ensure_window_allowed,
    )
    from core.backtest.runner import predictions

    frozen = tmp_path / "absent.json"
    with pytest.raises(PeriodLockedError):
        ensure_window_allowed(
            dt.datetime(2025, 1, 1, tzinfo=UTC),
            dt.datetime(2025, 8, 1, tzinfo=UTC), frozen_config=frozen)
    # a window fully before the test start is fine
    ensure_window_allowed(
        dt.datetime(2025, 1, 1, tzinfo=UTC),
        dt.datetime(2025, 6, 30, tzinfo=UTC), frozen_config=frozen)
    # and creating the file unlocks
    frozen.write_text("{}")
    ensure_window_allowed(dt.datetime(2025, 1, 1, tzinfo=UTC),
                          TEST_START, frozen_config=frozen)
    # predictions() gates the same way (absent repo file would unlock
    # the real path, so pin the lock through a monkeypatched path)
    import core.backtest.runner as runner_mod

    original = runner_mod.ensure_window_allowed

    def strict(start, end, frozen_config=None):
        return original(start, end,
                        frozen_config=frozen_config or tmp_path / "no.json")

    monkeypatch_fn = strict
    runner_mod.ensure_window_allowed = monkeypatch_fn
    try:
        with pytest.raises(PeriodLockedError):
            predictions(db_url, ModelConfig(xi=0.005),
                        dt.datetime(2025, 1, 1, tzinfo=UTC),
                        dt.datetime(2025, 9, 1, tzinfo=UTC),
                        leagues=("premier_league",))
    finally:
        runner_mod.ensure_window_allowed = original


def test_phase4b_scripts_test_lock(tmp_path) -> None:
    """S2 & NIT: backtest_hybrid and backtest_mixture require canonical FROZEN_CONFIG_4b.json."""
    import subprocess
    import sys

    from core.backtest.periods import (
        FROZEN_CONFIG_4B_PATH,
        PeriodLockedError,
        ensure_period_allowed,
        ensure_period_allowed_4b,
    )

    absent = tmp_path / "nonexistent.json"
    with pytest.raises(PeriodLockedError):
        ensure_period_allowed("test", frozen_config=absent)

    with pytest.raises(PeriodLockedError):
        ensure_period_allowed_4b("test", frozen_config=absent)

    arbitrary = tmp_path / "arbitrary.json"
    arbitrary.write_text("{}")
    with pytest.raises(PeriodLockedError):
        ensure_period_allowed_4b("test", frozen_config=arbitrary)

    # Valid with existing 4b frozen config
    assert FROZEN_CONFIG_4B_PATH.exists()
    ensure_period_allowed("test", frozen_config=FROZEN_CONFIG_4B_PATH)
    ensure_period_allowed_4b("test", frozen_config=FROZEN_CONFIG_4B_PATH)

    # CLI test lock check on both scripts with absent and arbitrary files
    dummy_records = tmp_path / "records.json"
    dummy_records.write_text("[]")
    for bad_cfg in (absent, arbitrary):
        for mod in ("scripts.backtest_hybrid", "scripts.backtest_mixture"):
            res = subprocess.run(
                [sys.executable, "-m", mod,
                 "--records", str(dummy_records),
                 "--period", "test",
                 "--frozen-config", str(bad_cfg),
                 "--config", "{}",
                 "--out", str(tmp_path / "out.json")],
                capture_output=True,
                text=True,
            )
            assert res.returncode != 0
            assert "PeriodLockedError" in res.stderr


