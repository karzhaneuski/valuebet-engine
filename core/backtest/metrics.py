"""Backtest metrics: log loss, history bins, Brier, reliability (phase 4).

Pure functions over the prediction records produced by
``core.backtest.runner.predictions``; nothing here touches the DB, so
the test-period lock lives at the caller's door (``score_records``).
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from pathlib import Path

from core.backtest.periods import (
    FROZEN_CONFIG_PATH,
    ensure_period_allowed,
    period_of_season,
)

BIN_ORDER = ("0", "1-5", "6-15", ">15")
OVER_LINES = (("p_over_15", 1.5), ("p_over_25", 2.5), ("p_over_35", 3.5))


def score_records(records: Iterable[dict], period: str,
                  frozen_config: Path = FROZEN_CONFIG_PATH) -> list[dict]:
    """Gate + filter: only records of the given period may be scored.

    The lock check runs BEFORE any filtering, so even an empty test set
    cannot be requested while frozen config is absent.
    """
    ensure_period_allowed(period, frozen_config=frozen_config)
    return [r for r in records if period_of_season(r["season"]) == period]


def mean_log_loss(records: Sequence[dict]) -> float:
    return -sum(math.log(max(r["log_p"], 1e-300)) for r in records) / len(records)


def log_loss_by_bin(records: Sequence[dict]) -> dict[str, dict[str, float]]:
    """Mean log loss per history-depth bin (and per bin per league)."""
    out: dict[str, dict[str, float]] = {}
    for name in BIN_ORDER:
        sub = [r for r in records if r["depth_bin"] == name]
        if not sub:
            out[name] = {"n": 0, "mean": float("nan")}
            continue
        out[name] = {"n": len(sub), "mean": mean_log_loss(sub)}
    return out


def _brier(ps: Sequence[float], ys: Sequence[float]) -> float:
    return sum((p - y) ** 2 for p, y in zip(ps, ys, strict=True)) / len(ps)


def binary_metrics(records: Sequence[dict], key: str,
                   ) -> dict[str, float]:
    """Brier + base rate for one probability column ('p_over_25',
    'p_btts'). The event for over X goals is home+away goals > X; for
    BTTS both teams scored."""
    if key == "p_btts":
        events = [float(r["home_goals"] > 0 and r["away_goals"] > 0)
                  for r in records]
    else:
        line = float(key.rsplit("_", 1)[1]) / 10.0
        events = [float(r["home_goals"] + r["away_goals"] > line)
                  for r in records]
    probs = [float(r[key]) for r in records]
    return {"n": len(records),
            "brier": _brier(probs, events),
            "base_rate": sum(events) / len(events) if events else float("nan")}


def reliability(records: Sequence[dict], key: str,
                n_bins: int = 10) -> list[dict]:
    """Reliability curve for one probability column."""
    if key == "p_btts":
        events = [float(r["home_goals"] > 0 and r["away_goals"] > 0)
                  for r in records]
    else:
        line = float(key.rsplit("_", 1)[1]) / 10.0
        events = [float(r["home_goals"] + r["away_goals"] > line)
                  for r in records]
    buckets: dict[int, list[float]] = defaultdict(list)
    for p, y in zip((float(r[key]) for r in records), events, strict=True):
        idx = min(int(p * n_bins), n_bins - 1)
        buckets[idx].append(y)
    return [{"bin_lo": i / n_bins, "bin_hi": (i + 1) / n_bins,
             "n": len(ys), "observed": sum(ys) / len(ys)}
            for i, ys in sorted(buckets.items())]


def summarize(records: Sequence[dict]) -> dict:
    """The standard phase-4 summary for one set of records."""
    by_league: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_league[r["league"]].append(r)
    out: dict = {
        "n": len(records),
        "log_loss": mean_log_loss(records) if records else float("nan"),
        "bins": log_loss_by_bin(records),
        "ou_brier": {key: binary_metrics(records, key)
                     for key, _ in OVER_LINES},
        "btts_brier": binary_metrics(records, "p_btts"),
        "by_league": {
            lg: {"n": len(sub), "log_loss": mean_log_loss(sub),
                 "bins": log_loss_by_bin(sub)}
            for lg, sub in sorted(by_league.items())
        },
    }
    return out
