"""Tests for user bet tracking, outcome settlement, pre-kickoff CLV, and portfolio metrics."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from decimal import Decimal

import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from core.orm import Base, ClosingOdds, Match, Team, UserBet
from core.tracking.clv import (
    calculate_user_bet_clv,
    evaluate_fair_probs_for_bet,
)
from core.tracking.settlement import (
    evaluate_bet_outcome,
    settle_user_bet,
)
from core.tracking.stats import (
    bootstrap_ci_95,
    compute_performance_metrics,
)
from core.value.fair_matrix import FairMatrix


@pytest.fixture
def db_session() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sm = sessionmaker(bind=engine)
    session = sm()
    yield session
    session.close()


# --- Outcome Settlement Tests ------------------------------------------------


def test_evaluate_exact_score() -> None:
    # Exact match
    assert evaluate_bet_outcome("exact_score", "2-1", None, 2, 1) == "won"
    assert evaluate_bet_outcome("exact_score", "2:1", None, 2, 1) == "won"
    assert evaluate_bet_outcome("exact_score", "2-1", None, 1, 1) == "lost"

    # Inny overflow (>4 goals per team)
    assert evaluate_bet_outcome("exact_score", "inny", None, 5, 0, cs_grid_max_goals=4) == "won"
    assert evaluate_bet_outcome("exact_score", "inny", None, 1, 5, cs_grid_max_goals=4) == "won"
    assert evaluate_bet_outcome("exact_score", "inny", None, 4, 4, cs_grid_max_goals=4) == "lost"


def test_evaluate_multiscore() -> None:
    sel = "1-0, 2-0, 2-1"
    assert evaluate_bet_outcome("multiscore", sel, None, 2, 0) == "won"
    assert evaluate_bet_outcome("multiscore", sel, None, 2, 1) == "won"
    assert evaluate_bet_outcome("multiscore", sel, None, 1, 1) == "lost"


def test_evaluate_goals_range() -> None:
    assert evaluate_bet_outcome("goals_range", "2-3", None, 1, 1) == "won"  # 2 goals
    assert evaluate_bet_outcome("goals_range", "2-3", None, 2, 1) == "won"  # 3 goals
    assert evaluate_bet_outcome("goals_range", "2-3", None, 0, 1) == "lost"  # 1 goal
    assert evaluate_bet_outcome("goals_range", "4-6", None, 3, 2) == "won"  # 5 goals
    assert evaluate_bet_outcome("goals_range", "7+", None, 4, 3) == "won"  # 7 goals


def test_evaluate_totals_half_and_integer_push() -> None:
    # Half line
    assert evaluate_bet_outcome("totals", "over 2.5", Decimal("2.5"), 2, 1) == "won"
    assert evaluate_bet_outcome("totals", "over 2.5", Decimal("2.5"), 1, 1) == "lost"
    assert evaluate_bet_outcome("totals", "under 2.5", Decimal("2.5"), 1, 1) == "won"

    # Integer line with push
    assert evaluate_bet_outcome("totals", "over 3.0", Decimal("3.0"), 2, 1) == "push"
    assert evaluate_bet_outcome("totals", "under 3.0", Decimal("3.0"), 2, 1) == "push"
    assert evaluate_bet_outcome("totals", "over 3.0", Decimal("3.0"), 3, 1) == "won"
    assert evaluate_bet_outcome("totals", "under 3.0", Decimal("3.0"), 1, 1) == "won"


def test_evaluate_btts() -> None:
    assert evaluate_bet_outcome("btts", "yes", None, 1, 1) == "won"
    assert evaluate_bet_outcome("btts", "yes", None, 2, 0) == "lost"
    assert evaluate_bet_outcome("btts", "no", None, 2, 0) == "won"
    assert evaluate_bet_outcome("btts", "no", None, 1, 1) == "lost"


def test_evaluate_h2h() -> None:
    assert evaluate_bet_outcome("h2h", "1", None, 2, 1) == "won"
    assert evaluate_bet_outcome("h2h", "1", None, 1, 1) == "lost"
    assert evaluate_bet_outcome("h2h", "X", None, 1, 1) == "won"
    assert evaluate_bet_outcome("h2h", "2", None, 0, 1) == "won"


def test_settle_user_bet_pnl_calculation() -> None:
    now = dt.datetime.now(dt.UTC)
    bet_won = UserBet(
        match_id=1,
        market="exact_score",
        selection_key="2-1",
        odds=Decimal("8.500"),
        stake_units=Decimal("1.00"),
        placed_at_utc=now,
        created_at_utc=now,
    )
    settle_user_bet(bet_won, home_goals=2, away_goals=1)
    assert bet_won.status == "won"
    assert bet_won.payout_units == Decimal("8.500")
    assert bet_won.pnl_units == Decimal("7.500")

    bet_lost = UserBet(
        match_id=1,
        market="exact_score",
        selection_key="2-1",
        odds=Decimal("8.500"),
        stake_units=Decimal("2.00"),
        placed_at_utc=now,
        created_at_utc=now,
    )
    settle_user_bet(bet_lost, home_goals=0, away_goals=0)
    assert bet_lost.status == "lost"
    assert bet_lost.payout_units == Decimal("0.00")
    assert bet_lost.pnl_units == Decimal("-2.00")

    bet_push = UserBet(
        match_id=1,
        market="totals",
        selection_key="over 3.0",
        line=Decimal("3.0"),
        odds=Decimal("2.100"),
        stake_units=Decimal("1.50"),
        placed_at_utc=now,
        created_at_utc=now,
    )
    settle_user_bet(bet_push, home_goals=2, away_goals=1)  # exactly 3 goals
    assert bet_push.status == "push"
    assert bet_push.payout_units == Decimal("1.50")
    assert bet_push.pnl_units == Decimal("0.00")


# --- CLV & Fair Probability Tests -------------------------------------------


def test_evaluate_fair_probs_for_bet() -> None:
    # Synthetic 11x11 score distribution with known cell probabilities
    m = np.zeros((11, 11), dtype=np.float64)
    m[1, 0] = 0.20
    m[2, 0] = 0.15
    m[1, 1] = 0.15
    m[0, 0] = 0.10
    m[2, 1] = 0.10
    m[3, 0] = 0.10
    m[0, 1] = 0.20

    matrix = FairMatrix(
        matrix=m,
        lambda_home=1.5,
        lambda_away=0.8,
        lambda_total=2.3,
        home_share=0.65,
        line_used=2.5,
        source="test",
        status="ok",
        books_used=["pinnacle"],
        snapshot_time=None,
        input_hash="hash",
        p_over_devig=0.45,
        p_home_devig=0.55,
        p_draw_devig=0.25,
        p_away_devig=0.20,
        rho=0.0,
        kappa_high=0.2,
    )

    p_win, p_push = evaluate_fair_probs_for_bet(matrix, "exact_score", "1-0", None)
    assert np.isclose(p_win, 0.20)
    assert p_push == 0.0

    p_win, _ = evaluate_fair_probs_for_bet(matrix, "multiscore", "1-0, 2-0", None)
    assert np.isclose(p_win, 0.35)

    p_win, _ = evaluate_fair_probs_for_bet(matrix, "btts", "yes", None)
    # Cells with h>0 and a>0: (1,1)=0.15, (2,1)=0.10 -> 0.25
    assert np.isclose(p_win, 0.25)


def test_calculate_user_bet_clv_from_closing_odds(db_session: Session) -> None:
    now = dt.datetime.now(dt.UTC)
    t1 = Team(name="Home FC")
    t2 = Team(name="Away FC")
    db_session.add_all([t1, t2])
    db_session.flush()

    match = Match(
        league="epl",
        season="2026-2027",
        kickoff_source="toa",
        home_team_id=t1.id,
        away_team_id=t2.id,
        date_utc=now.date(),
        commence_time_utc=now + dt.timedelta(hours=2),
        status="scheduled",
    )
    db_session.add(match)
    db_session.flush()

    # Add closing odds (football-data closing)
    c_odds = ClosingOdds(
        match_id=match.id,
        bookmaker="PC",
        over_price=Decimal("1.952"),
        under_price=Decimal("1.952"),
        home_price=Decimal("2.100"),
        draw_price=Decimal("3.400"),
        away_price=Decimal("3.800"),
    )
    db_session.add(c_odds)

    bet = UserBet(
        match_id=match.id,
        market="totals",
        selection_key="over 2.5",
        line=Decimal("2.5"),
        odds=Decimal("2.050"),
        stake_units=Decimal("1.00"),
        placed_at_utc=now - dt.timedelta(hours=5),
        created_at_utc=now - dt.timedelta(hours=5),
    )
    db_session.add(bet)
    db_session.commit()

    clv_val, source = calculate_user_bet_clv(db_session, bet)
    assert clv_val is not None
    assert source == "fair_market_matrix"
    # Over 2.5 fair prob is ~0.50, odds placed = 2.05 -> CLV ~ +0.025
    assert clv_val > 0


# --- Portfolio Performance Statistics Tests ----------------------------------


def test_compute_performance_metrics() -> None:
    now = dt.datetime.now(dt.UTC)
    bets = [
        UserBet(
            match_id=1,
            market="totals",
            selection_key="over 2.5",
            odds=Decimal("2.000"),
            stake_units=Decimal("1.00"),
            status="won",
            payout_units=Decimal("2.00"),
            pnl_units=Decimal("1.00"),
            clv_fair=Decimal("0.0500"),
            p_fair_at_placement=Decimal("0.5250"),
            placed_at_utc=now,
            created_at_utc=now,
        ),
        UserBet(
            match_id=2,
            market="h2h",
            selection_key="1",
            odds=Decimal("2.100"),
            stake_units=Decimal("1.00"),
            status="lost",
            payout_units=Decimal("0.00"),
            pnl_units=Decimal("-1.00"),
            clv_fair=Decimal("-0.0200"),
            p_fair_at_placement=Decimal("0.4600"),
            placed_at_utc=now,
            created_at_utc=now,
        ),
        UserBet(
            match_id=3,
            market="totals",
            selection_key="over 3.0",
            odds=Decimal("1.900"),
            stake_units=Decimal("1.00"),
            status="push",
            payout_units=Decimal("1.00"),
            pnl_units=Decimal("0.00"),
            clv_fair=Decimal("0.0100"),
            p_fair_at_placement=Decimal("0.5000"),
            placed_at_utc=now,
            created_at_utc=now,
        ),
    ]

    report = compute_performance_metrics(bets)
    assert report.total_bets == 3
    assert report.settled_bets == 3
    assert report.won_bets == 1
    assert report.lost_bets == 1
    assert report.push_bets == 1
    assert report.total_stake == Decimal("3.00")
    assert report.total_pnl == Decimal("0.00")
    assert report.roi == 0.0
    assert report.hit_rate == 0.50  # 1 won out of 2 decided (won+lost)

    assert report.avg_clv is not None
    assert np.isclose(report.avg_clv, (0.05 - 0.02 + 0.01) / 3, atol=1e-3)
    assert report.positive_clv_share == round(2 / 3, 4)

    # Small sample warning must be present since N < 100
    assert report.sample_size_warning is not None
    assert "статистически недостаточна" in report.sample_size_warning


def test_bootstrap_ci_95() -> None:
    vals = [0.1, -0.05, 0.08, -0.02, 0.04, 0.12]
    ci = bootstrap_ci_95(vals, n_resamples=500, seed=42)
    assert ci is not None
    low, high = ci
    assert low < high
    mean_val = float(np.mean(vals))
    assert low <= mean_val <= high
