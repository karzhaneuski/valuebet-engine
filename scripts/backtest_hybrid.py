"""Phase 4b B2/B3 & review updates: hybrid and market score matrices.

Supports:
- --share model (B2 baseline: market totals level + model attack/defence share)
- --share market_1x2 (market totals + 1X2 devigged implied home share)
- --all-market (fit lam_home, lam_away, rho to O/U 2.5 + 1X2 without model)
- --draw-kappa and --draw-kappa-high (B3 draw corrections)
- --frozen-config (S2 lock gate for test period)
- --cross-fit (evaluates cross-season alpha and kappa fitting)
- Breakdown by season with calendar-week clustered bootstrap CI95

Usage:
    DATABASE_URL=sqlite:////tmp/bt4b.db uv run python -m scripts.backtest_hybrid \
        --records .cache/bt_records/xiw_xi0.002_w0.5.json --period validation \
        --source prematch --config '{"xi":0.002,"weight_xg":0.5}' \
        --share market_1x2 --out reports/backtest/4b_hybrid_market_1x2.json
"""

from __future__ import annotations

import argparse
import json
import math
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime as dt
from pathlib import Path

import numpy as np
from scipy.optimize import brentq, least_squares, minimize_scalar
from sqlalchemy import select

from core.backtest.metrics import score_records
from core.backtest.periods import (
    FROZEN_CONFIG_4B_PATH,
    ensure_period_allowed,
    ensure_period_allowed_4b,
)
from core.backtest.runner import fitted_model, load_quarantine_or_empty
from core.config import ModelConfig
from core.db import session_scope
from core.model.markets import market_lambda_from_totals, proxy_score_matrix
from core.orm import ClosingOdds, PrematchOdds


@dataclass
class MatchOdds:
    over: float
    under: float
    home: float | None = None
    draw: float | None = None
    away: float | None = None


def load_odds(db_url: str, source: str, bookmaker: str | None = None) -> dict[int, MatchOdds]:
    table = PrematchOdds if source == "prematch" else ClosingOdds
    book = ("Avg" if source == "prematch" else "AvgC") if bookmaker is None else bookmaker
    with session_scope(db_url) as s:
        out: dict[int, MatchOdds] = {}
        for c in s.scalars(select(table).where(table.bookmaker == book)):
            if c.over_price and c.under_price:
                out[c.match_id] = MatchOdds(
                    over=float(c.over_price),
                    under=float(c.under_price),
                    home=float(c.home_price) if c.home_price else None,
                    draw=float(c.draw_price) if c.draw_price else None,
                    away=float(c.away_price) if c.away_price else None,
                )
        return out


def devig_totals(over: float, under: float) -> float:
    o, u = 1.0 / over, 1.0 / under
    return o / (o + u)


def devig_1x2(home: float, draw: float, away: float) -> tuple[float, float, float]:
    ih, id_, ia = 1.0 / home, 1.0 / draw, 1.0 / away
    s = ih + id_ + ia
    return ih / s, id_ / s, ia / s


def draw_inflate(m: np.ndarray, kappa: float,
                 high_only: bool = False) -> np.ndarray:
    """B3 draw corrections, renormalised."""
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
               ) -> list[dict[str, object]]:
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
    uniq = np.unique(wk)
    groups = [arr[wk == w] for w in uniq]
    rng = np.random.default_rng(seed)
    means = np.empty(n)
    for i in range(n):
        pick = rng.integers(0, len(groups), len(groups))
        means[i] = np.concatenate([groups[j] for j in pick]).mean()
    lo, hi = np.percentile(means, [2.5, 97.5])
    return float(lo), float(hi)


def fit_1x2_share(lam_total: float, ph: float, pa: float, rho: float,
                  s_fallback: float = 0.5) -> float:
    """Solve for home goal share s such that P(H)/(P(H)+P(A)) = ph/(ph+pa)."""
    target = ph / (ph + pa)

    def f(s: float) -> float:
        m = proxy_score_matrix(lam_total, s, rho=rho)
        sh = float(np.tril(m, -1).sum())
        sa = float(np.triu(m, 1).sum())
        return (sh / (sh + sa)) - target

    try:
        return float(brentq(f, 0.01, 0.99, xtol=1e-6))
    except Exception:
        return s_fallback


