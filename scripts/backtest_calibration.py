"""Phase-4 review S3: calibration with quantile bins, Wilson CIs, cells.

For the frozen config on saved records (matrices rebuilt from the model
cache): quantile-bin reliability with Wilson CIs for O/U 2.5 and BTTS
(PNG), plus per-cell (0:0..3:3) and per-total (0..7+) tables with
z-statistics comparing mean predicted probability with observed
frequency. The multiscore calibration of part 3.3 (top-1/top-3/typical
sets) is preserved as --multiscore.

Usage:
    DATABASE_URL=sqlite:////tmp/bt3.db uv run python -m scripts.backtest_calibration \
        --records .cache/bt_records/test_frozen.json --period test \
        --config '{"xi":0.002,"weight_xg":0.5}' \
        --out reports/backtest/part3_score_calibration.json \
        --png reports/backtest/part3_calibration_quantile.png
"""

from __future__ import annotations

import argparse
import json
import math
import os
from collections import defaultdict
from datetime import datetime as dt
from pathlib import Path

import numpy as np

from core.backtest.periods import ensure_period_allowed, period_of_season
from core.backtest.runner import fitted_model, load_quarantine_or_empty
from core.config import ModelConfig


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return float("nan"), float("nan")
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return centre - half, centre + half


def z_score(p_mean: float, obs_rate: float, n: int) -> float:
    """Normal-approximation z for observed count vs mean predicted prob."""
    if n == 0 or p_mean <= 0 or p_mean >= 1:
        return float("nan")
    se = math.sqrt(p_mean * (1 - p_mean) / n)
    return (obs_rate - p_mean) / se


def quantile_bins(ps: list[float], ys: list[float], n_bins: int = 10
                   ) -> list[dict]:
    ps = np.asarray(ps)
    ys = np.asarray(ys)
    order = np.argsort(ps, kind="stable")
    chunks = np.array_split(order, n_bins)
    out = []
    for idx in chunks:
        if len(idx) == 0:
            continue
        k = int(ys[idx].sum())
        n = len(idx)
        lo, hi = wilson(k, n)
        out.append({"n": n, "mean_p": float(ps[idx].mean()),
                    "observed": k / n, "wilson_lo": lo, "wilson_hi": hi})
    return out


