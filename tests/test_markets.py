"""Markets over a score matrix: properties, validation, prefilter pair.

The totals rules implement docs/decisions.md (phase 0 review S11): half
lines only; whole/quarter lines raise instead of being mis-priced.
proxy_score_matrix + market_lambda_from_totals form the phase-5 prefilter
loop and must be circularly consistent.
"""

from __future__ import annotations

import datetime as dt
import math

import numpy as np
import pytest
from scipy.stats import poisson

from core.model.dixon_coles import FittedModel
from core.model.markets import (
    btts,
    exact_score,
    goals_range,
    market_lambda_from_totals,
    multiscore_set,
    proxy_score_matrix,
    total_over,
    total_under,
)

AS_OF = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)


@pytest.fixture(scope="module")
def matrix() -> np.ndarray:
    """A realistic score matrix from a fitted-style model."""
    model = FittedModel(
        league="synthetic", as_of=AS_OF, xi=0.0, w=0.5, n_matches=0,
        attacks={"Home": 0.3, "Away": -0.1},
        defences={"Home": -0.2, "Away": 0.1},
        home_advantage=0.25, rho=-0.05, intercept=0.25,
        converged=True, config_hash="synthetic",
    )
    m = model.score_matrix("Home", "Away")
    assert np.isclose(m.sum(), 1.0, atol=1e-9)
    return m


# --- basic markets ----------------------------------------------------------


def test_exact_score_returns_the_cell(matrix: np.ndarray) -> None:
    assert exact_score(matrix, 1, 1) == matrix[1, 1]
    assert exact_score(matrix, 0, 0) == matrix[0, 0]
    assert exact_score(matrix, 10, 10) == matrix[10, 10]


@pytest.mark.parametrize("h,a", [(-1, 0), (0, -1), (11, 0), (0, 11)])
def test_exact_score_rejects_out_of_range(matrix: np.ndarray, h: int, a: int) -> None:
    with pytest.raises(ValueError, match="outside"):
        exact_score(matrix, h, a)


def test_multiscore_set_sums_cells_and_dedupes(matrix: np.ndarray) -> None:
    expected = matrix[1, 0] + matrix[2, 0] + matrix[2, 1]
    assert multiscore_set(matrix, {(1, 0), (2, 0), (2, 1)}) == pytest.approx(expected)
    # duplicates are the same score listed twice, not a double bet
    assert multiscore_set(matrix, [(1, 0), (1, 0)]) == pytest.approx(matrix[1, 0])


def test_multiscore_set_rejects_out_of_range(matrix: np.ndarray) -> None:
    with pytest.raises(ValueError, match="outside"):
        multiscore_set(matrix, {(1, 0), (5, 11)})


def test_goals_range_matches_manual_mask(matrix: np.ndarray) -> None:
    totals = np.add.outer(np.arange(11), np.arange(11))
    assert goals_range(matrix, 2, 4) == pytest.approx(
        matrix[(totals >= 2) & (totals <= 4)].sum())
    # hi beyond the matrix's own totals (0..20) covers every cell
    assert goals_range(matrix, 0, 20) == pytest.approx(1.0)
    assert goals_range(matrix, 3, 3) == pytest.approx(
        matrix[totals == 3].sum())


@pytest.mark.parametrize("lo,hi", [(-1, 2), (3, 2), (2, -1)])
def test_goals_range_rejects_bad_bounds(matrix: np.ndarray, lo: int, hi: int) -> None:
    with pytest.raises(ValueError, match="lo <= hi"):
        goals_range(matrix, lo, hi)


HALF_LINES = [0.5, 1.5, 2.5, 3.5, 4.5]


@pytest.mark.parametrize("line", HALF_LINES)
def test_over_plus_under_is_one_on_half_lines(matrix: np.ndarray, line: float) -> None:
    over = total_over(matrix, line)
    under = total_under(matrix, line)
    assert 0.0 < over < 1.0
    assert over + under == pytest.approx(1.0, abs=1e-12)
    # over(2.5) is exactly P(total >= 3) — no push on half lines
    totals = np.add.outer(np.arange(11), np.arange(11))
    assert over == pytest.approx(matrix[totals >= int(line) + 1].sum())


@pytest.mark.parametrize(
    "line", [2.0, 3.0, 2.25, 2.75, 3.75, 0.0, -0.5, -2.5, float("inf"), float("nan")])
def test_totals_reject_non_half_lines(matrix: np.ndarray, line: float) -> None:
    for fn in (total_over, total_under):
        with pytest.raises(ValueError, match="half line"):
            fn(matrix, line)


def test_btts_yes_and_no_partition(matrix: np.ndarray) -> None:
    yes = btts(matrix, yes=True)
    no = btts(matrix, yes=False)
    assert yes == pytest.approx(matrix[1:, 1:].sum())
    assert yes + no == pytest.approx(1.0, abs=1e-12)


def test_functions_reject_non_square_matrices() -> None:
    bad = np.ones((3, 4))
    for fn in (lambda m: exact_score(m, 0, 0),
               lambda m: btts(m),
               lambda m: total_over(m, 2.5),
               lambda m: goals_range(m, 0, 2)):
        with pytest.raises(ValueError, match="square"):
            fn(bad)


