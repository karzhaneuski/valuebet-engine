"""Dixon-Coles fitter: gradient, synthetic recovery, matrix, priors, JSON.

The synthetic tests generate matches FROM a Dixon-Coles model with known
parameters (rho != 0 included) and require the fitter to recover them
with reasonable precision — the phase-3 review checklist item.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pytest
from numpy.testing import assert_allclose

from core.config import ModelConfig
from core.model.dataset import TrainingMatch
from core.model.dixon_coles import (
    RHO_BOUNDS,
    FittedModel,
    _FitContext,
    _gammaln,
    _neg_loglik,
    config_hash,
    fit_dixon_coles,
)

AS_OF = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)

TEAMS = ["Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot", "Golf", "Hotel"]
TRUE_ATTACKS = {
    "Alpha": 0.45, "Bravo": 0.25, "Charlie": 0.10, "Delta": -0.05,
    "Echo": -0.15, "Foxtrot": -0.20, "Golf": -0.30, "Hotel": -0.10,
}
TRUE_DEFENCES = {
    "Alpha": -0.40, "Bravo": -0.25, "Charlie": -0.10, "Delta": 0.0,
    "Echo": 0.10, "Foxtrot": 0.20, "Golf": 0.35, "Hotel": 0.10,
}
TRUE_GAMMA = 0.30
TRUE_RHO = -0.06


def true_model(
    intercept: float = 0.0, gamma: float = TRUE_GAMMA, rho: float = TRUE_RHO,
) -> FittedModel:
    return FittedModel(
        league="synthetic", as_of=AS_OF, xi=0.0, w=0.5, n_matches=0,
        attacks=dict(TRUE_ATTACKS), defences=dict(TRUE_DEFENCES),
        home_advantage=gamma, rho=rho, intercept=intercept,
        converged=True, config_hash="synthetic",
    )


def generate_matches(
    rounds: int = 60, seed: int = 42, with_xg: bool = True,
    drop_xg_every: int = 0, intercept: float = 0.0,
    gamma: float = TRUE_GAMMA, rho: float = TRUE_RHO,
) -> list[TrainingMatch]:
    """Sample matches from the true DC model; xG ~ Poisson at the rates.

    ``drop_xg_every`` simulates matches without understat xG (goals-only,
    trained with w=1 for those matches).
    """
    rng = np.random.default_rng(seed)
    model = true_model(intercept=intercept, gamma=gamma, rho=rho)
    matches: list[TrainingMatch] = []
    mid = 0
    for _ in range(rounds):
        for i, home in enumerate(TEAMS):
            for away in TEAMS[i + 1:]:
                for h, a in ((home, away), (away, home)):
                    m = model.score_matrix(h, a)
                    k = int(rng.choice(m.size, p=m.ravel()))
                    gh, ga = divmod(k, m.shape[1])
                    lam, mu = model._rates(h, a)
                    mid += 1
                    xg_h = xg_a = None
                    if with_xg and not (drop_xg_every and mid % drop_xg_every == 0):
                        xg_h = float(rng.poisson(lam))
                        xg_a = float(rng.poisson(mu))
                    matches.append(TrainingMatch(
                        match_id=mid,
                        kickoff=AS_OF - dt.timedelta(days=float(rng.uniform(5, 400))),
                        home=h, away=a,
                        home_goals=gh, away_goals=ga,
                        home_xg=xg_h, away_xg=xg_a,
                    ))
    return matches


# --- gradient ---------------------------------------------------------------


def test_neg_loglik_gradient_matches_finite_differences() -> None:
    rng = np.random.default_rng(7)
    n_teams, n_matches = 6, 240
    home_idx = rng.integers(0, n_teams, n_matches)
    away_idx = np.array(
        [(h + 1 + rng.integers(0, n_teams - 1)) % n_teams for h in home_idx])
    yh = rng.gamma(3.0, 0.5, n_matches)   # non-integer quasi targets
    ya = rng.gamma(2.5, 0.5, n_matches)
    gh = rng.integers(0, 4, n_matches)
    ga = rng.integers(0, 4, n_matches)
    tw = np.exp(-0.005 * rng.uniform(0.0, 400.0, n_matches))
    ctx = _FitContext(
        home_idx=home_idx, away_idx=away_idx, yh=yh, ya=ya, gh=gh, ga=ga,
        time_w=tw, ll_const=_gammaln(yh) + _gammaln(ya), n_teams=n_teams,
    )
    eps = 1e-6
    for rho in (0.0, 0.05, -0.08):
        x = rng.normal(0.0, 0.1, 2 * n_teams + 1)
        x[-1] = rho
        _, g = _neg_loglik(x, ctx)
        g_fd = np.zeros_like(x)
        for i in range(x.size):
            xp, xm = x.copy(), x.copy()
            xp[i] += eps
            xm[i] -= eps
            g_fd[i] = (_neg_loglik(xp, ctx)[0] - _neg_loglik(xm, ctx)[0]) / (2 * eps)
        assert_allclose(g, g_fd, rtol=1e-5, atol=1e-6)


# --- synthetic recovery -----------------------------------------------------


def test_recovers_parameters_from_synthetic_data() -> None:
    matches = generate_matches(rounds=60, seed=42)
    config = ModelConfig(xi=0.0, weight_xg=0.5)
    fitted = fit_dixon_coles(
        matches, league="synthetic", as_of=AS_OF,
        team_universe=set(TEAMS), config=config,
    )
    assert fitted.converged
    assert fitted.n_matches == len(matches)
    # identifiability constraints hold exactly
    assert abs(sum(fitted.attacks.values())) < 1e-9
    assert abs(sum(fitted.defences.values())) < 1e-9
    # teams/defences recovered to ~3 standard errors
    for t in TEAMS:
        assert abs(fitted.attacks[t] - TRUE_ATTACKS[t]) < 0.15, t
        assert abs(fitted.defences[t] - TRUE_DEFENCES[t]) < 0.15, t
    assert abs(fitted.home_advantage - TRUE_GAMMA) < 0.10
    assert abs(fitted.rho - TRUE_RHO) < 0.10
    # strongest attack is identified (ordering of near-ties may flip)
    top = sorted(TEAMS, key=lambda t: -fitted.attacks[t])[:2]
    assert "Alpha" in top and "Bravo" in top
    weakest = sorted(TEAMS, key=lambda t: fitted.attacks[t])[:1]
    assert weakest == ["Golf"]


def test_high_mean_league_recovers_scoring_level() -> None:
    """Regression (review, step 1): without mu0 the double sum-to-zero
    constraint pins the away-side geometric mean rate to exactly 1 and a
    high-scoring league cannot be fitted."""
    intercept, gamma, rho = 0.40, 0.24, -0.05   # away ~1.5, home ~1.9
    matches = generate_matches(rounds=60, seed=42, intercept=intercept,
                               gamma=gamma, rho=rho)
    fitted = fit_dixon_coles(
        matches, league="synthetic", as_of=AS_OF,
        team_universe=set(TEAMS), config=ModelConfig(xi=0.0, weight_xg=0.5),
    )
    assert fitted.converged
    assert abs(sum(fitted.attacks.values())) < 1e-9
    assert abs(sum(fitted.defences.values())) < 1e-9
    assert fitted.intercept == pytest.approx(intercept, abs=0.1)
    assert fitted.home_advantage == pytest.approx(gamma, abs=0.1)
    # the mean scoring level itself is recovered, not pinned to ~1
    truth = true_model(intercept=intercept, gamma=gamma, rho=rho)
    pairs = [(h, a) for i, h in enumerate(TEAMS) for a in TEAMS[i + 1:]]
    for side in (0, 1):
        true_level = np.mean([
            truth.expected_goals(h, a)[side] + truth.expected_goals(a, h)[side]
            for h, a in pairs
        ])
        fit_level = np.mean([
            fitted.expected_goals(h, a)[side] + fitted.expected_goals(a, h)[side]
            for h, a in pairs
        ])
        assert fit_level == pytest.approx(true_level, abs=0.1), side
    away_level = np.mean([
        fitted.expected_goals(h, a)[1] + fitted.expected_goals(a, h)[1]
        for h, a in pairs
    ])
    assert away_level > 1.3   # away level e^0.40 ~ 1.49, was pinned to 1 before


def test_recovery_with_goals_only_matches_mixed_in() -> None:
    # every 5th match has no xG: trained on goals alone (w=1), never dropped
    matches = generate_matches(rounds=60, seed=7, drop_xg_every=5)
    assert any(m.home_xg is None for m in matches)
    config = ModelConfig(xi=0.0, weight_xg=0.5)
    fitted = fit_dixon_coles(
        matches, league="synthetic", as_of=AS_OF,
        team_universe=set(TEAMS), config=config,
    )
    assert fitted.converged
    assert fitted.n_matches == len(matches)
    for t in TEAMS:
        assert abs(fitted.attacks[t] - TRUE_ATTACKS[t]) < 0.15, t
    assert abs(fitted.home_advantage - TRUE_GAMMA) < 0.10


def test_time_decay_weights_older_matches_less() -> None:
    # identical matches, different ages: with xi>0 the recent copy must
    # dominate the fitted rates towards its own outcome
    config = ModelConfig(xi=0.02, weight_xg=1.0)
    old = TrainingMatch(
        match_id=1, kickoff=AS_OF - dt.timedelta(days=300),
        home="Alpha", away="Golf", home_goals=0, away_goals=3,
        home_xg=None, away_xg=None,
    )
    recent = TrainingMatch(
        match_id=2, kickoff=AS_OF - dt.timedelta(days=1),
        home="Alpha", away="Golf", home_goals=5, away_goals=0,
        home_xg=None, away_xg=None,
    )
    fitted = fit_dixon_coles(
        [old, recent], league="synthetic", as_of=AS_OF,
        team_universe=set(TEAMS), config=config,
    )
    lam, mu = fitted._rates("Alpha", "Golf")
    # the 1-day-old 5:0 outweighs the 300-day-old 0:3 (weight ratio e^{5.98})
    assert lam > mu


def test_fit_rejects_matches_at_or_after_as_of() -> None:
    bad = TrainingMatch(
        match_id=1, kickoff=AS_OF, home="Alpha", away="Bravo",
        home_goals=1, away_goals=1, home_xg=None, away_xg=None,
    )
    with pytest.raises(ValueError, match="kickoff >= as_of"):
        fit_dixon_coles(
            [bad], league="synthetic", as_of=AS_OF,
            team_universe=set(TEAMS), config=ModelConfig(),
        )


def test_fit_is_deterministic_bit_for_bit() -> None:
    matches = generate_matches(rounds=10, seed=3)
    config = ModelConfig(xi=0.005, weight_xg=0.5)
    a = fit_dixon_coles(matches, league="synthetic", as_of=AS_OF,
                        team_universe=set(TEAMS), config=config)
    b = fit_dixon_coles(matches, league="synthetic", as_of=AS_OF,
                        team_universe=set(TEAMS), config=config)
    assert a.to_json() == b.to_json()


# --- promoted-team prior ----------------------------------------------------


def test_promoted_teams_get_weakest_third_prior() -> None:
    matches = generate_matches(rounds=20, seed=11)
    fitted = fit_dixon_coles(
        matches, league="synthetic", as_of=AS_OF,
        team_universe=set(TEAMS) | {"Promoted FC"}, config=ModelConfig(xi=0.0),
    )
    assert "Promoted FC" in fitted.attacks
    assert fitted.prior_teams == frozenset({"Promoted FC"})
    strength = sorted(TEAMS, key=lambda t: fitted.attacks[t] - fitted.defences[t])
    weakest_third = strength[: max(1, len(TEAMS) // 3)]
    prior_att = sum(fitted.attacks[t] for t in weakest_third) / len(weakest_third)
    prior_def = sum(fitted.defences[t] for t in weakest_third) / len(weakest_third)
    assert fitted.attacks["Promoted FC"] == pytest.approx(prior_att, abs=1e-12)
    assert fitted.defences["Promoted FC"] == pytest.approx(prior_def, abs=1e-12)
    # prior teams are usable in predictions
    m = fitted.score_matrix("Promoted FC", "Alpha")
    assert m.shape == (11, 11)


def test_unknown_team_raises_keyerror() -> None:
    fitted = fit_dixon_coles(
        generate_matches(rounds=5, seed=1), league="synthetic", as_of=AS_OF,
        team_universe=set(TEAMS), config=ModelConfig(xi=0.0),
    )
    with pytest.raises(KeyError, match="not in fitted model"):
        fitted.score_matrix("Real Madrid", "Alpha")


# --- score matrix -----------------------------------------------------------


def test_score_matrix_shape_sum_and_rates() -> None:
    model = true_model()
    m = model.score_matrix("Alpha", "Hotel")
    assert m.shape == (11, 11)
    assert np.isclose(m.sum(), 1.0, atol=1e-9)
    assert (m >= 0).all()
    # tau preserves the conditional means; truncation at 10 costs ~1e-4
    # relative mass in the far tail (folded back by renormalisation)
    k = np.arange(11, dtype=float)
    eh, ea = k @ m.sum(axis=1), k @ m.sum(axis=0)
    lam, mu = model._rates("Alpha", "Hotel")
    assert eh == pytest.approx(lam, abs=1e-3)
    assert ea == pytest.approx(mu, abs=1e-3)


def test_tau_moves_low_score_cells_in_the_right_direction() -> None:
    base = true_model(rho=0.0)
    rho_pos = true_model(rho=0.08)
    rho_neg = true_model(rho=-0.08)
    m0, mp, mn = (m.score_matrix("Bravo", "Echo") for m in (base, rho_pos, rho_neg))
    # rho > 0: 0-0 and 1-1 lose mass, 1-0 and 0-1 gain it; rho < 0 mirrored
    assert mp[0, 0] < m0[0, 0] < mn[0, 0]
    assert mp[1, 1] < m0[1, 1] < mn[1, 1]
    assert mp[1, 0] > m0[1, 0] > mn[1, 0]
    assert mp[0, 1] > m0[0, 1] > mn[0, 1]
    for m in (mp, mn):
        assert np.isclose(m.sum(), 1.0, atol=1e-9)


def test_rho_stays_inside_bounds_after_fit() -> None:
    fitted = fit_dixon_coles(
        generate_matches(rounds=20, seed=5), league="synthetic", as_of=AS_OF,
        team_universe=set(TEAMS), config=ModelConfig(xi=0.0),
    )
    assert RHO_BOUNDS[0] <= fitted.rho <= RHO_BOUNDS[1]


# --- serialization ----------------------------------------------------------


def test_json_round_trip_preserves_predictions_bit_for_bit() -> None:
    matches = generate_matches(rounds=15, seed=9)
    fitted = fit_dixon_coles(
        matches, league="synthetic", as_of=AS_OF,
        team_universe=set(TEAMS) | {"Promoted FC"}, config=ModelConfig(xi=0.005),
    )
    restored = FittedModel.from_json(fitted.to_json())
    assert restored.league == fitted.league
    assert restored.as_of == fitted.as_of
    assert restored.xi == fitted.xi and restored.w == fitted.w
    assert restored.n_matches == fitted.n_matches
    assert restored.converged is fitted.converged
    assert restored.config_hash == fitted.config_hash
    assert restored.prior_teams == fitted.prior_teams
    for t in TEAMS:
        assert restored.attacks[t] == fitted.attacks[t]
        assert restored.defences[t] == fitted.defences[t]
    a = fitted.score_matrix("Alpha", "Golf")
    b = restored.score_matrix("Alpha", "Golf")
    assert np.array_equal(a, b)


def test_config_hash_is_stable_and_sensitive() -> None:
    base = ModelConfig()
    assert config_hash(base) == config_hash(ModelConfig())
    assert config_hash(base) != config_hash(ModelConfig(xi=0.006))
    assert config_hash(base) != config_hash(ModelConfig(weight_xg=0.6))
