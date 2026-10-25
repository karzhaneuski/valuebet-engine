"""Fair pricing and value discovery engine (SPEC §4, §5, §7)."""

from __future__ import annotations

from core.value.engine import (
    EvaluatedOutcome,
    EvaluationResult,
    calculate_kelly_quarter_capped,
    check_model_anomaly,
    evaluate_fortuna_prices,
)
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
from core.value.fortuna_input import (
    FortunaParseError,
    FortunaPrice,
    calculate_fortuna_overround,
    parse_fortuna_line,
    parse_fortuna_text,
)
from core.value.pinnacle_benchmark import (
    MatrixComparisonResult,
    build_pinnacle_benchmark_matrix,
    compare_matrices,
    load_pinnacle_correct_score_from_db,
    parse_score_outcome,
)

__all__ = [
    "EvaluatedOutcome",
    "EvaluationResult",
    "FairMatrix",
    "FortunaParseError",
    "FortunaPrice",
    "MarketInputs",
    "MarketOdds1X2",
    "MarketTotalsLine",
    "MatrixComparisonResult",
    "build_fair_matrix",
    "build_pinnacle_benchmark_matrix",
    "calculate_fortuna_overround",
    "calculate_kelly_quarter_capped",
    "check_model_anomaly",
    "compare_matrices",
    "devig_1x2",
    "devig_totals",
    "evaluate_fortuna_prices",
    "fit_1x2_share",
    "load_pinnacle_correct_score_from_db",
    "parse_fortuna_line",
    "parse_fortuna_text",
    "parse_score_outcome",
]
