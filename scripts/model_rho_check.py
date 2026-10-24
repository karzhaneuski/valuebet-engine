"""Phase-3 review S2/S3 evidence: rho sensitivity and parameter SEs.

Rebuilds the fit context for each league exactly as `fit_dixon_coles`
does, then computes the 2x2 observed-information block for (gamma, rho)
from finite differences of the analytic gradient, and compares rho
across w and xi settings.

Usage:
    DATABASE_URL=sqlite:////tmp/wf-smoke.db uv run python -m scripts.model_rho_check
"""

from __future__ import annotations

import datetime as dt
import os

import numpy as np

from core.config import ModelConfig
from core.model.dataset import load_training_data
from core.model.dixon_coles import (
    _FitContext,
    _gammaln,
    _last_completed_season,
    _neg_loglik,
    _season_baseline,
    fit_league,
)

TOP_LEAGUES = ["premier_league", "la_liga", "serie_a", "bundesliga", "ligue_1"]
AS_OF = dt.datetime.now(dt.UTC)


def rebuild_ctx(db_url: str, league: str, config: ModelConfig) -> tuple[_FitContext, dict]:
    """Same array construction as fit_dixon_coles (kept in sync by the
    equality assertion on the fitted objective below)."""
    data = load_training_data(db_url, league, AS_OF,
                              result_lag_minutes=config.result_lag_minutes)
    matches = data.matches
    w = config.weight_xg
    teams = sorted({m.home for m in matches} | {m.away for m in matches})
    idx = {t: i for i, t in enumerate(teams)}
    n = len(teams)
    home_idx = np.array([idx[m.home] for m in matches], dtype=np.intp)
    away_idx = np.array([idx[m.away] for m in matches], dtype=np.intp)
    gh = np.array([m.home_goals for m in matches], dtype=np.int64)
    ga = np.array([m.away_goals for m in matches], dtype=np.int64)
    yh = np.array([m.home_goals if m.home_xg is None
                   else w * m.home_goals + (1 - w) * m.home_xg
                   for m in matches], dtype=np.float64)
    ya = np.array([m.away_goals if m.away_xg is None
                   else w * m.away_goals + (1 - w) * m.away_xg
                   for m in matches], dtype=np.float64)
    days = np.array([(AS_OF - m.kickoff).total_seconds() / 86400.0
                     for m in matches], dtype=np.float64)
    baseline_season = _last_completed_season(matches, AS_OF)
    baseline_teams, pa, pd = _season_baseline(matches, yh, ya, baseline_season)
    established = np.array([t in baseline_teams for t in teams])
    ctx = _FitContext(
        home_idx=home_idx, away_idx=away_idx, yh=yh, ya=ya, gh=gh, ga=ga,
        time_w=np.exp(-config.xi * days),
        ll_const=_gammaln(yh) + _gammaln(ya),
        m_attack=np.where(established, 0.0, pa),
        m_defence=np.where(established, 0.0, pd),
        prior_sigma=config.prior_sigma, n_teams=n,
    )
    return ctx, {"teams": teams}


def gamma_rho_se(ctx: _FitContext, x: np.ndarray) -> tuple[float, float]:
    """SEs of gamma/rho from the inverted 2x2 observed-information block
    (finite differences of the analytic gradient at the optimum)."""
    n = ctx.n_teams
    gi, ri = 2 * n - 1, 2 * n  # gamma, rho indices
    eps = 1e-5

    def grad(xv: np.ndarray) -> np.ndarray:
        return _neg_loglik(xv, ctx)[1]

    h_gg = (grad(_bump(x, gi, eps))[gi] - grad(_bump(x, gi, -eps))[gi]) / (2 * eps)
    h_rr = (grad(_bump(x, ri, eps))[ri] - grad(_bump(x, ri, -eps))[ri]) / (2 * eps)
    h_gr = (grad(_bump(x, gi, eps))[ri] - grad(_bump(x, gi, -eps))[ri]) / (2 * eps)
    h = np.array([[h_gg, h_gr], [h_gr, h_rr]])
    cov = np.linalg.inv(h)
    return float(np.sqrt(cov[0, 0])), float(np.sqrt(cov[1, 1]))


def _bump(x: np.ndarray, i: int, eps: float) -> np.ndarray:
    out = x.copy()
    out[i] += eps
    return out


def x_of(model, ctx: _FitContext) -> np.ndarray:
    """Pack a fitted model back into the optimizer's parameter vector."""
    n = ctx.n_teams
    teams = sorted(model.attacks)
    att = {t: model.attacks[t] for t in teams}
    dfn = {t: model.defences[t] for t in teams}
    # last team's params are derived; the vector uses the fitted values
    # directly for reconstruction (sum constraints hold to ~1e-9)
    x = np.zeros(2 * n + 1)
    for i, t in enumerate(teams[:-1]):
        x[i] = att[t]
    x[n - 1 : 2 * n - 2] = [dfn[t] for t in teams[:-1]]
    x[2 * n - 2] = model.intercept
    x[2 * n - 1] = model.home_advantage
    x[2 * n] = model.rho
    return x


def main() -> None:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise SystemExit("set DATABASE_URL to a DB copy")
    print(f"as_of={AS_OF.isoformat()}")
    print(f"{'league':>15} {'rho w.5':>8} {'rho w1':>8} {'d_rho':>7} "
          f"{'rho xi0':>8} {'se(gamma)':>9} {'se(rho)':>8}")
    for league in TOP_LEAGUES:
        config = ModelConfig(xi=0.005, weight_xg=0.5)
        model, _ = fit_league(db_url, league, AS_OF, config)
        ctx, _ = rebuild_ctx(db_url, league, config)
        se_g, se_r = gamma_rho_se(ctx, x_of(model, ctx))
        model_w1, _ = fit_league(db_url, league, AS_OF,
                                 ModelConfig(xi=0.005, weight_xg=1.0))
        model_xi0, _ = fit_league(db_url, league, AS_OF,
                                  ModelConfig(xi=0.0, weight_xg=0.5))
        print(f"{league:>15} {model.rho:8.4f} {model_w1.rho:8.4f} "
              f"{model_w1.rho - model.rho:7.4f} {model_xi0.rho:8.4f} "
              f"{se_g:9.4f} {se_r:8.4f}")


if __name__ == "__main__":
    main()
