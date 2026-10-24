"""Dixon-Coles model: fitter, fitted model, score matrix (phase 3).

SPEC §4, one league at a time:
- attack/defence per team, league-wide home advantage gamma, low-score
  correction rho (Dixon & Coles 1997);
- time decay exp(-xi * days), fractional days from kickoff to as_of;
- quasi-Poisson likelihood on the target y = w*goals + (1-w)*xG: for
  integer y it is the ordinary Poisson pmf; for mixed targets the gamma
  function replaces the factorial (lgamma). Matches without xG are
  trained on goals alone (w=1 for that match, never dropped);
- tau applies to the OBSERVED integer score (0-0/1-0/0-1/1-1) — the
  correction is about discrete scores, not about the xG-mixed target;
- rates: log lam_home = mu0 + gamma + attack_home + defence_away and
  log lam_away = mu0 + attack_away + defence_home, where mu0 is the
  league scoring level (intercept) and gamma the home advantage;
- identifiability: without constraints the likelihood is invariant under
  (attack+p, defence-p) and (defence-s, mu0+s) shifts, so the mean goals
  level can hide anywhere; BOTH sum(attack) = 0 and sum(defence) = 0 are
  enforced (n-1 free attacks, n-1 free defences) and the level lives in
  mu0. Without mu0 the second constraint would pin the away-side
  geometric mean rate to exactly 1 - the model could not fit a league
  scoring more than ~2.0 goals per game (caught in review,
  regression-tested with a high-mean synthetic).

Optimisation: scipy L-BFGS-B with an analytic gradient (checked against
finite differences in tests). rho is bounded to [-0.15, 0.15]: empirical
league-level estimates stay well inside, and the bound keeps tau positive
for realistic products of the two means. Points where any tau <= 0 are
rejected with a large objective value.

home_advantage_prior / promoted_team_prior from config are STARTING
values / starting rules only — both are marked as phase-4 tunables.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import numpy.typing as npt
import structlog
from scipy.optimize import minimize
from scipy.special import gammaln

from core.config import ModelConfig
from core.model.dataset import LoadStats, TrainingMatch, load_training_data

log = structlog.get_logger(__name__)

# empirical DC rho estimates are ~|0.05|; the bound also keeps
# 1 - lambda*mu*rho > 0 for realistic products of the two means
RHO_BOUNDS = (-0.15, 0.15)

# rejected-point value for L-BFGS-B (tau out of admissible range)
_BAD_POINT = 1e12

_FACTORIAL_CACHE: dict[int, npt.NDArray[np.float64]] = {}


def config_hash(config: ModelConfig) -> str:
    """Stable hash of the model hyperparameters (phase-4 traceability)."""
    payload = json.dumps(
        {
            "xi": config.xi,
            "weight_xg": config.weight_xg,
            "max_score": config.max_score,
            "home_advantage_prior": config.home_advantage_prior,
            "promoted_team_prior": config.promoted_team_prior,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True)
class _FitContext:
    """Pre-flattened training arrays plus per-match constants."""

    home_idx: npt.NDArray[np.intp]
    away_idx: npt.NDArray[np.intp]
    yh: npt.NDArray[np.float64]           # w*goals + (1-w)*xG (or goals)
    ya: npt.NDArray[np.float64]
    gh: npt.NDArray[np.int64]              # observed integer scores
    ga: npt.NDArray[np.int64]
    time_w: npt.NDArray[np.float64]        # exp(-xi * days), all in (0, 1]
    ll_const: npt.NDArray[np.float64]      # lgamma(yh+1) + lgamma(ya+1)
    n_teams: int


class _RejectedPoint(Exception):
    """Internal: some tau <= 0 at the current parameters."""


def _neg_loglik(
    x: npt.NDArray[np.float64], ctx: _FitContext
) -> tuple[float, npt.NDArray[np.float64]]:
    """Weighted negative quasi-log-likelihood and its analytic gradient.

    Parameter layout: [attack_0..attack_{n-2}, defence_0..defence_{n-2},
    mu0, gamma, rho]; the last attack and defence are the negative sums
    of the free ones, pinning sum(attack) = 0 and sum(defence) = 0.
    """
    n = ctx.n_teams
    att_free = x[: n - 1]
    def_free = x[n - 1 : 2 * n - 2]
    mu0 = float(x[2 * n - 2])
    gamma = float(x[2 * n - 1])
    rho = float(x[2 * n])

    attack = np.empty(n, dtype=np.float64)
    attack[: n - 1] = att_free
    attack[n - 1] = -att_free.sum()
    defence = np.empty(n, dtype=np.float64)
    defence[: n - 1] = def_free
    defence[n - 1] = -def_free.sum()

    # mu0 is the league scoring level (both sides); gamma is the home
    # advantage and enters the home rate only (Dixon & Coles 1997)
    log_lam = attack[ctx.home_idx] + defence[ctx.away_idx] + mu0 + gamma
    log_mu = attack[ctx.away_idx] + defence[ctx.home_idx] + mu0
    lam = np.exp(log_lam)
    mu = np.exp(log_mu)
    tw = ctx.time_w

    ll = float(np.sum(tw * (ctx.yh * log_lam - lam + ctx.ya * log_mu - mu)))
    ll -= float(np.sum(tw * ctx.ll_const))

    # d/dθ of (y log λ - λ) with dλ/dθ = λ  ->  residual (y - λ);
    # r_home feeds attack[home], defence[away], gamma, mu0;
    # r_away feeds attack[away], defence[home], mu0
    r_home = tw * (ctx.yh - lam)
    r_away = tw * (ctx.ya - mu)
    g_attack = (
        np.bincount(ctx.home_idx, weights=r_home, minlength=n)
        + np.bincount(ctx.away_idx, weights=r_away, minlength=n)
    )
    g_defence = (
        np.bincount(ctx.away_idx, weights=r_home, minlength=n)
        + np.bincount(ctx.home_idx, weights=r_away, minlength=n)
    )
    g_mu0 = float(r_home.sum() + r_away.sum())
    g_gamma = float(r_home.sum())
    g_rho = 0.0

    def add_tau(
        mask: npt.NDArray[np.bool_],
        tau: npt.NDArray[np.float64] | float,
        dtau_dlam: npt.NDArray[np.float64] | float | None,
        dtau_dmu: npt.NDArray[np.float64] | float | None,
        dtau_drho: npt.NDArray[np.float64] | float,
    ) -> None:
        """Accumulate log(tau) and chain its derivatives into the gradient.

        Derivative arguments are per-match arrays or scalars (broadcast);
        None means the cell's tau does not depend on that rate at all.
        """
        nonlocal ll, g_gamma, g_mu0, g_rho
        if not mask.any():
            return
        if isinstance(tau, np.ndarray):
            t = tau[mask]
        else:  # scalar tau (constant across the group)
            t = np.full(int(mask.sum()), tau, dtype=np.float64)
        if np.any(t <= 0.0):
            raise _RejectedPoint
        w = tw[mask]
        ll += float(np.sum(w * np.log(t)))
        if dtau_dlam is not None:
            dl = dtau_dlam[mask] if isinstance(dtau_dlam, np.ndarray) \
                else dtau_dlam
            coef_l = w * (dl / t) * lam[mask]
            np.add.at(g_attack, ctx.home_idx[mask], coef_l)
            np.add.at(g_defence, ctx.away_idx[mask], coef_l)
            g_gamma += float(coef_l.sum())  # gamma enters the home rate only
            g_mu0 += float(coef_l.sum())
        if dtau_dmu is not None:
            dm = dtau_dmu[mask] if isinstance(dtau_dmu, np.ndarray) \
                else dtau_dmu
            coef_m = w * (dm / t) * mu[mask]
            np.add.at(g_attack, ctx.away_idx[mask], coef_m)
            np.add.at(g_defence, ctx.home_idx[mask], coef_m)
            g_mu0 += float(coef_m.sum())
        drho = dtau_drho[mask] if isinstance(dtau_drho, np.ndarray) \
            else dtau_drho
        g_rho += float(np.sum(w * (drho / t)))

    try:
        # (gh, ga) cell -> tau and its derivatives (Dixon & Coles 1997):
        # tau(0,0)=1-lam*mu*rho, tau(0,1)=1+lam*rho, tau(1,0)=1+mu*rho,
        # tau(1,1)=1-rho
        add_tau((ctx.gh == 0) & (ctx.ga == 0), 1.0 - lam * mu * rho,
                -rho * mu, -rho * lam, -lam * mu)
        add_tau((ctx.gh == 0) & (ctx.ga == 1), 1.0 + lam * rho,
                rho, None, lam)
        add_tau((ctx.gh == 1) & (ctx.ga == 0), 1.0 + mu * rho,
                None, rho, mu)
        add_tau((ctx.gh == 1) & (ctx.ga == 1), 1.0 - rho,
                None, None, -1.0)
    except _RejectedPoint:
        return _BAD_POINT, np.zeros_like(x)

    grad = np.empty(2 * n + 1, dtype=np.float64)
    grad[: n - 1] = g_attack[: n - 1] - g_attack[n - 1]
    grad[n - 1 : 2 * n - 2] = g_defence[: n - 1] - g_defence[n - 1]
    grad[2 * n - 2] = g_mu0
    grad[2 * n - 1] = g_gamma
    grad[2 * n] = g_rho
    # the accumulated gradient is d(ll)/dx; the objective is -ll
    return -ll, -grad


def _weakest_third_prior(
    attacks: dict[str, float], defences: dict[str, float]
) -> tuple[float, float]:
    """promoted_team_prior rule 'league_weakest_third' (config default).

    Strength of a team is attack - defence (higher = stronger); the prior
    is the mean attack / mean defence over the weakest third of the teams
    fitted on real history (at least one team).
    """
    if not attacks:
        raise ValueError("no fitted teams to derive the prior from")
    ordered = sorted(attacks, key=lambda t: attacks[t] - defences[t])
    k = max(1, len(ordered) // 3)
    weakest = ordered[:k]
    return (
        sum(attacks[t] for t in weakest) / k,
        sum(defences[t] for t in weakest) / k,
    )


@dataclass
class FittedModel:
    """A fitted Dixon-Coles model for one league at one as_of moment.

    Everything phase 4 needs for traceability is kept on the object and
    round-trips through JSON: league, as_of, xi, w, match count,
    parameters (incl. prior-only teams), convergence, config hash.
    """

    league: str
    as_of: dt.datetime
    xi: float
    w: float
    n_matches: int
    attacks: dict[str, float]
    defences: dict[str, float]
    home_advantage: float          # gamma: home-rate bonus, log-goals
    rho: float
    intercept: float               # mu0: league scoring level, log-goals
    converged: bool
    config_hash: str
    prior_teams: frozenset[str] = frozenset()
    iterations: int = 0
    loglik: float = 0.0
    max_score: int = 10

    def _rates(self, home: str, away: str) -> tuple[float, float]:
        if home not in self.attacks or away not in self.attacks:
            known = sorted(self.attacks)
            raise KeyError(
                f"team not in fitted model for {self.league!r}: "
                f"{home!r} / {away!r}; known teams: {known}"
            )
        lam = math.exp(self.intercept + self.home_advantage
                       + self.attacks[home] + self.defences[away])
        mu = math.exp(self.intercept
                      + self.attacks[away] + self.defences[home])
        return lam, mu

    def score_matrix(self, home: str, away: str) -> npt.NDArray[np.float64]:
        """(max_score+1)x(max_score+1) score probabilities, sum = 1.

        Independent Poissons with tau applied to the four low-score cells;
        the truncated tail is folded back in by renormalisation.
        """
        lam, mu = self._rates(home, away)
        k = np.arange(self.max_score + 1, dtype=np.float64)
        ph = np.exp(-lam + k * math.log(lam)) / _factorials(self.max_score)
        pa = np.exp(-mu + k * math.log(mu)) / _factorials(self.max_score)
        m = np.outer(ph, pa)
        factors = {
            (0, 0): 1.0 - lam * mu * self.rho,
            (0, 1): 1.0 + lam * self.rho,
            (1, 0): 1.0 + mu * self.rho,
            (1, 1): 1.0 - self.rho,
        }
        for (h, a), f in factors.items():
            if f <= 0.0:
                raise ValueError(
                    f"tau({h},{a}) = {f} <= 0 for {home} v {away} "
                    f"(lam={lam:.3f}, mu={mu:.3f}, rho={self.rho:.3f}); "
                    "parameters out of the admissible Dixon-Coles range"
                )
            m[h, a] *= f
        m /= m.sum()
        return m

    def expected_goals(self, home: str, away: str) -> tuple[float, float]:
        """Expected home/away goals under the (renormalised) matrix."""
        m = self.score_matrix(home, away)
        goals = np.arange(self.max_score + 1, dtype=np.float64)
        return float(goals @ m.sum(axis=1)), float(goals @ m.sum(axis=0))

    # --- serialization -------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "league": self.league,
            "as_of": self.as_of.isoformat(),
            "xi": self.xi,
            "w": self.w,
            "n_matches": self.n_matches,
            "teams": {
                t: {
                    "attack": self.attacks[t],
                    "defence": self.defences[t],
                    "prior": t in self.prior_teams,
                }
                for t in sorted(self.attacks)
            },
            "home_advantage": self.home_advantage,
            "rho": self.rho,
            "intercept": self.intercept,
            "converged": self.converged,
            "config_hash": self.config_hash,
            "iterations": self.iterations,
            "loglik": self.loglik,
            "max_score": self.max_score,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FittedModel:
        attacks = {t: v["attack"] for t, v in data["teams"].items()}
        defences = {t: v["defence"] for t, v in data["teams"].items()}
        priors = frozenset(t for t, v in data["teams"].items() if v["prior"])
        return cls(
            league=data["league"],
            as_of=dt.datetime.fromisoformat(data["as_of"]),
            xi=data["xi"],
            w=data["w"],
            n_matches=data["n_matches"],
            attacks=attacks,
            defences=defences,
            home_advantage=data["home_advantage"],
            rho=data["rho"],
            intercept=data.get("intercept", 0.0),
            converged=data["converged"],
            config_hash=data["config_hash"],
            prior_teams=priors,
            iterations=data.get("iterations", 0),
            loglik=data.get("loglik", 0.0),
            max_score=data.get("max_score", 10),
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> FittedModel:
        return cls.from_dict(json.loads(text))


def _factorials(max_score: int) -> npt.NDArray[np.float64]:
    """0!, 1!, ..., max_score! (cached per max_score)."""
    cached = _FACTORIAL_CACHE.get(max_score)
    if cached is None:
        cached = np.array(
            [math.factorial(k) for k in range(max_score + 1)],
            dtype=np.float64,
        )
        _FACTORIAL_CACHE[max_score] = cached
    return cached


def fit_dixon_coles(
    matches: Sequence[TrainingMatch],
    *,
    league: str,
    as_of: dt.datetime,
    team_universe: Collection[str],
    config: ModelConfig,
) -> FittedModel:
    """Fit one league from raw training matches (pure, no DB access).

    Synthetic-data tests call this directly with generated matches.
    """
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    if not 0.0 <= config.weight_xg <= 1.0:
        raise ValueError(f"weight_xg must be in [0, 1], got {config.weight_xg}")
    if config.xi < 0.0:
        raise ValueError(f"xi must be >= 0, got {config.xi}")
    if not matches:
        raise ValueError(f"no training matches for league {league!r}")

    late = [m.match_id for m in matches
            if m.kickoff >= as_of or m.kickoff.tzinfo is None]
    if late:
        raise ValueError(
            f"training matches with kickoff >= as_of (or naive datetime) "
            f"are not allowed: match ids {sorted(late)[:5]}"
        )

    w = config.weight_xg
    teams = sorted({m.home for m in matches} | {m.away for m in matches})
    idx = {t: i for i, t in enumerate(teams)}
    n = len(teams)
    if n < 2:
        raise ValueError("need at least two teams to fit")

    home_idx = np.array([idx[m.home] for m in matches], dtype=np.intp)
    away_idx = np.array([idx[m.away] for m in matches], dtype=np.intp)
    gh = np.array([m.home_goals for m in matches], dtype=np.int64)
    ga = np.array([m.away_goals for m in matches], dtype=np.int64)
    if np.any(gh < 0) or np.any(ga < 0):
        raise ValueError("negative goals in training data")

    yh = np.array(
        [m.home_goals if m.home_xg is None
         else w * m.home_goals + (1.0 - w) * m.home_xg
         for m in matches], dtype=np.float64)
    ya = np.array(
        [m.away_goals if m.away_xg is None
         else w * m.away_goals + (1.0 - w) * m.away_xg
         for m in matches], dtype=np.float64)

    days = np.array(
        [(as_of - m.kickoff).total_seconds() / 86400.0 for m in matches],
        dtype=np.float64)
    time_w = np.exp(-config.xi * days)

    ctx = _FitContext(
        home_idx=home_idx, away_idx=away_idx, yh=yh, ya=ya, gh=gh, ga=ga,
        time_w=time_w,
        ll_const=_gammaln(yh) + _gammaln(ya),
        n_teams=n,
    )

    # start from closed-form-ish rates: log per-team scored/conceded
    # averages in the target space, centred (the sum constraints need
    # sum=0). Puts L-BFGS-B near the optimum instead of at zeros, which
    # matters in the flat attack/defence valleys.
    scored = np.zeros(n)
    conceded = np.zeros(n)
    games = np.zeros(n)
    for k in range(len(matches)):
        scored[home_idx[k]] += yh[k]
        conceded[home_idx[k]] += ya[k]
        scored[away_idx[k]] += ya[k]
        conceded[away_idx[k]] += yh[k]
        games[home_idx[k]] += 1.0
        games[away_idx[k]] += 1.0
    league_rate = 0.5 * (yh.sum() + ya.sum()) / len(matches)
    att0 = np.log((scored + 0.5) / games) - math.log(league_rate + 0.5)
    def0 = np.log((conceded + 0.5) / games) - math.log(league_rate + 0.5)
    att0 -= att0.mean()
    def0 -= def0.mean()

    x0 = np.zeros(2 * n + 1, dtype=np.float64)
    x0[: n - 1] = att0[: n - 1]     # last attack/defence are derived
    x0[n - 1 : 2 * n - 2] = def0[: n - 1]
    x0[2 * n - 2] = math.log(league_rate + 0.5)   # mu0 start: level
    x0[2 * n - 1] = config.home_advantage_prior   # gamma start; rho starts 0
    bounds = [(None, None)] * (2 * n) + [RHO_BOUNDS]

    res = minimize(
        _neg_loglik, x0, args=(ctx,), method="L-BFGS-B", jac=True,
        bounds=bounds,
        options={"maxiter": 5000, "ftol": 1e-13, "gtol": 1e-8, "maxcor": 50},
    )
    # L-BFGS-B can report success while sitting on the reject plateau;
    # a gradient of exactly zero with objective _BAD_POINT means it never
    # left the start (pathological data) — surface it as non-convergence.
    converged = bool(res.success) and float(res.fun) < _BAD_POINT / 2.0
    if not converged:
        log.warning("dc_fit_did_not_converge", league=league,
                    status=res.status, message=res.message)

    attack = np.empty(n, dtype=np.float64)
    attack[: n - 1] = res.x[: n - 1]
    attack[n - 1] = -res.x[: n - 1].sum()
    defence = np.empty(n, dtype=np.float64)
    defence[: n - 1] = res.x[n - 1 : 2 * n - 2]
    defence[n - 1] = -res.x[n - 1 : 2 * n - 2].sum()
    attacks = {t: float(attack[i]) for i, t in enumerate(teams)}
    defences = {t: float(defence[i]) for i, t in enumerate(teams)}

    prior_teams: frozenset[str] = frozenset()
    universe = set(team_universe) - set(teams)
    if universe:
        pa, pd = _weakest_third_prior(attacks, defences)
        for t in universe:
            attacks[t] = pa
            defences[t] = pd
        prior_teams = frozenset(universe)
        log.info("dc_promoted_priors_assigned", league=league,
                 n_prior=len(universe), attack=round(pa, 4),
                 defence=round(pd, 4))

    return FittedModel(
        league=league,
        as_of=as_of,
        xi=config.xi,
        w=w,
        n_matches=len(matches),
        attacks=attacks,
        defences=defences,
        home_advantage=float(res.x[2 * n - 1]),
        rho=float(res.x[2 * n]),
        intercept=float(res.x[2 * n - 2]),
        converged=converged,
        config_hash=config_hash(config),
        prior_teams=prior_teams,
        iterations=int(res.nit),
        loglik=-float(res.fun),
        max_score=config.max_score,
    )


def _gammaln(y: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
    """lgamma(y + 1) — the quasi-Poisson normaliser for non-integer y."""
    return np.asarray(gammaln(y + 1.0), dtype=np.float64)


def fit_league(
    db_url: str,
    league: str,
    as_of: dt.datetime,
    config: ModelConfig,
    excluded_match_ids: Collection[int] = frozenset(),
) -> tuple[FittedModel, LoadStats]:
    """Load training data from the DB and fit one league.

    ``excluded_match_ids`` carries the B5 quarantine (parsed from the
    ingest report by ``dataset.quarantined_match_ids``). Returns the
    fitted model plus the loader stats.
    """
    data = load_training_data(db_url, league, as_of, excluded_match_ids)
    model = fit_dixon_coles(
        data.matches, league=league, as_of=as_of,
        team_universe=data.team_universe, config=config,
    )
    return model, data.stats
