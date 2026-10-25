"""Tests for FairMatrix construction and property guarantees (Step 1)."""

from __future__ import annotations

import datetime as dt

import numpy as np

from core.model.markets import (
    market_lambda_from_totals,
    proxy_score_matrix,
)
from core.value.fair_matrix import (
    MarketInputs,
    MarketOdds1X2,
    MarketTotalsLine,
    build_fair_matrix,
    devig_1x2,
    devig_totals,
    fit_1x2_share,
    is_half_line,
)


def _reference_hybrid_matrix(
    p_over: float,
    ph: float,
    pa: float,
    line: float = 2.5,
    rho: float = 0.0,
    kappa_high: float = 0.20,
) -> np.ndarray:
    """Exact pipeline from scripts/backtest_hybrid.py."""
    lam_total = market_lambda_from_totals(p_over, line)
    s_1x2 = fit_1x2_share(lam_total, ph, pa, rho=rho)
    m = proxy_score_matrix(lam_total, s_1x2, rho=rho)
    # draw_inflate with high_only=True
    if kappa_high != 0.0:
        idx = np.arange(min(m.shape))
        for i in idx:
            if i >= 2:
                m[i, i] *= 1.0 + kappa_high
        m /= m.sum()
    return m


def test_coincidence_with_backtest_hybrid_numerical_precision() -> None:
    """Test agreement with backtest_hybrid down to 1e-9 on several parameter sets."""
    test_cases = [
        # (over, under, home, draw, away)
        (1.95, 1.95, 2.10, 3.40, 3.60),
        (1.65, 2.30, 1.45, 4.50, 7.50),
        (2.40, 1.60, 3.80, 3.20, 2.10),
        (1.85, 2.05, 2.80, 3.10, 2.70),
    ]
    for over, under, h, d, a in test_cases:
        p_over, _ = devig_totals(over, under)
        ph, _, pa = devig_1x2(h, d, a)
        ref_m = _reference_hybrid_matrix(p_over, ph, pa, line=2.5, rho=0.0, kappa_high=0.20)

        inputs = MarketInputs(
            source="toa_snapshot",
            totals_by_book={"pinnacle": [MarketTotalsLine(line=2.5, over=over, under=under)]},
            h2h_by_book={"pinnacle": MarketOdds1X2(home=h, draw=d, away=a)},
        )
        fair = build_fair_matrix(inputs)
        assert fair.status == "ok"
        assert fair.matrix.shape == (11, 11)
        np.testing.assert_allclose(fair.matrix, ref_m, atol=1e-12, rtol=1e-12)


def test_fair_matrix_properties_and_query_methods() -> None:
    inputs = MarketInputs(
        source="fd_prematch",
        totals_by_book={"avg": [MarketTotalsLine(line=2.5, over=2.0, under=1.90)]},
        h2h_by_book={"avg": MarketOdds1X2(home=2.20, draw=3.30, away=3.50)},
    )
    fair = build_fair_matrix(inputs)
    assert fair.status == "ok"
    assert fair.matrix.shape == (11, 11)
    assert np.all(fair.matrix >= 0.0)
    assert abs(float(fair.matrix.sum()) - 1.0) < 1e-9

    # Query methods
    p_00 = fair.exact_score(0, 0)
    p_11 = fair.exact_score(1, 1)
    p_22 = fair.exact_score(2, 2)
    assert 0.0 < p_00 < 1.0
    assert 0.0 < p_11 < 1.0
    assert 0.0 < p_22 < 1.0

    # Multiscore set
    p_multi = fair.multiscore([(0, 0), (1, 1), (2, 2)])
    assert abs(p_multi - (p_00 + p_11 + p_22)) < 1e-12

    # Goals range
    p_range = fair.goals_range(0, 2)
    assert 0.0 < p_range < 1.0

    # Totals over / under
    p_o25 = fair.total_over(2.5)
    p_u25 = fair.total_under(2.5)
    assert abs((p_o25 + p_u25) - 1.0) < 1e-9

    # BTTS
    p_btts = fair.btts(yes=True)
    p_btts_no = fair.btts(yes=False)
    assert abs((p_btts + p_btts_no) - 1.0) < 1e-9


def test_bookmaker_selection_pinnacle_vs_consensus() -> None:
    # 1. Pinnacle available -> Pinnacle chosen
    inputs_pinn = MarketInputs(
        source="toa_snapshot",
        totals_by_book={
            "bet365": [MarketTotalsLine(line=2.5, over=1.90, under=1.90)],
            "pinnacle": [MarketTotalsLine(line=2.5, over=2.00, under=1.95)],
        },
        h2h_by_book={
            "bet365": MarketOdds1X2(home=2.10, draw=3.20, away=3.40),
            "pinnacle": MarketOdds1X2(home=2.15, draw=3.25, away=3.50),
        },
    )
    fair_pinn = build_fair_matrix(inputs_pinn)
    assert fair_pinn.source == "market_pinnacle"
    assert fair_pinn.books_used == ["pinnacle"]
    assert fair_pinn.status == "ok"

    # 2. Pinnacle missing -> Consensus of allowed books used
    inputs_cons = MarketInputs(
        source="toa_snapshot",
        totals_by_book={
            "bet365": [MarketTotalsLine(line=2.5, over=1.90, under=1.90)],
            "williamhill": [MarketTotalsLine(line=2.5, over=2.00, under=1.85)],
        },
        h2h_by_book={
            "bet365": MarketOdds1X2(home=2.10, draw=3.20, away=3.40),
            "williamhill": MarketOdds1X2(home=2.20, draw=3.10, away=3.50),
        },
    )
    fair_cons = build_fair_matrix(inputs_cons)
    assert fair_cons.source == "market_consensus"
    assert "bet365" in fair_cons.books_used
    assert "williamhill" in fair_cons.books_used


