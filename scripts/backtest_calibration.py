"""Phase-4 part 3.3: exact-score and multiscore calibration (no odds).

Rebuilds each match's score matrix from the cached walk-forward models
(same configs -> cache hits) and evaluates reliability for:
- the model's top-1 exact score (p vs "that score happened");
- the top-3 score set (a typical multiscore set);
- the fixed set {1:0, 2:0, 2:1} (SPEC example);
- the 2-4 goals range.

Usage:
    DATABASE_URL=sqlite:////tmp/bt3.db uv run python -m scripts.backtest_calibration \
        --records .cache/bt_records/test_frozen.json --period test \
        --config '{"xi":0.002,"weight_xg":0.5}' \
        --out reports/backtest/part3_score_calibration.json
"""

from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from pathlib import Path

import numpy as np

from core.backtest.periods import ensure_period_allowed, period_of_season


def brier(ps, ys):
    return float(np.mean((np.array(ps) - np.array(ys)) ** 2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", required=True)
    parser.add_argument("--period", default="test")
    parser.add_argument("--config", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    ensure_period_allowed(args.period)
    from core.backtest.runner import fitted_model, load_quarantine_or_empty
    from core.config import ModelConfig

    cfg = ModelConfig(**json.loads(args.config))
    db_url = os.environ["DATABASE_URL"]
    records = json.loads(Path(args.records).read_text())
    records = [r for r in records
               if period_of_season(r["season"]) == args.period]
    quarantine = load_quarantine_or_empty()

    series: dict[str, tuple[list[float], list[float]]] = defaultdict(
        lambda: ([], []))
    model_cache: dict[tuple, object] = {}
    for r in records:
        as_of = r["as_of"]
        import datetime as dt

        as_of_dt = dt.datetime.fromisoformat(as_of)
        key = (r["league"], as_of)
        model = model_cache.get(key)
        if model is None:
            model, _ = fitted_model(db_url, r["league"], as_of_dt, cfg,
                                    Path(".cache/bt_models"), quarantine)
            model_cache[key] = model
        m = model.score_matrix(r["home"], r["away"])
        gh, ga = r["home_goals"], r["away_goals"]
        flat = [(p, (i, j)) for i, row in enumerate(m)
                for j, p in enumerate(row)]
        top = sorted(flat, reverse=True)[:3]
        p_top1, score_top1 = top[0]
        p_top3 = sum(p for p, _ in top)
        fixed_set = {(1, 0), (2, 0), (2, 1)}
        p_fixed = sum(m[i, j] for i, j in fixed_set)
        k = np.arange(m.shape[0])
        totals = np.add.outer(k, k)
        p_range24 = float(m[(totals >= 2) & (totals <= 4)].sum())

        events = {
            "top1_exact": (float((gh, ga) == score_top1), p_top1),
            "top3_multiscore": (float((gh, ga) in {s for _, s in top}),
                                p_top3),
            "fixed_set_1_0_2_0_2_1": (float((gh, ga) in fixed_set), p_fixed),
            "goals_range_2_4": (float(2 <= gh + ga <= 4), p_range24),
        }
        for name, (y, p) in events.items():
            series[name][0].append(float(p))
            series[name][1].append(float(y))

    out = {}
    for name, (ps, ys) in series.items():
        out[name] = {
            "n": len(ps),
            "mean_p": float(np.mean(ps)),
            "observed": float(np.mean(ys)),
            "brier": brier(ps, ys),
            "reliability": [
                {"bin_lo": b["bin_lo"], "bin_hi": b["bin_hi"],
                 "n": b["n"], "observed": b["observed"]}
                # reuse the reliability binning on raw (p, y) pairs
                for b in _reliability_raw(ps, ys)],
        }
    Path(args.out).write_text(json.dumps(out, indent=1))
    for name, d in out.items():
        print(f"{name:24s} n={d['n']:5d} mean_p={d['mean_p']:.4f} "
              f"observed={d['observed']:.4f} brier={d['brier']:.4f}")


def _reliability_raw(ps, ys, n_bins=10):
    buckets: dict[int, list[float]] = defaultdict(list)
    for p, y in zip(ps, ys, strict=True):
        buckets[min(int(p * n_bins), n_bins - 1)].append(y)
    return [{"bin_lo": i / n_bins, "bin_hi": (i + 1) / n_bins,
             "n": len(v), "observed": sum(v) / len(v)}
            for i, v in sorted(buckets.items())]


if __name__ == "__main__":
    main()
