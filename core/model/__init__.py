"""Dixon-Coles model package (phase 3): data selection, fitter, markets."""

from core.model.dataset import (
    LoadStats,
    TrainingData,
    TrainingMatch,
    load_quarantined_match_ids,
    load_training_data,
    quarantined_match_ids,
)
from core.model.dixon_coles import (
    FittedModel,
    config_hash,
    fit_dixon_coles,
    fit_league,
)
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

__all__ = [
    "FittedModel",
    "LoadStats",
    "TrainingData",
    "TrainingMatch",
    "btts",
    "config_hash",
    "exact_score",
    "fit_dixon_coles",
    "fit_league",
    "goals_range",
    "load_quarantined_match_ids",
    "load_training_data",
    "market_lambda_from_totals",
    "multiscore_set",
    "proxy_score_matrix",
    "quarantined_match_ids",
    "total_over",
    "total_under",
]
