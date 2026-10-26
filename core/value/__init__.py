"""Fair pricing and value discovery engine (SPEC §4, §5, §7)."""

from __future__ import annotations

from core.value.engine import (
    EvaluatedOutcome,
    EvaluationResult,
    calculate_kelly_quarter_capped,
    check_model_anomaly,
    evaluate_fortuna_prices,
    match_key_player,
    normalize_player_name,
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
    calculate_section_overrounds,
    parse_fortuna_line,
    parse_fortuna_text,
)
from core.value.persistence import (
    find_matches_by_query,
    load_market_inputs_for_match,
    save_recommendations_to_db,
)
from core.value.pinnacle_benchmark import (
    MatrixComparisonResult,
    build_pinnacle_benchmark_matrix,
    compare_matrices,
    load_pinnacle_correct_score_from_db,
    parse_score_outcome,
)
from core.value.prefilter import (
    CandidateMatch,
    PrefilterDecisionRecord,
    PrefilterReport,
    compute_high_draws_mass,
    filter_candidates,
    log_prefilter_decisions_to_db,
)
from core.value.standby_model import (
    get_key_players_for_team,
    get_model_home_share_and_model,
    has_team_lineups,
)

__all__ = [
    "CandidateMatch",
    "EvaluatedOutcome",
    "EvaluationResult",
    "FairMatrix",
    "FortunaParseError",
    "FortunaPrice",
    "MarketInputs",
    "MarketOdds1X2",
    "MarketTotalsLine",
    "MatrixComparisonResult",
    "PrefilterDecisionRecord",
    "PrefilterReport",
    "build_fair_matrix",
    "build_pinnacle_benchmark_matrix",
    "calculate_fortuna_overround",
    "calculate_kelly_quarter_capped",
    "calculate_section_overrounds",
    "check_model_anomaly",
    "compare_matrices",
    "compute_high_draws_mass",
    "devig_1x2",
    "devig_totals",
    "evaluate_fortuna_prices",
    "filter_candidates",
    "find_matches_by_query",
    "fit_1x2_share",
    "get_key_players_for_team",
    "get_model_home_share_and_model",
    "has_team_lineups",
    "load_market_inputs_for_match",
    "load_pinnacle_correct_score_from_db",
    "log_prefilter_decisions_to_db",
    "match_key_player",
    "normalize_player_name",
    "parse_fortuna_line",
    "parse_fortuna_text",
    "parse_score_outcome",
    "save_recommendations_to_db",
]
