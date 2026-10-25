"""Tests for Pinnacle event-level correct_score benchmark matrix & comparison metrics (Step 2)."""

from __future__ import annotations

import numpy as np
import pytest

from core.value.fair_matrix import (
    MarketInputs,
    MarketOdds1X2,
    MarketTotalsLine,
    build_fair_matrix,
)
from core.value.pinnacle_benchmark import (
    build_pinnacle_benchmark_matrix,
    compare_matrices,
    load_pinnacle_correct_score_from_db,
    parse_score_outcome,
)

DB_URL = "sqlite:////tmp/p5.db"


def test_parse_score_outcome() -> None:
    # Team1:G1|Team2:G2
    h, a = "Aston Villa", "Manchester City"
    assert parse_score_outcome("Aston Villa:2|Manchester City:1", h, a) == (2, 1)
    assert parse_score_outcome("Manchester City:3|Aston Villa:1", h, a) == (1, 3)
    # H{g1}|A{g2}
    assert parse_score_outcome("H2|A0") == (2, 0)
    assert parse_score_outcome("h0|a1") == (0, 1)
    # simple 2:1 or 2-1
    assert parse_score_outcome("2:1") == (2, 1)
    assert parse_score_outcome("0-0") == (0, 0)
    # other
    assert parse_score_outcome("Other") == "other"
    assert parse_score_outcome("Any Other Score") == "other"
    assert parse_score_outcome("Inny wynik") == "other"

    with pytest.raises(ValueError):
        parse_score_outcome("invalid text")


def test_build_pinnacle_benchmark_matrix_properties() -> None:
    prices = {
        (0, 0): 10.0,
        (1, 0): 8.0,
        (0, 1): 9.0,
        (1, 1): 7.0,
        (2, 0): 15.0,
        (2, 1): 12.0,
        (1, 2): 14.0,
        (0, 2): 18.0,
        (2, 2): 16.0,
    }
    m = build_pinnacle_benchmark_matrix(prices, other_price=None, max_score=10)
    assert m.shape == (11, 11)
    assert np.all(m >= 0.0)
    assert abs(float(m.sum()) - 1.0) < 1e-9

    # Relative ratio between 1:0 and 0:1 should match inverse prices: (1/8) / (1/9) = 9/8 = 1.125
    ratio = m[1, 0] / m[0, 1]
    assert np.isclose(ratio, 9.0 / 8.0, atol=1e-6)


def test_build_pinnacle_benchmark_with_other_tail_distribution() -> None:
    prices = {
        (0, 0): 10.0,
        (1, 0): 8.0,
        (0, 1): 9.0,
        (1, 1): 7.0,
    }
    # Reference matrix with some tail mass
    ref_m = np.zeros((11, 11), dtype=np.float64)
    ref_m[0, 0] = 0.1
    ref_m[1, 0] = 0.2
    ref_m[0, 1] = 0.2
    ref_m[1, 1] = 0.2
    ref_m[2, 2] = 0.15
    ref_m[3, 3] = 0.15
    ref_m /= ref_m.sum()

    m = build_pinnacle_benchmark_matrix(
        prices, other_price=5.0, reference_matrix=ref_m, max_score=10,
    )
    assert m.shape == (11, 11)
    assert abs(float(m.sum()) - 1.0) < 1e-9
    # Unquoted scores (2, 2) and (3, 3) must receive positive tail probability
    assert m[2, 2] > 0.0
    assert m[3, 3] > 0.0
    assert np.isclose(m[2, 2], m[3, 3], atol=1e-6)


def test_compare_matrices_metrics() -> None:
    # 1. Identical matrices
    m = np.zeros((11, 11), dtype=np.float64)
    m[1, 0] = 0.4
    m[0, 1] = 0.3
    m[1, 1] = 0.3
    res_ident = compare_matrices(m, m)
    assert res_ident.max_cell_diff == 0.0
    assert res_ident.mean_cell_diff == 0.0
    assert res_ident.tvd == 0.0
    assert res_ident.rmse == 0.0
    assert res_ident.jsd == 0.0

    # 2. Differing matrices
    m2 = m.copy()
    m2[1, 0] = 0.35
    m2[0, 1] = 0.35
    res_diff = compare_matrices(m, m2)
    assert res_diff.max_cell_diff == pytest.approx(0.05)
    assert res_diff.tvd == pytest.approx(0.05)
    assert res_diff.rmse > 0.0
    assert res_diff.jsd > 0.0
    assert res_diff.home_win_diff == pytest.approx(0.05)
    assert res_diff.away_win_diff == pytest.approx(-0.05)
    assert res_diff.draw_diff == pytest.approx(0.0)


def test_pinnacle_benchmark_on_real_db_snapshot_match_1973() -> None:
    """Evaluate benchmark on actual DB snapshot rows for match 1973."""
    prices, other_price = load_pinnacle_correct_score_from_db(
        DB_URL,
        match_id=1973,
        home_team="Aston Villa",
        away_team="Manchester City",
    )
    assert len(prices) == 32  # Exactly 32 quoted scores for Pinnacle on match 1973
    # Pinnacle event-level market quotes standard 32 scores without 'other'
    assert other_price is None

    # Build Pinnacle benchmark matrix
    m_pinn = build_pinnacle_benchmark_matrix(prices, other_price=other_price, max_score=10)
    assert m_pinn.shape == (11, 11)
    assert abs(float(m_pinn.sum()) - 1.0) < 1e-9

    # Most probable score for Man City away at Aston Villa was 1:2 or 0:1
    top_score = divmod(int(np.argmax(m_pinn)), 11)
    assert top_score in [(1, 2), (0, 1), (1, 1), (0, 2)]

    # Now construct a synthetic / consensus fair matrix to run compare_matrices
    # Match 1973 has William Hill totals in DB: 2.5 @ 1.50 / 2.45
    # Since h2h was not yet polled in TOA snapshots (Phase 7 task), we use consensus 1X2
    fair_inputs = MarketInputs(
        source="toa_snapshot",
        totals_by_book={"williamhill": [MarketTotalsLine(line=2.5, over=1.50, under=2.45)]},
        h2h_by_book={"consensus": MarketOdds1X2(home=4.50, draw=4.00, away=1.75)},
        match_name="Aston Villa - Manchester City",
    )
    fair_mat = build_fair_matrix(fair_inputs)
    assert fair_mat.status == "ok"

    # Compare fair market matrix with Pinnacle benchmark
    comp = compare_matrices(fair_mat.matrix, m_pinn)
    assert comp.max_cell_diff < 0.15
    assert comp.tvd < 0.35
    assert comp.rmse < 0.05
    assert comp.jsd < 0.05
    # High draws diff should be well-controlled due to kappa_high = 0.20
    assert abs(comp.high_draws_diff) < 0.05
