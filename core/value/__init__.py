"""Fair pricing and value discovery engine (SPEC §4, §5, §7)."""

from __future__ import annotations

from core.value.fair_matrix import (
    FairMatrix,
    MarketInputs,
    MarketOdds1X2,
    MarketTotalsLine,
    build_fair_matrix,
    devig_1x2,
    devig_totals,
    fit_1x2_share,
)

__all__ = [
    "FairMatrix",
    "MarketInputs",
    "MarketOdds1X2",
    "MarketTotalsLine",
    "build_fair_matrix",
    "devig_1x2",
    "devig_totals",
    "fit_1x2_share",
]
