"""Markets derived from a score-probability matrix (SPEC §1, phase 3).

All market functions are pure: matrix in, probability out. The matrix is
the model's (or the proxy's) score distribution — a square array where
entry [h, a] is P(home scores h, away scores a).

Totals accept half lines (x.5) only: whole lines need push semantics and
quarter lines half-stake splitting, both out of scope for v1
(docs/decisions.md, phase 0 review S11) — anything else raises
ValueError instead of being silently mis-priced.

``proxy_score_matrix`` and ``market_lambda_from_totals`` back the phase-5
prefilter (docs/decisions.md, final round S2): the market's de-vigged
over/under probabilities imply a total lambda, which becomes a
book-free proxy distribution to compare the model against.
"""

from __future__ import annotations

import math
from collections.abc import Iterable

import numpy as np
import numpy.typing as npt
from scipy.optimize import brentq
from scipy.stats import poisson

_PROXY_MAX_SCORE = 10  # same 11x11 shape as the model's matrices


def _check_matrix(m: npt.NDArray[np.float64]) -> None:
    if not isinstance(m, np.ndarray) or m.ndim != 2 \
            or m.shape[0] != m.shape[1] or m.shape[0] < 1:
        raise ValueError(f"score matrix must be a square ndarray, got {m!r}")
    if m.shape[0] < 2:
        raise ValueError("score matrix must cover at least scores 0 and 1")
    if float(m.min()) < -1e-12:
        raise ValueError(f"score matrix has negative cells (min {m.min():.3e})")
    if not np.isclose(float(m.sum()), 1.0, atol=1e-6):
        raise ValueError(
            f"score matrix must sum to 1, got {float(m.sum()):.6f}")


def _check_score(m: npt.NDArray[np.float64], h: int, a: int) -> None:
    n = m.shape[0]
    if not (0 <= h < n and 0 <= a < n):
        raise ValueError(f"score ({h}, {a}) outside the 0..{n - 1} matrix")


def _check_half_line(line: float) -> None:
    """Only x.5 lines: 2*line must be a positive odd integer (S11)."""
    doubled = line * 2.0
    if line <= 0.0 or not math.isfinite(line) \
            or abs(doubled - round(doubled)) > 1e-9 \
            or round(doubled) % 2 == 0:
        raise ValueError(
            f"totals line must be a half line (x.5), got {line}; "
            "whole and quarter lines are unsupported (decisions S11)"
        )


def exact_score(m: npt.NDArray[np.float64], h: int, a: int) -> float:
    """P(home h, away a)."""
    _check_matrix(m)
    _check_score(m, h, a)
    return float(m[h, a])


def multiscore_set(
    m: npt.NDArray[np.float64], scores: Iterable[tuple[int, int]]
) -> float:
    """P(score in the set); duplicates count once (it is a set of scores)."""
    _check_matrix(m)
    total = 0.0
    for h, a in set(scores):
        _check_score(m, h, a)
        total += float(m[h, a])
    return total


def goals_range(m: npt.NDArray[np.float64], lo: int, hi: int) -> float:
    """P(lo <= home goals + away goals <= hi)."""
    _check_matrix(m)
    if not (0 <= lo <= hi):
        raise ValueError(f"goals_range needs 0 <= lo <= hi, got {lo}..{hi}")
    n = m.shape[0]
    totals = np.add.outer(np.arange(n), np.arange(n))
    mask = (totals >= lo) & (totals <= hi)
    return float(m[mask].sum())


def total_over(m: npt.NDArray[np.float64], line: float) -> float:
    """P(total goals > line) — half lines only, so no push is possible."""
    _check_matrix(m)
    _check_half_line(line)
    n = m.shape[0]
    totals = np.add.outer(np.arange(n), np.arange(n))
    return float(m[totals > line].sum())


def total_under(m: npt.NDArray[np.float64], line: float) -> float:
    """P(total goals < line) — half lines only, so no push is possible."""
    _check_matrix(m)
    _check_half_line(line)
    n = m.shape[0]
    totals = np.add.outer(np.arange(n), np.arange(n))
    return float(m[totals < line].sum())


def btts(m: npt.NDArray[np.float64], yes: bool = True) -> float:
    """P(both teams to score)."""
    _check_matrix(m)
    p_yes = float(m[1:, 1:].sum())
    return p_yes if yes else 1.0 - p_yes


# --- phase-5 prefilter (docs/decisions.md, final round S2) -------------------


def proxy_score_matrix(
    lambda_total: float, home_share: float, max_score: int = _PROXY_MAX_SCORE,
    rho: float = 0.0,
) -> npt.NDArray[np.float64]:
    """Book-free proxy distribution: two Poissons + the DC tau.

    ``lambda_total`` is split into home/away rates by ``home_share``
    (the model's expected-goals share of the home side); ``rho`` applies
    the same Dixon-Coles low-score correction the model uses, so at
    equal rates and rho the proxy coincides with the model's matrix
    cell-for-cell. Same shape as the model's matrices, so every market
    function applies to it.
    """
    if lambda_total <= 0.0:
        raise ValueError(f"lambda_total must be > 0, got {lambda_total}")
    if not 0.0 < home_share < 1.0:
        raise ValueError(f"home_share must be in (0, 1), got {home_share}")
    lam_h = lambda_total * home_share
    lam_a = lambda_total * (1.0 - home_share)
    k = np.arange(max_score + 1, dtype=np.float64)
    fact = np.array([math.factorial(i) for i in range(max_score + 1)],
                    dtype=np.float64)
    ph = np.exp(-lam_h + k * math.log(lam_h)) / fact
    pa = np.exp(-lam_a + k * math.log(lam_a)) / fact
    m = np.outer(ph, pa)
    factors = {
        (0, 0): 1.0 - lam_h * lam_a * rho,
        (0, 1): 1.0 + lam_h * rho,
        (1, 0): 1.0 + lam_a * rho,
        (1, 1): 1.0 - rho,
    }
    for (h, a), f in factors.items():
        if f <= 0.0:
            raise ValueError(
                f"tau({h},{a}) = {f} <= 0 (lam={lam_h:.3f}, mu={lam_a:.3f}, "
                f"rho={rho:.3f}); parameters out of the admissible range"
            )
        m[h, a] *= f
    m /= m.sum()  # folded tail, same as the model's matrix
    return m


def market_lambda_from_totals(p_over: float, line: float) -> float:
    """Total lambda implied by a margin-free P(over line) (inverse problem).

    Under the proxy model the total goals are Poisson(lambda_total), so
    this solves ``P(X > line; lambda) = p_over`` for lambda numerically.
    ``p_over`` must be the DE-VIGGED over probability, strictly inside
    (0, 1); the line must be a half line (x.5).
    """
    _check_half_line(line)
    if not 0.0 < p_over < 1.0:
        raise ValueError(
            f"p_over must be a de-vigged probability in (0, 1), got {p_over}"
        )
    k = math.floor(line)  # P(X > line) = P(X >= k + 1) = sf(k)

    def f(lam: float) -> float:
        return float(poisson.sf(k, lam)) - p_over

    lo, hi = 1e-9, 10.0
    if f(lo) > 0.0:  # p_over below the numerically reachable range
        raise ValueError(f"p_over={p_over} is too small to invert")
    while f(hi) < 0.0:
        hi *= 2.0
        if hi > 1e6:
            raise ValueError(f"p_over={p_over} is too close to 1 to invert")
    return float(brentq(f, lo, hi, xtol=1e-12, rtol=1e-14))
