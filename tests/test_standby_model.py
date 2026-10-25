"""Tests for Standby Dixon-Coles Model and Lineup Risk Checks (Phase 5)."""

from __future__ import annotations

import datetime as dt

from core.db import session_scope
from core.value.standby_model import (
    get_key_players_for_team,
    get_model_home_share_and_model,
)

DB_URL = "sqlite:////tmp/p5.db"


def test_get_model_home_share_and_model_on_real_db() -> None:
    # Train dynamically before 2024-10-20
    as_of = dt.datetime(2024, 10, 20, 12, 0, tzinfo=dt.UTC)
    share, model = get_model_home_share_and_model(
        db_url=DB_URL,
        league="premier_league",
        as_of=as_of,
        home_team="Arsenal",
        away_team="Liverpool",
    )
    assert share is not None
    assert 0.40 < share < 0.70
    assert model is not None
    # Model can generate reference score matrix
    m = model.score_matrix("Arsenal", "Liverpool")
    assert m.shape == (11, 11)
    assert abs(float(m.sum()) - 1.0) < 1e-4


def test_get_model_home_share_invalid_team_graceful_fallback() -> None:
    as_of = dt.datetime(2024, 10, 20, 12, 0, tzinfo=dt.UTC)
    share, model = get_model_home_share_and_model(
        db_url=DB_URL,
        league="premier_league",
        as_of=as_of,
        home_team="NonExistentFC",
        away_team="Arsenal",
    )
    assert share is None
    assert model is None


def test_get_key_players_for_team_on_real_db() -> None:
    with session_scope(DB_URL) as session:
        # Arsenal (team_id=1) has lineups in autumn 2026
        kp = get_key_players_for_team(
            session=session,
            team_id=1,
            before_date=dt.date(2026, 10, 25),
            last_n=8,
            min_starts_share=0.70,
        )
        assert len(kp) > 0
        assert "Linton Maina" in kp or "Marvin Schwäbe" in kp

        # Non-existent team or date far in past has 0 key players
        kp_empty = get_key_players_for_team(
            session=session,
            team_id=99999,
            before_date=dt.date(2026, 10, 25),
        )
        assert len(kp_empty) == 0
