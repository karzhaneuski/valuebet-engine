"""Pinnacle event-level correct_score benchmark matrix & comparison metrics.

Constructs the 11x11 exact score probability benchmark directly from Pinnacle's
event-level correct_score market in odds_snapshots (SPEC §4, decisions B6.3):
- Proportional devigging across all quoted exact score outcomes;
- Distribution of the 'Any other' / tail mass proportionally to the fair matrix tail;
- Metric suite for comparing the fair market matrix against the Pinnacle benchmark.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from dataclasses import dataclass
from typing import Literal

import numpy as np
import numpy.typing as npt
from sqlalchemy import select

from core.db import session_scope
from core.model.markets import (
    _check_matrix,
)
from core.model.markets import btts as calc_btts
from core.model.markets import goals_range as calc_goals_range
from core.model.markets import multiscore_set as calc_multiscore_set
from core.model.markets import total_over as calc_total_over
from core.orm import Match, OddsSnapshot

ParsedOutcome = tuple[int, int] | Literal["other"]


def parse_score_outcome(
    outcome: str,
    home_team: str | None = None,
    away_team: str | None = None,
) -> ParsedOutcome:
    """Parse TOA / snapshot correct_score outcome string.

    Supports:
    - 'Aston Villa:1|Manchester City:0'
    - 'TeamA:2|TeamB:1'
    - '1:0', '2-1', 'H2|A1'
    - 'Other', 'Any Other', 'Any Other Score', 'inny wynik'
    """
    cleaned = outcome.strip()
    lowered = cleaned.lower()
    other_exact = {
        "other",
        "any other",
        "any other score",
        "inny",
        "inny wynik",
        "pozostałe",
        "pozostale",
        "rest",
    }
    if lowered in other_exact:
        return "other"

    # Pattern 1: Team1:G1|Team2:G2
    if "|" in cleaned and ":" in cleaned:
        parts = cleaned.split("|")
        if len(parts) == 2:
            try:
                t1, g1_str = parts[0].rsplit(":", 1)
                t2, g2_str = parts[1].rsplit(":", 1)
                g1, g2 = int(g1_str.strip()), int(g2_str.strip())
                t1_clean, t2_clean = t1.strip().lower(), t2.strip().lower()
                if home_team and away_team:
                    h_clean = home_team.strip().lower()
                    a_clean = away_team.strip().lower()
                    if t1_clean == h_clean or h_clean in t1_clean:
                        return g1, g2
                    if t2_clean == h_clean or h_clean in t2_clean:
                        return g2, g1
                    if t1_clean == a_clean or a_clean in t1_clean:
                        return g2, g1
                # Default: first is home, second is away
                return g1, g2
            except (ValueError, IndexError):
                pass

    # Pattern 2: H{g1}|A{g2}
    m_ha = re.match(r"^H(\d+)\|A(\d+)$", cleaned, re.IGNORECASE)
    if m_ha:
        return int(m_ha.group(1)), int(m_ha.group(2))

    # Pattern 3: {g1}:{g2} or {g1}-{g2}
    m_simple = re.match(r"^(\d+)\s*[:\-]\s*(\d+)$", cleaned)
    if m_simple:
        return int(m_simple.group(1)), int(m_simple.group(2))

    raise ValueError(f"Unrecognized correct_score outcome format: {outcome!r}")


def build_pinnacle_benchmark_matrix(
    prices: dict[tuple[int, int], float],
    other_price: float | None = None,
    reference_matrix: npt.NDArray[np.float64] | None = None,
    max_score: int = 10,
) -> npt.NDArray[np.float64]:
    """Build 11x11 exact score distribution from devigged Pinnacle correct_score market.

    Rule for 'Any other' score:
    - Quoted outcomes are inverted to implied probabilities via proportional devigging.
    - If other_price is provided, its devigged probability mass p_other represents all
      unquoted score combinations in the 11x11 grid (tail scores).
    - p_other is distributed across all unquoted cells proportionally to reference_matrix
      (if provided and non-zero on the tail), or uniformly across unquoted cells.
    - The final 11x11 matrix is strictly normalized to sum to 1.0 within 1e-9.
    """
    if not prices:
        raise ValueError("Cannot build benchmark matrix from empty prices")

    # Invert prices
    inv_weights: dict[tuple[int, int], float] = {}
    for (h, a), p in prices.items():
        if p <= 1.0 or not math.isfinite(p):
            raise ValueError(f"Invalid price for ({h}, {a}): {p}")
        if 0 <= h <= max_score and 0 <= a <= max_score:
            inv_weights[(h, a)] = 1.0 / p

    w_other = 0.0
    if other_price is not None and other_price > 1.0 and math.isfinite(other_price):
        w_other = 1.0 / other_price

    total_weight = sum(inv_weights.values()) + w_other
    if total_weight <= 0.0:
        raise ValueError("Sum of inverse odds is non-positive")

    m = np.zeros((max_score + 1, max_score + 1), dtype=np.float64)
    for (h, a), w in inv_weights.items():
        m[h, a] = w / total_weight

    # Distribute other mass if present
    p_other = w_other / total_weight
    if p_other > 0.0:
        unquoted: list[tuple[int, int]] = [
            (h, a)
            for h in range(max_score + 1)
            for a in range(max_score + 1)
            if (h, a) not in inv_weights
        ]
        if unquoted:
            if reference_matrix is not None:
                _check_matrix(reference_matrix)
                tail_weights = np.array(
                    [float(reference_matrix[h, a]) for h, a in unquoted],
                    dtype=np.float64,
                )
                tail_sum = float(tail_weights.sum())
                if tail_sum > 1e-12:
                    probs = p_other * (tail_weights / tail_sum)
                    for (h, a), p_val in zip(unquoted, probs, strict=True):
                        m[h, a] = float(p_val)
                else:
                    share = p_other / len(unquoted)
                    for h, a in unquoted:
                        m[h, a] = share
            else:
                share = p_other / len(unquoted)
                for h, a in unquoted:
                    m[h, a] = share

    # Renormalize to ensure exact 1.0 sum
    m_sum = float(m.sum())
    if m_sum <= 0.0:
        raise ValueError("Benchmark matrix sum is 0")
    m /= m_sum
    assert np.isclose(float(m.sum()), 1.0, atol=1e-9)
    return m


@dataclass(frozen=True)
class MatrixComparisonResult:
    """Comprehensive comparison metrics between two exact score matrices (e.g. Fair vs Pinnacle)."""

    max_cell_diff: float
    mean_cell_diff: float
    tvd: float  # Total Variation Distance: 0.5 * sum(|m1 - m2|)
    rmse: float
    jsd: float  # Jensen-Shannon Divergence
    top1_fair: tuple[tuple[int, int], float]
    top1_pinnacle: tuple[tuple[int, int], float]
    top3_diff: float
    high_draws_diff: float  # {2:2, 3:3, 4:4}
    low_home_diff: float  # {1:0, 2:0, 2:1}
    low_away_diff: float  # {0:1, 0:2, 1:2}
    goals_2_4_diff: float
    home_win_diff: float
    draw_diff: float
    away_win_diff: float
    total_over_25_diff: float
    btts_diff: float
    cell_diffs: npt.NDArray[np.float64]


def compare_matrices(
    m_fair: npt.NDArray[np.float64],
    m_pinnacle: npt.NDArray[np.float64],
) -> MatrixComparisonResult:
    """Compare fair market matrix against Pinnacle benchmark across cell and multiscore metrics."""
    _check_matrix(m_fair)
    _check_matrix(m_pinnacle)
    if m_fair.shape != m_pinnacle.shape:
        raise ValueError(f"Shape mismatch: {m_fair.shape} vs {m_pinnacle.shape}")

    diff = m_fair - m_pinnacle
    abs_diff = np.abs(diff)
    max_cell = float(abs_diff.max())
    mean_cell = float(abs_diff.mean())
    tvd = 0.5 * float(abs_diff.sum())
    rmse = float(np.sqrt(np.mean(diff ** 2)))

    # Jensen-Shannon Divergence
    m_mid = 0.5 * (m_fair + m_pinnacle)
    eps = 1e-15
    f_safe = np.clip(m_fair, eps, 1.0)
    p_safe = np.clip(m_pinnacle, eps, 1.0)
    mid_safe = np.clip(m_mid, eps, 1.0)
    kl_fm = float(np.sum(f_safe * np.log(f_safe / mid_safe)))
    kl_pm = float(np.sum(p_safe * np.log(p_safe / mid_safe)))
    jsd = 0.5 * (kl_fm + kl_pm)

    # Top-1 scores
    idx_f = int(np.argmax(m_fair))
    h_f, a_f = divmod(idx_f, m_fair.shape[1])
    top1_f = ((h_f, a_f), float(m_fair[h_f, a_f]))

    idx_p = int(np.argmax(m_pinnacle))
    h_p, a_p = divmod(idx_p, m_pinnacle.shape[1])
    top1_p = ((h_p, a_p), float(m_pinnacle[h_p, a_p]))

    # Top-3 scores of fair matrix compared to same scores in pinnacle
    sorted_fair_indices = np.argsort(m_fair.ravel())[::-1][:3]
    top3_scores = [divmod(int(idx), m_fair.shape[1]) for idx in sorted_fair_indices]
    top3_f = calc_multiscore_set(m_fair, top3_scores)
    top3_p = calc_multiscore_set(m_pinnacle, top3_scores)

    # Multiscore sets
    high_draws = [(2, 2), (3, 3), (4, 4)]
    low_home = [(1, 0), (2, 0), (2, 1)]
    low_away = [(0, 1), (0, 2), (1, 2)]

    high_draws_diff = (
        calc_multiscore_set(m_fair, high_draws) - calc_multiscore_set(m_pinnacle, high_draws)
    )
    low_home_diff = (
        calc_multiscore_set(m_fair, low_home) - calc_multiscore_set(m_pinnacle, low_home)
    )
    low_away_diff = (
        calc_multiscore_set(m_fair, low_away) - calc_multiscore_set(m_pinnacle, low_away)
    )
    goals_2_4_diff = calc_goals_range(m_fair, 2, 4) - calc_goals_range(m_pinnacle, 2, 4)

    # 1X2 differences
    hw_diff = float(np.tril(m_fair, -1).sum()) - float(np.tril(m_pinnacle, -1).sum())
    dr_diff = float(np.diag(m_fair).sum()) - float(np.diag(m_pinnacle).sum())
    aw_diff = float(np.triu(m_fair, 1).sum()) - float(np.triu(m_pinnacle, 1).sum())

    # Totals and BTTS differences
    o25_diff = calc_total_over(m_fair, 2.5) - calc_total_over(m_pinnacle, 2.5)
    btts_diff = calc_btts(m_fair, yes=True) - calc_btts(m_pinnacle, yes=True)

    return MatrixComparisonResult(
        max_cell_diff=max_cell,
        mean_cell_diff=mean_cell,
        tvd=tvd,
        rmse=rmse,
        jsd=jsd,
        top1_fair=top1_f,
        top1_pinnacle=top1_p,
        top3_diff=top3_f - top3_p,
        high_draws_diff=high_draws_diff,
        low_home_diff=low_home_diff,
        low_away_diff=low_away_diff,
        goals_2_4_diff=goals_2_4_diff,
        home_win_diff=hw_diff,
        draw_diff=dr_diff,
        away_win_diff=aw_diff,
        total_over_25_diff=o25_diff,
        btts_diff=btts_diff,
        cell_diffs=diff,
    )


def load_pinnacle_correct_score_from_db(
    db_url: str,
    match_id: int,
    home_team: str | None = None,
    away_team: str | None = None,
    as_of: dt.datetime | None = None,
) -> tuple[dict[tuple[int, int], float], float | None]:
    """Load Pinnacle correct_score odds from odds_snapshots for a match (<= cutoff)."""
    prices: dict[tuple[int, int], float] = {}
    other_price: float | None = None

    with session_scope(db_url) as session:
        match = session.get(Match, match_id)
        cutoff = as_of or (match.commence_time_utc if match else None)

        stmt = select(OddsSnapshot).where(
            OddsSnapshot.match_id == match_id,
            OddsSnapshot.bookmaker == "pinnacle",
            OddsSnapshot.market == "correct_score",
        )
        if cutoff is not None:
            stmt = stmt.where(OddsSnapshot.captured_at_utc <= cutoff)

        stmt = stmt.order_by(OddsSnapshot.captured_at_utc.desc())
        all_rows = list(session.scalars(stmt))
        if not all_rows:
            return {}, None

        # Filter to the latest capture timestamp
        latest_ts = all_rows[0].captured_at_utc
        latest_rows = [r for r in all_rows if r.captured_at_utc == latest_ts]

        for r in latest_rows:
            p_val = float(r.price)
            parsed = parse_score_outcome(r.outcome, home_team=home_team, away_team=away_team)
            if parsed == "other":
                other_price = p_val
            else:
                prices[parsed] = p_val

    return prices, other_price