def fit_all_market(p_over: float, ph: float, pd: float,
                   rho_override: float | None = None,
                   rho_start: float = 0.0) -> tuple[float, float, float]:
    """Fit lam_home, lam_away, rho to match O/U 2.5 and 1X2 without the model."""
    lam_tot0 = market_lambda_from_totals(p_over, 2.5)
    s0 = ph / (ph + (1.0 - ph - pd))

    if rho_override is not None:
        fixed_rho = float(rho_override)
        pa = 1.0 - ph - pd
        target_ratio = ph / (ph + pa)

        def f_ratio(s: float) -> float:
            m = proxy_score_matrix(lam_tot0, s, rho=fixed_rho)
            sh = float(np.tril(m, -1).sum())
            sa = float(np.triu(m, 1).sum())
            return (sh / (sh + sa)) - target_ratio

        try:
            s_opt = float(brentq(f_ratio, 0.01, 0.99, xtol=1e-6))
        except Exception:
            s_opt = s0
        return lam_tot0 * s_opt, lam_tot0 * (1.0 - s_opt), fixed_rho

    x0 = [lam_tot0 * s0, lam_tot0 * (1.0 - s0), rho_start]

    def residuals(x: list[float] | np.ndarray) -> list[float]:
        lh, la, r = x
        try:
            m = proxy_score_matrix(lh + la, lh / (lh + la), rho=float(r))
        except Exception:
            return [10.0, 10.0, 10.0]
        n = m.shape[0]
        totals = np.add.outer(np.arange(n), np.arange(n))
        return [
            float(m[totals > 2.5].sum()) - p_over,
            float(np.tril(m, -1).sum()) - ph,
            float(np.diag(m).sum()) - pd,
        ]

    res = least_squares(residuals, x0, bounds=([0.05, 0.05, -0.15], [6.0, 6.0, 0.15]),
                        xtol=1e-5, ftol=1e-5)
    return float(res.x[0]), float(res.x[1]), float(res.x[2])


