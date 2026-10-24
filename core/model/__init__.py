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

__all__ = [
    "FittedModel",
    "LoadStats",
    "TrainingData",
    "TrainingMatch",
    "config_hash",
    "fit_dixon_coles",
    "fit_league",
    "load_quarantined_match_ids",
    "load_training_data",
    "quarantined_match_ids",
]
