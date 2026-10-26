"""User bet portfolio performance statistics and confidence intervals (SPEC §7).

Computes aggregate performance metrics:
- Total stake and total P/L
- ROI (yield on turnover) with 95% bootstrap CI
- Hit rate
- Average pre-kickoff CLV and share of positive CLV bets with 95% bootstrap CI
- Brier score
- Mandatory sample size warning for N < 100 bets
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from decimal import Decimal

import numpy as np
from pydantic import BaseModel

from core.orm import UserBet

logger = logging.getLogger(__name__)


class PerformanceReport(BaseModel):
    """Audited betting track record and CLV statistics."""

    total_bets: int = 0
    settled_bets: int = 0
    won_bets: int = 0
    lost_bets: int = 0
    push_bets: int = 0
    void_bets: int = 0
    open_bets: int = 0

    total_stake: Decimal = Decimal("0.00")
    total_pnl: Decimal = Decimal("0.00")
    roi: float = 0.0  # PnL / Stake
    hit_rate: float = 0.0  # Won / (Won + Lost)

    avg_clv: float | None = None
    positive_clv_share: float | None = None
    brier_score: float | None = None

    roi_ci_95: tuple[float, float] | None = None
    clv_ci_95: tuple[float, float] | None = None
    sample_size_warning: str | None = None


def bootstrap_ci_95(
    values: Sequence[float],
    n_resamples: int = 1000,
    seed: int = 42,
) -> tuple[float, float] | None:
    """Compute 95% bootstrap confidence interval (percentile method)."""
    if len(values) < 2:
        return None

    rng = np.random.default_rng(seed)
    arr = np.asarray(values, dtype=np.float64)
    n = len(arr)

    # Resample with replacement
    boot_indices = rng.integers(0, n, size=(n_resamples, n))
    boot_means = np.mean(arr[boot_indices], axis=1)

    low = float(np.percentile(boot_means, 2.5))
    high = float(np.percentile(boot_means, 97.5))
    return round(low, 4), round(high, 4)


def compute_performance_metrics(
    bets: Sequence[UserBet],
    n_bootstrap: int = 1000,
) -> PerformanceReport:
    """Compute full portfolio performance report across user bets."""
    report = PerformanceReport(total_bets=len(bets))
    if not bets:
        report.sample_size_warning = "⚠️ Нет зарегистрированных ставок."
        return report

    won_count = 0
    lost_count = 0
    push_count = 0
    void_count = 0
    open_count = 0

    tot_stake = Decimal("0.00")
    tot_pnl = Decimal("0.00")

    pnl_fractions: list[float] = []
    clv_values: list[float] = []
    brier_diffs: list[float] = []

    for b in bets:
        if b.status == "open":
            open_count += 1
            continue

        stake = b.stake_units or Decimal("1.00")
        pnl = b.pnl_units or Decimal("0.00")

        if b.status == "won":
            won_count += 1
            tot_stake += stake
            tot_pnl += pnl
            pnl_fractions.append(float(pnl / stake))
            if b.p_fair_at_placement is not None:
                brier_diffs.append((1.0 - float(b.p_fair_at_placement)) ** 2)

        elif b.status == "lost":
            lost_count += 1
            tot_stake += stake
            tot_pnl += pnl
            pnl_fractions.append(float(pnl / stake))
            if b.p_fair_at_placement is not None:
                brier_diffs.append((0.0 - float(b.p_fair_at_placement)) ** 2)

        elif b.status == "push":
            push_count += 1
            tot_stake += stake
            tot_pnl += Decimal("0.00")
            pnl_fractions.append(0.0)

        elif b.status in ("void", "cancelled"):
            void_count += 1

        if b.clv_fair is not None:
            clv_values.append(float(b.clv_fair))

    settled = won_count + lost_count + push_count + void_count
    report.settled_bets = settled
    report.won_bets = won_count
    report.lost_bets = lost_count
    report.push_bets = push_count
    report.void_bets = void_count
    report.open_bets = open_count

    report.total_stake = tot_stake
    report.total_pnl = tot_pnl

    if tot_stake > Decimal("0.00"):
        report.roi = round(float(tot_pnl / tot_stake), 4)

    if (won_count + lost_count) > 0:
        report.hit_rate = round(won_count / (won_count + lost_count), 4)

    if clv_values:
        report.avg_clv = round(float(np.mean(clv_values)), 4)
        pos_clv = sum(1 for c in clv_values if c > 0)
        report.positive_clv_share = round(pos_clv / len(clv_values), 4)
        report.clv_ci_95 = bootstrap_ci_95(clv_values, n_resamples=n_bootstrap)

    if pnl_fractions:
        report.roi_ci_95 = bootstrap_ci_95(pnl_fractions, n_resamples=n_bootstrap)

    if brier_diffs:
        report.brier_score = round(float(np.mean(brier_diffs)), 4)

    # Small sample warning (< 100 settled bets)
    if settled < 100:
        report.sample_size_warning = (
            f"⚠️ Выборка (N={settled}) статистически недостаточна "
            "для выводов о прибыльности. Высокая дисперсия."
        )

    return report