def run_compare(path_a: str, path_b: str, out_path: str) -> None:
    data_a = json.loads(Path(path_a).read_text())
    data_b = json.loads(Path(path_b).read_text())
    matches_a = {m["match_id"]: m for m in data_a.get("per_match", [])}
    matches_b = {m["match_id"]: m for m in data_b.get("per_match", [])}
    common_ids = sorted(set(matches_a.keys()) & set(matches_b.keys()))
    if not common_ids:
        raise ValueError(f"No common match_ids between {path_a} and {path_b}")

    diffs: list[float] = []
    weeks: list[str] = []
    seasons: list[int] = []
    per_match = []
    for mid in common_ids:
        ma = matches_a[mid]
        mb = matches_b[mid]
        delta_m = float(ma["ll_hybrid"]) - float(mb["ll_hybrid"])
        diffs.append(delta_m)
        weeks.append(ma["week"])
        seasons.append(int(ma["season"]))
        per_match.append({
            "match_id": mid,
            "week": ma["week"],
            "season": int(ma["season"]),
            "ll_a": float(ma["ll_hybrid"]),
            "ll_b": float(mb["ll_hybrid"]),
            "delta": delta_m,
        })

    arr_diff = np.asarray(diffs)
    arr_seasons = np.asarray(seasons)
    mean_delta = float(np.mean(arr_diff))
    ci_lo, ci_hi = week_cluster_boot(diffs, weeks)

    by_season: dict[str, dict[str, object]] = {}
    for sn in sorted(set(seasons)):
        mask = (arr_seasons == sn)
        diff_sn = arr_diff[mask].tolist()
        weeks_sn = np.asarray(weeks)[mask].tolist()
        lo_sn, hi_sn = week_cluster_boot(diff_sn, weeks_sn)
        by_season[str(sn)] = {
            "n": int(mask.sum()),
            "mean_delta": float(np.mean(arr_diff[mask])),
            "ci95": [lo_sn, hi_sn],
        }

    out = {
        "artifact_a": path_a,
        "artifact_b": path_b,
        "n_matches": len(common_ids),
        "n_weeks": len(set(weeks)),
        "mean_delta": mean_delta,
        "ci95": [ci_lo, ci_hi],
        "by_season": by_season,
        "per_match": per_match,
    }
    Path(out_path).write_text(json.dumps(out, indent=1))
    print(f"[compare] {path_a} vs {path_b}: n={len(common_ids)} "
          f"delta={mean_delta:+.4f} CI95 [{ci_lo:+.4f}, {ci_hi:+.4f}]")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compare", nargs=2, metavar=("RUN_A", "RUN_B"),
                        help="Pairwise comparison of two run artifacts (A vs B)")
    parser.add_argument("--records", default=None)
    parser.add_argument("--period", default="validation")
    parser.add_argument("--source", choices=("prematch", "closing"),
                        default="prematch")
    parser.add_argument("--bookmaker", default=None,
                        help="Specific bookmaker e.g. P or PC (default Avg/AvgC)")
    parser.add_argument("--config", default=None)
    parser.add_argument("--share", choices=("model", "market_1x2"), default="model",
                        help="Source of home goal share: statistical model or 1X2 market")
    parser.add_argument("--all-market", action="store_true",
                        help="Fit lam_home, lam_away, rho entirely to O/U 2.5 + 1X2")
    parser.add_argument("--draw-kappa", type=float, default=0.0)
    parser.add_argument("--draw-kappa-high", type=float, default=0.0,
                        help="B3 option (c): inflate only 2:2/3:3/...")
    parser.add_argument("--rho-override", type=float, default=None,
                        help="Fixed rho override for the matrix instead of model/fitted rho")
    parser.add_argument("--multiscore", action="store_true",
                        help="Multiscore calibration for the hybrid matrices")
    parser.add_argument("--cross-fit", action="store_true",
                        help="Evaluate cross-season alpha blending and kappa fitting")
    parser.add_argument("--frozen-config", default=str(FROZEN_CONFIG_4B_PATH),
                        help="Path to frozen config for test period lock (S2)")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    if args.compare:
        run_compare(args.compare[0], args.compare[1], args.out)
        return

    if not args.records or not args.config:
        parser.error("--records and --config are required when not using --compare")

    frozen_cfg_path = Path(args.frozen_config)
    ensure_period_allowed_4b(args.period, frozen_config=frozen_cfg_path)
    ensure_period_allowed(args.period, frozen_config=frozen_cfg_path)

    cfg = ModelConfig(**json.loads(args.config))
    db_url = os.environ["DATABASE_URL"]
    raw_records = json.loads(Path(args.records).read_text())
    for r in raw_records:
        r.setdefault("p_score", r.pop("log_p", None))
    records = score_records(raw_records, args.period, frozen_config=frozen_cfg_path)
    odds = load_odds(db_url, args.source, bookmaker=args.bookmaker)
    quarantine = load_quarantine_or_empty()

    models: dict[tuple[str, str], object] = {}
    ll_model, ll_hybrid = [], []
    weeks, goals, hybrid_mats, pure_mats = [], [], [], []
    seasons = []
    shares_model, shares_1x2 = [], []
    used_records: list[dict] = []
    used_models: list[object] = []
    used_lambdas: list[float] = []
    used = missing = 0

    for r in records:
        o = odds.get(r["match_id"])
        if not o or o.over <= 1.0 or o.under <= 1.0:
            missing += 1
            continue
        if (args.share == "market_1x2" or args.all_market) and (
                o.home is None or o.draw is None or o.away is None or
                o.home <= 1.0 or o.draw <= 1.0 or o.away <= 1.0):
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
            p_over = devig_totals(o.over, o.under)
            eh, ea = model.expected_goals(r["home"], r["away"])
            s_mod = eh / (eh + ea)

            if args.all_market:
                assert o.home is not None and o.draw is not None and o.away is not None
                ph, pd, pa = devig_1x2(o.home, o.draw, o.away)
                lh, la, rho_fit = fit_all_market(p_over, ph, pd,
                                                 rho_override=args.rho_override,
                                                 rho_start=model.rho)
                lam_total = lh + la
                m = proxy_score_matrix(lam_total, lh / lam_total, rho=rho_fit)
            elif args.share == "market_1x2":
                assert o.home is not None and o.draw is not None and o.away is not None
                ph, pd, pa = devig_1x2(o.home, o.draw, o.away)
                lam_total = market_lambda_from_totals(p_over, 2.5)
                rho = model.rho if args.rho_override is None else args.rho_override
                s_1x2 = fit_1x2_share(lam_total, ph, pa, rho=rho, s_fallback=s_mod)
                m = proxy_score_matrix(lam_total, s_1x2, rho=rho)
                shares_model.append(s_mod)
                shares_1x2.append(s_1x2)
            else:
                lam_total = market_lambda_from_totals(p_over, 2.5)
                rho = model.rho if args.rho_override is None else args.rho_override
                m = proxy_score_matrix(lam_total, s_mod, rho=rho)

        except (ValueError, KeyError):
            missing += 1
            continue

        m = draw_inflate(m, args.draw_kappa)
        m = draw_inflate(m, args.draw_kappa_high, high_only=True)
        gh, ga = r["home_goals"], r["away_goals"]
        ll_model.append(-math.log(max(r["p_score"], 1e-300)))
        ll_hybrid.append(-math.log(max(float(m[gh, ga]), 1e-300)))
        y_kick, w_kick, _ = dt.fromisoformat(r["kickoff"]).isocalendar()
        weeks.append(f"{y_kick}-W{w_kick:02d}")
        goals.append((gh, ga))
        hybrid_mats.append(m)
        pure_mats.append(model.score_matrix(r["home"], r["away"]))
        seasons.append(r["season"])
        used_records.append(r)
        used_models.append(model)
        used_lambdas.append(lam_total)
        used += 1

    ll_m = float(np.mean(ll_model))
    ll_h = float(np.mean(ll_hybrid))
    delta = ll_h - ll_m
    diff_list = (np.asarray(ll_hybrid) - np.asarray(ll_model)).tolist()
    lo, hi = week_cluster_boot(diff_list, weeks)

    # Breakdown by season
    by_season: dict[str, dict[str, object]] = {}
    seasons_arr = np.asarray(seasons)
    for sn in sorted(set(seasons)):
        mask = (seasons_arr == sn)
        ll_m_sn = float(np.mean(np.asarray(ll_model)[mask]))
        ll_h_sn = float(np.mean(np.asarray(ll_hybrid)[mask]))
        diff_sn = (np.asarray(ll_hybrid)[mask] - np.asarray(ll_model)[mask]).tolist()
        weeks_sn = np.asarray(weeks)[mask].tolist()
        lo_sn, hi_sn = week_cluster_boot(diff_sn, weeks_sn)
        by_season[str(sn)] = {
            "n": int(mask.sum()),
            "log_loss_model": ll_m_sn,
            "log_loss_hybrid": ll_h_sn,
            "delta": ll_h_sn - ll_m_sn,
            "delta_ci95": [lo_sn, hi_sn],
        }

    out: dict[str, object] = {
        "period": args.period,
        "source": args.source,
        "bookmaker": args.bookmaker or ("Avg" if args.source == "prematch" else "AvgC"),
        "share": args.share,
        "all_market": args.all_market,
        "draw_kappa": args.draw_kappa,
        "draw_kappa_high": args.draw_kappa_high,
        "rho_override": args.rho_override,
        "used": used,
        "missing_odds": missing,
        "log_loss_model": ll_m,
        "log_loss_hybrid": ll_h,
        "delta_hybrid_minus_model": delta,
        "delta_ci95": [lo, hi],
        "by_season": by_season,
        "model_cells": cell_table(pure_mats, goals),
        "hybrid_cells": cell_table(hybrid_mats, goals),
        "per_match": [
            {
                "match_id": int(rec["match_id"]),
                "week": wk,
                "season": int(sn),
                "home": rec["home"],
                "away": rec["away"],
                "home_goals": int(g[0]),
                "away_goals": int(g[1]),
                "ll_model": float(lm),
                "ll_hybrid": float(lh),
            }
            for rec, wk, sn, g, lm, lh in zip(
                used_records, weeks, seasons, goals, ll_model, ll_hybrid, strict=True
            )
        ],
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
            for name, p_val, hit in (
                    ("top1_exact", top3[0][0], (gh, ga) == top3[0][1]),
                    ("top3_multiscore", sum(x for x, _ in top3),
                     (gh, ga) in {s2 for _, s2 in top3}),
                    ("fixed_set_1_0_2_0_2_1",
                     sum(m[i, j] for i, j in fixed), (gh, ga) in fixed),
                    ("goals_range_2_4",
                     float(m[(tg >= 2) & (tg <= 4)].sum()),
                     2 <= gh + ga <= 4)):
                series[name][0].append(p_val)
                series[name][1].append(1.0 if hit else 0.0)
        out["multiscore"] = {
            name: {"n": len(ps), "mean_p": float(np.mean(ps)),
                   "observed": float(np.mean(ys))}
            for name, (ps, ys) in series.items()}

    if args.cross_fit and len(set(seasons)) >= 2:
        # Cross-season fitting for alpha and kappa
        uniq_sn = sorted(set(seasons))
        cf_res: dict[str, object] = {}

        # Alpha cross-fit (if share comparisons are available)
        if shares_model and shares_1x2:
            s_mod_arr = np.asarray(shares_model)
            s_1x2_arr = np.asarray(shares_1x2)
            alpha_cv = {}
            for tr_s, te_s in [(uniq_sn[0], uniq_sn[1]), (uniq_sn[1], uniq_sn[0])]:
                tr = (seasons_arr == tr_s)
                te = (seasons_arr == te_s)
                tr_idx = np.where(tr)[0]
                te_idx = np.where(te)[0]

                def loss_alpha(a: float, tr_mask: np.ndarray = tr,
                               idx_arr: np.ndarray = tr_idx) -> float:
                    a_val = float(a)
                    s_b = a_val * s_mod_arr[tr_mask] + (1.0 - a_val) * s_1x2_arr[tr_mask]
                    cur = []
                    for k, idx in enumerate(idx_arr):
                        r_k = used_records[idx]
                        mod_k = used_models[idx]
                        l_tot = used_lambdas[idx]
                        rho_mod = getattr(mod_k, "rho", 0.0)
                        rho_k = rho_mod if args.rho_override is None else args.rho_override
                        m_k = proxy_score_matrix(l_tot, s_b[k], rho=rho_k)
                        prob_score = max(float(m_k[r_k["home_goals"], r_k["away_goals"]]), 1e-300)
                        cur.append(-math.log(prob_score))
                    return float(np.mean(cur))

                res_a = minimize_scalar(loss_alpha, bounds=(0.0, 1.0), method="bounded")
                a_opt = float(res_a.x)

                # Evaluate on held-out test season te_s
                s_b_te_0 = s_1x2_arr[te]
                s_b_te_opt = a_opt * s_mod_arr[te] + (1.0 - a_opt) * s_1x2_arr[te]
                ll_a0 = []
                ll_aopt = []
                for k, idx in enumerate(te_idx):
                    r_k = used_records[idx]
                    mod_k = used_models[idx]
                    l_tot = used_lambdas[idx]
                    rho_mod = getattr(mod_k, "rho", 0.0)
                    rho_k = rho_mod if args.rho_override is None else args.rho_override
                    m_0 = proxy_score_matrix(l_tot, s_b_te_0[k], rho=rho_k)
                    m_opt = proxy_score_matrix(l_tot, s_b_te_opt[k], rho=rho_k)
                    gh_k, ga_k = r_k["home_goals"], r_k["away_goals"]
                    ll_a0.append(-math.log(max(float(m_0[gh_k, ga_k]), 1e-300)))
                    ll_aopt.append(-math.log(max(float(m_opt[gh_k, ga_k]), 1e-300)))

                ll_a0_arr = np.asarray(ll_a0)
                ll_aopt_arr = np.asarray(ll_aopt)
                diff_a = (ll_aopt_arr - ll_a0_arr).tolist()
                weeks_te = np.asarray(weeks)[te].tolist()
                ci_a_lo, ci_a_hi = week_cluster_boot(diff_a, weeks_te)
                alpha_cv[f"{tr_s}_to_{te_s}"] = {
                    "alpha_opt": a_opt,
                    "test_season": te_s,
                    "test_ll_alpha0": float(np.mean(ll_a0_arr)),
                    "test_ll_alpha_opt": float(np.mean(ll_aopt_arr)),
                    "delta_vs_alpha0": float(np.mean(ll_aopt_arr) - np.mean(ll_a0_arr)),
                    "ci95_vs_alpha0": [ci_a_lo, ci_a_hi],
                }
            cf_res["alpha_by_direction"] = alpha_cv

        # Kappa cross-fit
        kappa_cv = {}
        for tr_s, te_s in [(uniq_sn[0], uniq_sn[1]), (uniq_sn[1], uniq_sn[0])]:
            tr = (seasons_arr == tr_s)
            te = (seasons_arr == te_s)
            tr_idx = np.where(tr)[0]
            te_idx = np.where(te)[0]

            def loss_kappa(k: float, idx_arr: np.ndarray = tr_idx) -> float:
                k_val = float(k)
                cur = []
                for idx in idx_arr:
                    m_inf = draw_inflate(hybrid_mats[idx], k_val, high_only=True)
                    gh_k, ga_k = goals[idx]
                    cur.append(-math.log(max(float(m_inf[gh_k, ga_k]), 1e-300)))
                return float(np.mean(cur))

            res_k = minimize_scalar(loss_kappa, bounds=(0.0, 0.6), method="bounded")
            k_opt = float(res_k.x)
            ll_k0 = [
                -math.log(max(float(hybrid_mats[i][goals[i][0], goals[i][1]]), 1e-300))
                for i in te_idx
            ]
            ll_kopt = [
                -math.log(max(float(
                    draw_inflate(hybrid_mats[i], k_opt, high_only=True)[goals[i][0], goals[i][1]]
                ), 1e-300))
                for i in te_idx
            ]
            ll_k0_arr = np.asarray(ll_k0)
            ll_kopt_arr = np.asarray(ll_kopt)
            diff_k = (ll_kopt_arr - ll_k0_arr).tolist()
            weeks_te = np.asarray(weeks)[te].tolist()
            ci_k_lo, ci_k_hi = week_cluster_boot(diff_k, weeks_te)
            kappa_cv[f"{tr_s}_to_{te_s}"] = {
                "k_opt": k_opt,
                "test_season": te_s,
                "test_ll_k0": float(np.mean(ll_k0_arr)),
                "test_ll_kopt": float(np.mean(ll_kopt_arr)),
                "delta": float(np.mean(ll_kopt_arr) - np.mean(ll_k0_arr)),
                "ci95": [ci_k_lo, ci_k_hi],
            }
        cf_res["kappa_by_direction"] = kappa_cv
        out["cross_fit"] = cf_res
        cf_res["kappa_by_direction"] = kappa_cv
        out["cross_fit"] = cf_res

    Path(args.out).write_text(json.dumps(out, indent=1))
    print(f"[{args.period}/{args.source}/{args.share}] n={used} LL model {ll_m:.4f} "
          f"hybrid {ll_h:.4f} delta {delta:+.4f} CI95 [{lo:+.4f}, {hi:+.4f}]")
    big = [(c["cell"], round(float(c["z"]), 1)) for c in out["hybrid_cells"]
           if abs(float(c["z"])) > 2]
    print("hybrid cells |z|>2:", big if big else "none")


if __name__ == "__main__":
    main()