def test_excluded_books_ignored() -> None:
    # Onexbet and Betfair are excluded; if only they exist, market is unavailable
    inputs_excl = MarketInputs(
        source="toa_snapshot",
        totals_by_book={
            "onexbet": [MarketTotalsLine(line=2.5, over=1.95, under=1.95)],
            "betfair": [MarketTotalsLine(line=2.5, over=1.95, under=1.95)],
        },
        h2h_by_book={
            "onexbet": MarketOdds1X2(home=2.0, draw=3.0, away=4.0),
        },
    )
    fair = build_fair_matrix(inputs_excl)
    assert fair.status == "no_market"
    assert fair.source == "no_market"


def test_nearest_half_line_fallback() -> None:
    # 2.5 is missing, but 1.5 and 3.5 are present; nearest is 1.5 (|1.5 - 2.5| = 1.0)
    inputs_lines = MarketInputs(
        source="toa_snapshot",
        totals_by_book={
            "pinnacle": [
                MarketTotalsLine(line=1.5, over=1.35, under=3.40),
                MarketTotalsLine(line=3.5, over=3.10, under=1.38),
            ]
        },
        h2h_by_book={
            "pinnacle": MarketOdds1X2(home=1.80, draw=3.60, away=4.80),
        },
    )
    fair = build_fair_matrix(inputs_lines)
    assert fair.status == "ok"
    assert fair.line_used in (1.5, 3.5)
    assert is_half_line(fair.line_used)
    assert fair.lambda_total > 0.0


def test_no_market_behavior() -> None:
    # Missing 1X2 entirely
    inputs_no_h2h = MarketInputs(
        source="toa_snapshot",
        totals_by_book={"pinnacle": [MarketTotalsLine(line=2.5, over=1.95, under=1.95)]},
        h2h_by_book={},
    )
    fair_no_h2h = build_fair_matrix(inputs_no_h2h)
    assert fair_no_h2h.status == "no_market"
    assert fair_no_h2h.source == "no_market"
    assert fair_no_h2h.matrix.shape == (11, 11)
    assert float(fair_no_h2h.matrix.sum()) == 0.0

    # Missing totals entirely
    inputs_no_totals = MarketInputs(
        source="toa_snapshot",
        totals_by_book={},
        h2h_by_book={"pinnacle": MarketOdds1X2(home=2.0, draw=3.0, away=4.0)},
    )
    fair_no_totals = build_fair_matrix(inputs_no_totals)
    assert fair_no_totals.status == "no_market"


class DummyModel:
    def score_matrix(self, home_team: str, away_team: str) -> np.ndarray:
        # Simple symmetric Poisson-like matrix
        m = np.zeros((11, 11), dtype=np.float64)
        m[1, 0] = 0.3
        m[0, 1] = 0.2
        m[1, 1] = 0.5
        return m / m.sum()


def test_model_fallback_when_enabled() -> None:
    empty_inputs = MarketInputs(source="manual")

    # 1. Fallback disabled (default)
    fair_no = build_fair_matrix(empty_inputs, allow_model_fallback=False)
    assert fair_no.status == "no_market"

    # 2. Fallback enabled
    dummy = DummyModel()
    fair_fallback = build_fair_matrix(
        empty_inputs,
        allow_model_fallback=True,
        model=dummy,
        home_team="Team A",
        away_team="Team B",
    )
    assert fair_fallback.status == "fallback"
    assert fair_fallback.source == "model_fallback"
    assert fair_fallback.matrix.shape == (11, 11)
    assert abs(float(fair_fallback.matrix.sum()) - 1.0) < 1e-9
    assert fair_fallback.books_used == ["model"]


def test_input_hash_deterministic() -> None:
    inputs1 = MarketInputs(
        source="fd_prematch",
        snapshot_time=dt.datetime(2026, 10, 25, 12, 0),
        totals_by_book={"pinnacle": [MarketTotalsLine(line=2.5, over=1.95, under=1.95)]},
        h2h_by_book={"pinnacle": MarketOdds1X2(home=2.0, draw=3.2, away=3.8)},
    )
    inputs2 = MarketInputs(
        source="fd_prematch",
        snapshot_time=dt.datetime(2026, 10, 25, 12, 0),
        totals_by_book={"pinnacle": [MarketTotalsLine(line=2.5, over=1.95, under=1.95)]},
        h2h_by_book={"pinnacle": MarketOdds1X2(home=2.0, draw=3.2, away=3.8)},
    )
    assert inputs1.compute_hash() == inputs2.compute_hash()

    # Different odds produce different hash
    inputs3 = MarketInputs(
        source="fd_prematch",
        snapshot_time=dt.datetime(2026, 10, 25, 12, 0),
        totals_by_book={"pinnacle": [MarketTotalsLine(line=2.5, over=1.96, under=1.94)]},
        h2h_by_book={"pinnacle": MarketOdds1X2(home=2.0, draw=3.2, away=3.8)},
    )
    assert inputs1.compute_hash() != inputs3.compute_hash()
