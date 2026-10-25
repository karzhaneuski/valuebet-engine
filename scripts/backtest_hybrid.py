"""Phase 4b B2/B3: hybrid matrix - market total level + model shape.

For each saved walk-forward record the hybrid matrix is
proxy_score_matrix(lambda_total from the de-vigged MARKET over 2.5
probability, home share from the model's expected goals, rho from the
model). Metrics on the VALIDATION records only (test is one-shot in
B5): exact-score log loss vs the pure model with a paired calendar-week
bootstrap CI, plus 0:0..3:3 cell z-tables for model / hybrid (B3 uses
the same machinery for the draw correction).

Usage:
    DATABASE_URL=sqlite:////tmp/bt4b.db uv run python -m scripts.backtest_hybrid \
        --records .cache/bt_records/xiw_xi0.002_w0.5.json --period validation \
        --source prematch --config '{"xi":0.002,"weight_xg":0.5}' \
        [--draw-kappa 0.0] [--hybrid-rho-override None] \
        --out reports/backtest/4b_hybrid_prematch.json
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
from sqlalchemy import select

from core.backtest.metrics import score_records
from core.backtest.periods import ensure_period_allowed
from core.backtest.runner import fitted_model, load_quarantine_or_empty
from core.config import ModelConfig
from core.db import session_scope
from core.model.markets import market_lambda_from_totals, proxy_score_matrix
from core.orm import ClosingOdds, PrematchOdds


def load_odds(db_url: str, source: str) -> dict[int, tuple[float, float]]:
    table = PrematchOdds if source == "prematch" else ClosingOdds
    book = "Avg" if source == "prematch" else "AvgC"
    with session_scope(db_url) as s:
        return {c.match_id: (float(c.over_price), float(c.under_price))
                for c in s.scalars(select(table).where(table.bookmaker
                                                      == book))
                if c.over_price and c.under_price}


def devig(pair: tuple[float, float]) -> float:
    o, u = 1.0 / pair[0], 1.0 / pair[1]
    return o / (o + u)


def draw_inflate(m: np.ndarray, kappa: float,
                 high_only: bool = False) -> np.ndarray:
    """B3 draw corrections, renormalised.

    kappa: flat inflation of the whole diagonal (option a).
    high_only: inflate ONLY the high draws (2:2, 3:3, ...) - the flat
    version cures the 2:2 deficit but creates a 0:0 surplus (the model
    already OVERprices 0:0), so the correction must skip 0:0/1:1.
    """
    if kappa == 0.0:
        return m
    out = m.copy()
    idx = np.arange(min(out.shape))
    for i in idx:
        if high_only and i < 2:
            continue
        out[i, i] *= 1.0 + kappa
    return out / out.sum()


def z_stat(p_mean: float, obs: float, n: int) -> float:
    if n == 0 or p_mean <= 0 or p_mean >= 1:
        return float("nan")
    return (obs - p_mean) / math.sqrt(p_mean * (1 - p_mean) / n)


def cell_table(matrices: list[np.ndarray], goals: list[tuple[int, int]]
               ) -> list[dict]:
    acc: dict[tuple[int, int], list[float]] = defaultdict(list)
    hits: dict[tuple[int, int], int] = defaultdict(int)
    for m, (gh, ga) in zip(matrices, goals, strict=True):
        for i in range(4):
            for j in range(4):
                acc[(i, j)].append(float(m[i, j]))
                if gh == i and ga == j:
                    hits[(i, j)] += 1
    return [{"cell": f"{i}:{j}", "mean_p": float(np.mean(acc[(i, j)])),
             "observed": hits[(i, j)] / len(acc[(i, j)]),
             "z": z_stat(float(np.mean(acc[(i, j)])),
                         hits[(i, j)] / len(acc[(i, j)]),
                         len(acc[(i, j)]))}
            for i in range(4) for j in range(4)]


def week_cluster_boot(diff_terms: list[float], weeks: list[str],
                      n: int = 2000, seed: int = 7) -> tuple[float, float]:
    arr = np.asarray(diff_terms)
    wk = np.asarray(weeks)
    groups = [arr[wk == w] for w in np.unique(wk)]
    rng = np.random.default_rng(seed)
    means = np.empty(n)
    for i in range(n):
        pick = rng.integers(0, len(groups), len(groups))
        means[i] = np.concatenate([groups[j] for j in pick]).mean()
    lo, hi = np.percentile(means, [2.5, 97.5])
    return float(lo), float(hi)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", required=True)
    parser.add_argument("--period", default="validation")
    parser.add_argument("--source", choices=("prematch", "closing"),
                        default="prematch")
    parser.add_argument("--config", required=True)
    parser.add_argument("--draw-kappa", type=float, default=0.0)
    parser.add_argument("--rho-override", type=float, default=None,
                        help="B3 option (b): fixed rho for the hybrid "
                             "matrix instead of the model's fitted rho")
    parser.add_argument("--draw-kappa-high", type=float, default=0.0,
                        help="B3 option (c): inflate only 2:2/3:3/...")
    parser.add_argument("--multiscore", action="store_true",
                        help="multiscore calibration for the hybrid "
                             "matrices (top-1, top-3, fixed set, 2-4)")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    ensure_period_allowed(args.period)

    cfg = ModelConfig(**json.loads(args.config))
    db_url = os.environ["DATABASE_URL"]
    records = json.loads(Path(args.records).read_text())
    for r in records:
        r.setdefault("p_score", r.pop("log_p", None))
    records = score_records(records, args.period)
    odds = load_odds(db_url, args.source)
    quarantine = load_quarantine_or_empty()

    models: dict[tuple, object] = {}
    ll_model, ll_hybrid = [], []
    weeks, goals, hybrid_mats, pure_mats = [], [], [], []
    used = missing = 0
    for r in records:
        pair = odds.get(r["match_id"])
        if not pair or min(pair) <= 1.0:
            missing += 1
            continue
        as_of = dt.fromisoformat(r["as_of"])
        key = (r["league"], r["as_of"])
        model = models.get(key)
        if model is None:
            model, _ = fitted_model(db_url, r["league"], as_of, cfg,
                                    Path(".cache/bt_models"), quarantine)
            models[key] = model
        try:
            lam_total = market_lambda_from_totals(devig(pair), 2.5)
            eh, ea = model.expected_goals(r["home"], r["away"])
            share = eh / (eh + ea)
            rho = model.rho if args.rho_override is None \
                else args.rho_override
            m = proxy_score_matrix(lam_total, share, rho=rho)
        except (ValueError, KeyError):
            missing += 1
            continue
        m = draw_inflate(m, args.draw_kappa)
        m = draw_inflate(m, args.draw_kappa_high, high_only=True)
        gh, ga = r["home_goals"], r["away_goals"]
        ll_model.append(-math.log(max(r["p_score"], 1e-300)))
        ll_hybrid.append(-math.log(max(float(m[gh, ga]), 1e-300)))
        y, w, _ = dt.fromisoformat(r["kickoff"]).isocalendar()
        weeks.append(f"{y}-W{w:02d}")
        goals.append((gh, ga))
        hybrid_mats.append(m)
        pure_mats.append(model.score_matrix(r["home"], r["away"]))
        used += 1

    ll_m = float(np.mean(ll_model))
    ll_h = float(np.mean(ll_hybrid))
    delta = ll_h - ll_m
    lo, hi = week_cluster_boot((np.asarray(ll_hybrid) - np.asarray(ll_model)
                               ).tolist(), weeks)
    out = {
        "period": args.period, "source": args.source,
        "draw_kappa": args.draw_kappa,
        "draw_kappa_high": args.draw_kappa_high,
        "per_match_ll_model": ll_model,
        "per_match_ll_hybrid": ll_hybrid,
        "per_match_weeks": weeks,
        "rho_override": args.rho_override,
        "model_cells": cell_table(pure_mats, goals),
        "used": used, "missing_odds": missing,
        "log_loss_model": ll_m, "log_loss_hybrid": ll_h,
        "delta_hybrid_minus_model": delta,
        "delta_ci95": [lo, hi],
        "hybrid_cells": cell_table(hybrid_mats, goals),
    }
    if args.multiscore:
        fixed = {(1, 0), (2, 0), (2, 1)}
        series: dict[str, tuple[list[float], list[float]]] = defaultdict(
            lambda: ([], []))
        for m, (gh, ga) in zip(hybrid_mats, goals, strict=True):
            flat = sorted(((float(m[i, j]), (i, j))
                           for i in range(m.shape[0])
                           for j in range(m.shape[0])), reverse=True)
            top3 = flat[:3]
            tg = np.add.outer(np.arange(m.shape[0]),
                              np.arange(m.shape[0]))
            for name, p, hit in (
                    ("top1_exact", top3[0][0], (gh, ga) == top3[0][1]),
                    ("top3_multiscore", sum(x for x, _ in top3),
                     (gh, ga) in {s2 for _, s2 in top3}),
                    ("fixed_set_1_0_2_0_2_1",
                     sum(m[i, j] for i, j in fixed), (gh, ga) in fixed),
                    ("goals_range_2_4",
                     float(m[(tg >= 2) & (tg <= 4)].sum()),
                     2 <= gh + ga <= 4)):
                series[name][0].append(p)
                series[name][1].append(1.0 if hit else 0.0)
        out["multiscore"] = {
            name: {"n": len(ps), "mean_p": float(np.mean(ps)),
                   "observed": float(np.mean(ys))}
            for name, (ps, ys) in series.items()}
    Path(args.out).write_text(json.dumps(out, indent=1))
    print(f"[{args.period}/{args.source}] n={used} LL model {ll_m:.4f} "
          f"hybrid {ll_h:.4f} delta {delta:+.4f} CI95 [{lo:+.4f}, {hi:+.4f}]")
    big = [(c["cell"], round(c["z"], 1)) for c in out["hybrid_cells"]
           if abs(c["z"]) > 2]
    print("hybrid cells |z|>2:", big if big else "none")
    mbig = [(c["cell"], round(c["z"], 1)) for c in out["model_cells"]
            if abs(c["z"]) > 2]
    print("model  cells |z|>2:", mbig if mbig else "none")


if __name__ == "__main__":
    main()