def brier(ps: list[float], ys: list[float]) -> float:
    return float(np.mean((np.asarray(ps) - np.asarray(ys)) ** 2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", required=True)
    parser.add_argument("--period", default="test")
    parser.add_argument("--config", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--png", default=None)
    parser.add_argument("--multiscore", action="store_true",
                        help="also emit the part-3.3 multiscore summary")
    args = parser.parse_args()
    ensure_period_allowed(args.period)

    cfg = ModelConfig(**json.loads(args.config))
    db_url = os.environ["DATABASE_URL"]
    records = [r for r in json.loads(Path(args.records).read_text())
               if period_of_season(r["season"]) == args.period]
    quarantine = load_quarantine_or_empty()

    ou25: tuple[list[float], list[float]] = ([], [])
    btts: tuple[list[float], list[float]] = ([], [])
    cells: dict[tuple[int, int], list[float]] = defaultdict(list)
    totals: dict[int, list[float]] = defaultdict(list)
    cell_hits: dict[tuple[int, int], int] = defaultdict(int)
    total_hits: dict[int, int] = defaultdict(int)
    multi = defaultdict(lambda: ([], []))
    for r in records:
        as_of = dt.fromisoformat(r["as_of"])
        model, _ = fitted_model(db_url, r["league"], as_of, cfg,
                                Path(".cache/bt_models"), quarantine)
        m = model.score_matrix(r["home"], r["away"])
        gh, ga = r["home_goals"], r["away_goals"]
        ou25[0].append(r["p_over_25"])
        ou25[1].append(1.0 if gh + ga > 2.5 else 0.0)
        btts[0].append(r["p_btts"])
        btts[1].append(1.0 if gh > 0 and ga > 0 else 0.0)
        total_grid = np.add.outer(np.arange(m.shape[0]),
                                  np.arange(m.shape[0]))
        for i in range(4):
            for j in range(4):
                cells[(i, j)].append(float(m[i, j]))
                if gh == i and ga == j:
                    cell_hits[(i, j)] += 1
        for key in range(8):
            mask = (total_grid == key) if key < 7 else (total_grid >= 7)
            totals[key].append(float(m[mask].sum()))
            if (gh + ga == key) or (key == 7 and gh + ga >= 7):
                total_hits[key] += 1
        if args.multiscore:
            flat = sorted(((float(m[i, j]), (i, j))
                           for i in range(m.shape[0])
                           for j in range(m.shape[0])), reverse=True)
            top3 = flat[:3]
            fixed = {(1, 0), (2, 0), (2, 1)}
            pairs = {
                "top1_exact": (top3[0][0], (gh, ga) == top3[0][1]),
                "top3_multiscore": (sum(p for p, _ in top3),
                                    (gh, ga) in {s for _, s in top3}),
                "fixed_set_1_0_2_0_2_1": (sum(m[i, j] for i, j in fixed),
                                          (gh, ga) in fixed),
                "goals_range_2_4": (float(m[(total_grid >= 2)
                                            & (total_grid <= 4)].sum()),
                                    2 <= gh + ga <= 4),
            }
            for name, (p, y) in pairs.items():
                multi[name][0].append(p)
                multi[name][1].append(1.0 if y else 0.0)

    out: dict = {
        "ou25": {"n": len(ou25[0]), "brier": brier(*ou25),
                 "bins": quantile_bins(*ou25)},
        "btts": {"n": len(btts[0]), "brier": brier(*btts),
                 "bins": quantile_bins(*btts)},
        "cells": [
            {"cell": f"{i}:{j}", "n": len(cells[(i, j)]),
             "mean_p": float(np.mean(cells[(i, j)])),
             "observed": cell_hits[(i, j)] / len(cells[(i, j)]),
             "z": z_score(float(np.mean(cells[(i, j)])),
                          cell_hits[(i, j)] / len(cells[(i, j)]),
                          len(cells[(i, j)]))}
            for i in range(4) for j in range(4)],
        "totals": [
            {"total": str(k) if k < 7 else "7+", "n": len(totals[k]),
             "mean_p": float(np.mean(totals[k])),
             "observed": total_hits[k] / len(totals[k]),
             "z": z_score(float(np.mean(totals[k])),
                          total_hits[k] / len(totals[k]), len(totals[k]))}
            for k in range(8)],
    }
    if args.multiscore:
        out["multiscore"] = {
            name: {"n": len(ps), "mean_p": float(np.mean(ps)),
                   "observed": float(np.mean(ys)), "brier": brier(ps, ys)}
            for name, (ps, ys) in multi.items()}
    Path(args.out).write_text(json.dumps(out, indent=1))

    print(f"ou25 brier {out['ou25']['brier']:.4f}, "
          f"btts brier {out['btts']['brier']:.4f}")
    print("cells (mean_p, observed, z):")
    for c in out["cells"]:
        flag = " *" if abs(c["z"]) > 2.0 else ""
        print(f"  {c['cell']}: {c['mean_p']:.4f} {c['observed']:.4f} "
              f"z={c['z']:+.1f}{flag}")
    print("totals:")
    for t in out["totals"]:
        flag = " *" if abs(t["z"]) > 2.0 else ""
        print(f"  {t['total']}: {t['mean_p']:.4f} {t['observed']:.4f} "
              f"z={t['z']:+.1f}{flag}")

    if args.png:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, axes = plt.subplots(1, 2, figsize=(11, 4.5))
        for ax, (name, key) in zip(axes, (("O/U 2.5", "ou25"),
                                          ("BTTS", "btts")), strict=True):
            xs = np.array([b["mean_p"] for b in out[key]["bins"]])
            ys = np.array([b["observed"] for b in out[key]["bins"]])
            los = np.array([b["wilson_lo"] for b in out[key]["bins"]])
            his = np.array([b["wilson_hi"] for b in out[key]["bins"]])
            ax.errorbar(xs, ys, yerr=[ys - los, his - ys], fmt="o", ms=4,
                        capsize=3, label="observed (Wilson 95%)")
            ax.plot([0, 1], [0, 1], "k--", lw=0.8)
            ax.set_title(f"{name} (Brier {out[key]['brier']:.4f})")
            ax.set_xlabel("mean predicted p (quantile bin)")
            ax.set_ylabel("observed frequency")
        fig.tight_layout()
        fig.savefig(args.png, dpi=130)
        print(f"png: {args.png}")


if __name__ == "__main__":
    main()