# --- prefilter pair (decisions, final round S2) -----------------------------


def test_proxy_matrix_is_normalized_and_poisson_marginal() -> None:
    lam_total, share = 2.8, 0.58
    m = proxy_score_matrix(lam_total, share)
    assert m.shape == (11, 11)
    assert np.isclose(m.sum(), 1.0, atol=1e-9)
    # home/away marginals are Poisson at the split rates (up to the folded tail)
    k = np.arange(11)
    assert np.allclose(m.sum(axis=1), poisson.pmf(k, lam_total * share),
                       atol=1e-9)
    assert np.allclose(m.sum(axis=0), poisson.pmf(k, lam_total * (1 - share)),
                       atol=1e-9)


@pytest.mark.parametrize("lam,share", [(0.0, 0.5), (-1.0, 0.5),
                                       (2.5, 0.0), (2.5, 1.0), (2.5, 1.5)])
def test_proxy_matrix_rejects_bad_arguments(lam: float, share: float) -> None:
    with pytest.raises(ValueError):
        proxy_score_matrix(lam, share)


def test_proxy_with_rho_matches_model_btts_and_cells() -> None:
    """Review S5: at equal rates and rho the proxy matrix coincides with
    the fitted model's matrix (tau included), so BTTS (and any market)
    agrees between prefilter proxy and model."""
    lam, mu, rho = 1.9, 1.2, -0.06
    model = FittedModel(
        league="s", as_of=AS_OF, xi=0.0, w=0.5, n_matches=0,
        attacks={"H": 0.0, "A": 0.0}, defences={"H": 0.0, "A": 0.0},
        home_advantage=math.log(lam / mu), rho=rho,
        intercept=0.5 * (math.log(lam) + math.log(mu)) - 0.5 * math.log(lam / mu),
        converged=True, config_hash="s",
    )
    assert model._rates("H", "A") == pytest.approx((lam, mu), rel=1e-12)
    m_model = model.score_matrix("H", "A")
    m_proxy = proxy_score_matrix(lam + mu, lam / (lam + mu), rho=rho)
    assert btts(m_model, yes=True) == pytest.approx(btts(m_proxy, yes=True),
                                                    abs=1e-12)
    assert btts(m_model, yes=False) == pytest.approx(btts(m_proxy, yes=False),
                                                     abs=1e-12)
    # rho actually moves the proxy's low-score cells (guard against a
    # silently ignored argument)
    m_rho0 = proxy_score_matrix(lam + mu, lam / (lam + mu), rho=0.0)
    assert m_proxy[1, 1] != pytest.approx(m_rho0[1, 1])


def test_proxy_rejects_rho_out_of_admissible_range() -> None:
    with pytest.raises(ValueError, match="admissible"):
        # lam=mu=2.85 -> lam*mu ~ 8.1 -> tau(0,0) = 1 - 8.1*0.15 < 0
        proxy_score_matrix(5.7, 0.5, rho=0.15)


@pytest.mark.parametrize("line", HALF_LINES)
@pytest.mark.parametrize("lam", [1.2, 2.8, 3.6])
def test_market_lambda_inverts_poisson_over_prob(line: float, lam: float) -> None:
    p_over = float(poisson.sf(int(line), lam))
    recovered = market_lambda_from_totals(p_over, line)
    assert recovered == pytest.approx(lam, rel=1e-6)


def test_proxy_and_lambda_inversion_are_circular() -> None:
    """proxy -> total_over -> market_lambda must return the input lambda.

    Tolerance 1e-4: the proxy matrix is truncated at 10 and renormalised,
    so its over-probability deviates from the exact Poisson tail by the
    folded-tail mass (~1e-5 at lambda ~ 3.4)."""
    for lam in (1.5, 2.6, 3.4):
        m = proxy_score_matrix(lam, home_share=0.55)
        for line in (1.5, 2.5, 3.5):
            p_over = total_over(m, line)
            assert market_lambda_from_totals(p_over, line) == pytest.approx(
                lam, rel=1e-4)


@pytest.mark.parametrize("p", [0.0, 1.0, -0.1, 1.1, float("nan")])
def test_market_lambda_rejects_bad_probability(p: float) -> None:
    with pytest.raises(ValueError, match="p_over"):
        market_lambda_from_totals(p, 2.5)


@pytest.mark.parametrize("line", [2.0, 2.25])
def test_market_lambda_rejects_non_half_lines(line: float) -> None:
    with pytest.raises(ValueError, match="half line"):
        market_lambda_from_totals(0.55, line)


def test_market_lambda_inverts_extreme_probabilities() -> None:
    # every p strictly inside (0, 1) is invertible: extremes map to the
    # correspondingly extreme lambdas (monotone sf)
    tiny = market_lambda_from_totals(1e-12, 2.5)
    assert 0.0 < tiny < 0.01
    huge = market_lambda_from_totals(1.0 - 1e-12, 2.5)
    assert huge > 20.0
