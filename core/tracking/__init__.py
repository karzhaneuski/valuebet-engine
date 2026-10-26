"""User bets tracking, settlement, CLV audit, and performance reporting."""

from core.tracking.clv import (
    calculate_user_bet_clv,
    evaluate_fair_probs_for_bet,
    load_closing_market_inputs,
)
from core.tracking.settlement import (
    evaluate_bet_outcome,
    parse_totals_line,
    settle_user_bet,
)
from core.tracking.stats import (
    PerformanceReport,
    bootstrap_ci_95,
    compute_performance_metrics,
)

__all__ = [
    "PerformanceReport",
    "bootstrap_ci_95",
    "calculate_user_bet_clv",
    "compute_performance_metrics",
    "evaluate_bet_outcome",
    "evaluate_fair_probs_for_bet",
    "load_closing_market_inputs",
    "parse_totals_line",
    "settle_user_bet",
]
