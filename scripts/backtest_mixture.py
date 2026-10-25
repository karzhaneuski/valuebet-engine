"""Phase 4b B4: logit mixture on O/U 2.5 + CLV vs pre-match prices.

On VALIDATION only: fit logit(p_mix) = w0 + w1*logit(p_model) +
w2*logit(p_market) by maximum likelihood on the prematch-Avg market
probability, compare binary log loss against the pure components
(paired week bootstrap), then measure CLV of the model's edge-bets:
share of bets whose side the market moved TOWARDS by the close
(p_closing > p_prematch on our side) and the mean probability shift.

Usage:
    DATABASE_URL=sqlite:////tmp/bt4b.db uv run python -m scripts.backtest_mixture \
        --records .cache/bt_records/xiw_xi0.002_w0.5.json --period validation \
        --config '{"xi":0.002,"weight_xg":0.5}' \
        --out reports/backtest/4b_mixture.json
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime as dt
from pathlib import Path

import numpy as np
from scipy.optimize import minimize
from sqlalchemy import select

from core.backtest.metrics import score_records
from core.backtest.periods import ensure_period_allowed
from core.db import session_scope
from core.orm import ClosingOdds, PrematchOdds


def logit(p: np.ndarray) -> np.ndarray:
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return np.log(p / (1 - p))


def sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-x))


def devig(pair: tuple[float, float], over: bool) -> float:
    o, u = 1.0 / pair[0], 1.0 / pair[1]
    po = o / (o + u)
    return po if over else 1 - po


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", required=True)
    parser.add_argument("--period", default="validation")
    parser.add_argument("--config", default="{}")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    ensure_period_allowed(args.period)

    records = json.loads(Path(args.records).read_text())
    for r in records:
        r.setdefault("p_score", r.pop("log_p", None))
    records = score_records(records, args.period)
    db = os.environ["DATABASE_URL"]
    pre: dict[int, tuple[float, float]] = {}
    clo: dict[int, tuple[float, float]] = {}
    with session_scope(db) as s:
        for c in s.scalars(select(PrematchOdds).where(
                PrematchOdds.bookmaker == "Avg")):
            if c.over_price and c.under_price:
                pre[c.match_id] = (float(c.over_price), float(c.under_price))
        for c in s.scalars(select(ClosingOdds).where(
                ClosingOdds.bookmaker == "AvgC")):
            if c.over_price and c.under_price:
                clo[c.match_id] = (float(c.over_price), float(c.under_price))

    pm, pk, y, weeks = [], [], [], []
    for r in records:
        pair = pre.get(r["match_id"])
        if not pair or min(pair) <= 1.0:
            continue
        y.append(1.0 if r["home_goals"] + r["away_goals"] > 2.5 else 0.0)
        pm.append(r["p_over_25"])
        pk.append(devig(pair, over=True))
        yy, ww, _ = dt.fromisoformat(r["kickoff"]).isocalendar()
        weeks.append(f"{yy}-W{ww:02d}")
    pm, pk, y = np.array(pm), np.array(pk), np.array(y)
    weeks = np.array(weeks)

    def nll(w: np.ndarray) -> float:
        p = sigmoid(w[0] + w[1] * logit(pm) + w[2] * logit(pk))
        return -float(np.mean(y * np.log(p + 1e-12)
                              + (1 - y) * np.log(1 - p + 1e-12)))

    res = minimize(nll, x0=np.array([0.0, 0.5, 0.5]), method="Nelder-Mead",
                   options={"xatol": 1e-8, "fatol": 1e-10, "maxiter": 4000})
    w0, w1, w2 = res.x
    p_mix = sigmoid(w0 + w1 * logit(pm) + w2 * logit(pk))

    def ll(p):
        p = np.clip(p, 1e-12, 1 - 1e-12)
        return -(y * np.log(p) + (1 - y) * np.log(1 - p))

    def boot_diff(a_terms, b_terms):
        uniq = np.unique(weeks)
        g = [(a_terms - b_terms)[weeks == w] for w in uniq]
        rng = np.random.default_rng(7)
        out = np.empty(2000)
        for i in range(2000):
            pick = rng.integers(0, len(g), len(g))
            out[i] = np.concatenate([g[j] for j in pick]).mean()
        lo, hi = np.percentile(out, [2.5, 97.5])
        return float(lo), float(hi)

    ll_m, ll_k, ll_x = ll(pm), ll(pk), ll(p_mix)
    out = {
        "period": args.period, "n": len(y),
        "weights": {"w0": float(w0), "w_model": float(w1),
                    "w_market": float(w2)},
        "logloss_model": float(ll_m.mean()),
        "logloss_market": float(ll_k.mean()),
        "logloss_mixture": float(ll_x.mean()),
        "mix_vs_model_ci95": boot_diff(ll_x, ll_m),
        "mix_vs_market_ci95": boot_diff(ll_x, ll_k),
        "market_vs_model_ci95": boot_diff(ll_k, ll_m),
    }

    # CLV of the model's edge bets (threshold 2%, both sides,
    # executed/evaluated at closing prices)
    moved_toward = moved_against = n_bets = 0
    shifts = []
    for r in score_records(records, args.period):
        p0_pair = pre.get(r["match_id"])
        p1_pair = clo.get(r["match_id"])
        if not p0_pair or not p1_pair:
            continue
        for over, p in ((True, r["p_over_25"]),
                        (False, 1 - r["p_over_25"])):
            price1 = p1_pair[0] if over else p1_pair[1]
            if p * price1 - 1.0 < 0.02:
                continue
            prob0 = devig(p0_pair, over)
            prob1 = devig(p1_pair, over)
            n_bets += 1
            shifts.append(prob1 - prob0)
            if prob1 > prob0:
                moved_toward += 1
            else:
                moved_against += 1
    out["clv"] = {
        "bets": n_bets,
        "share_moved_toward": moved_toward / n_bets if n_bets else
        float("nan"),
        "mean_probability_shift": float(np.mean(shifts)) if shifts
        else float("nan"),
    }
    Path(args.out).write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
