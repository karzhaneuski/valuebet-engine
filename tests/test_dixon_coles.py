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
    _last_completed_season,
    _neg_loglik,
    _season_baseline,
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
    season: int = 2021, xg_gamma_k: float = 0.0,
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
                        if xg_gamma_k > 0.0:
                            xg_h = lam * float(
                                rng.gamma(xg_gamma_k, 1.0 / xg_gamma_k))
                            xg_a = mu * float(
                                rng.gamma(xg_gamma_k, 1.0 / xg_gamma_k))
                        else:
                            xg_h = float(rng.poisson(lam))
                            xg_a = float(rng.poisson(mu))
                    matches.append(TrainingMatch(
                        match_id=mid,
                        kickoff=AS_OF - dt.timedelta(days=float(rng.uniform(5, 400))),
                        season=season,
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
    rng_m = np.random.default_rng(99)
    ctx = _FitContext(
        home_idx=home_idx, away_idx=away_idx, yh=yh, ya=ya, gh=gh, ga=ga,
        time_w=tw, ll_const=_gammaln(yh) + _gammaln(ya), n_teams=n_teams,
        m_attack=rng_m.normal(-0.1, 0.1, n_teams),
        m_defence=rng_m.normal(0.1, 0.1, n_teams),
        prior_sigma=0.5,   # penalty ON: FD must verify the prior term too
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


RHO_SEEDS = (42, 1, 2, 3, 4)


def _fit_rho_mean(rounds: int, xi: float) -> float:
    """Mean fitted rho over RHO_SEEDS; truth -0.10, Gamma-shaped xG."""
    rhos = []
    for seed in RHO_SEEDS:
        matches = generate_matches(rounds=rounds, seed=seed,
                                   rho=-0.10, xg_gamma_k=10.0)
        fitted = fit_dixon_coles(
            matches, league="synthetic", as_of=AS_OF,
            team_universe=set(TEAMS),
            config=ModelConfig(xi=xi, weight_xg=0.5),
        )
        assert fitted.converged
        rhos.append(fitted.rho)
    return float(np.mean(rhos))


def test_recovers_rho_minus_0_10_with_noninteger_xg() -> None:
    """Review S4: 200 rounds (11 200 matches), Gamma-shaped non-integer
    xG, mean over 5 seeds - |error| < 0.03 with the correct sign."""
    mean_rho = _fit_rho_mean(rounds=200, xi=0.0)
    assert mean_rho < 0.0, mean_rho
    assert abs(mean_rho - (-0.10)) < 0.03, mean_rho


def test_recovers_rho_with_time_decay_active() -> None:
    """Same recovery with xi=0.005 (kickoffs spread over ~400 days): the
    weighted quasi-likelihood stays consistent for rho."""
    mean_rho = _fit_rho_mean(rounds=200, xi=0.005)
    assert mean_rho < 0.0, mean_rho
    assert abs(mean_rho - (-0.10)) < 0.04, mean_rho   # slightly noisier


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
        match_id=1, kickoff=AS_OF - dt.timedelta(days=300), season=2021,
        home="Alpha", away="Golf", home_goals=0, away_goals=3,
        home_xg=None, away_xg=None,
    )
    recent = TrainingMatch(
        match_id=2, kickoff=AS_OF - dt.timedelta(days=1), season=2021,
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
        match_id=1, kickoff=AS_OF - dt.timedelta(minutes=60), season=2021,
        home="Alpha", away="Bravo",
        home_goals=1, away_goals=1, home_xg=None, away_xg=None,
    )
    with pytest.raises(ValueError, match="result cannot be known"):
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


def _yh_ya(matches: list[TrainingMatch], w: float) -> tuple[np.ndarray, np.ndarray]:
    yh = np.array([m.home_goals if m.home_xg is None
                   else w * m.home_goals + (1 - w) * m.home_xg
                   for m in matches])
    ya = np.array([m.away_goals if m.away_xg is None
                   else w * m.away_goals + (1 - w) * m.away_xg
                   for m in matches])
    return yh, ya


def test_prior_teams_relax_to_their_prior_means() -> None:
    matches = generate_matches(rounds=20, seed=11, season=2020)
    # push the season into the quiet past so it counts as completed
    matches = [TrainingMatch(**{**m.__dict__,
                                "kickoff": AS_OF - dt.timedelta(days=200)})
               for m in matches]
    config = ModelConfig(xi=0.0)
    fitted = fit_dixon_coles(
        matches, league="synthetic", as_of=AS_OF,
        team_universe=set(TEAMS) | {"Promoted FC", "Also Promoted"},
        config=config,
    )
    assert fitted.prior_teams == frozenset({"Promoted FC", "Also Promoted"})
    # no data term -> the prior dominates: both relax to it (up to the
    # sum-to-zero packing, which couples the last team to the others)
    assert fitted.attacks["Promoted FC"] == pytest.approx(
        fitted.attacks["Also Promoted"], abs=1e-3)
    assert fitted.defences["Promoted FC"] == pytest.approx(
        fitted.defences["Also Promoted"], abs=1e-3)
    # and the prior equals the weakest-third baseline of the last
    # completed season (all synthetic teams, computed in target space)
    yh, ya = _yh_ya(matches, config.weight_xg)
    _, prior_att, prior_def = _season_baseline(matches, yh, ya, 2020)
    assert fitted.attacks["Promoted FC"] == pytest.approx(prior_att, abs=1e-3)
    assert fitted.defences["Promoted FC"] == pytest.approx(prior_def, abs=1e-3)
    # prior teams are usable in predictions
    assert fitted.score_matrix("Promoted FC", "Alpha").shape == (11, 11)


def test_one_match_does_not_run_away_from_the_prior() -> None:
    """Regression (review B1): w=1, one 0:3 match used to send the free
    attack to lambda ~ 0 (and defence to mu ~ 7); with the Gaussian prior
    the newcomer stays near its prior."""
    established = generate_matches(rounds=15, seed=5, season=2020)
    established = [TrainingMatch(**{**m.__dict__,
                                    "kickoff": AS_OF - dt.timedelta(days=200)})
                   for m in established]
    newcomer_match = TrainingMatch(
        match_id=999, kickoff=AS_OF - dt.timedelta(days=3), season=2021,
        home="Newcomer", away="Alpha", home_goals=0, away_goals=3,
        home_xg=None, away_xg=None,
    )
    config = ModelConfig(xi=0.0, weight_xg=1.0)
    fitted = fit_dixon_coles(
        [*established, newcomer_match], league="synthetic", as_of=AS_OF,
        team_universe=set(TEAMS) | {"Newcomer"}, config=config,
    )
    yh, ya = _yh_ya(established, config.weight_xg)
    _, prior_att, prior_def = _season_baseline(established, yh, ya, 2020)
    a = fitted.attacks["Newcomer"]
    d = fitted.defences["Newcomer"]
    assert abs(a - prior_att) < 0.35   # pulled down by 0 goals, but bounded
    assert a > prior_att - 0.35
    assert abs(d - prior_def) < 0.35   # 3 conceded, still bounded
    lam, mu = fitted._rates("Newcomer", "Alpha")
    assert 0.05 < lam < 6.0 and 0.05 < mu < 6.0
    assert not fitted.suspicious_params


def test_three_matches_stay_within_one_sigma_of_the_prior() -> None:
    established = generate_matches(rounds=15, seed=6, season=2020)
    established = [TrainingMatch(**{**m.__dict__,
                                    "kickoff": AS_OF - dt.timedelta(days=200)})
                   for m in established]
    # newcomer loses twice and scraps one goal: weak but not extreme
    newcomer_games = [("Newcomer", "Bravo", 0, 2), ("Alpha", "Newcomer", 2, 0),
                      ("Newcomer", "Charlie", 1, 2)]
    newcomer = [
        TrainingMatch(match_id=900 + i,
                      kickoff=AS_OF - dt.timedelta(days=10 - i), season=2021,
                      home=h, away=a, home_goals=gh, away_goals=ga,
                      home_xg=None, away_xg=None)
        for i, (h, a, gh, ga) in enumerate(newcomer_games)
    ]
    config = ModelConfig(xi=0.0, weight_xg=1.0)
    fitted = fit_dixon_coles(
        established + newcomer, league="synthetic", as_of=AS_OF,
        team_universe=set(TEAMS) | {"Newcomer"}, config=config,
    )
    yh, ya = _yh_ya(established, config.weight_xg)
    _, prior_att, prior_def = _season_baseline(established, yh, ya, 2020)
    sigma = config.prior_sigma
    assert abs(fitted.attacks["Newcomer"] - prior_att) < sigma
    assert abs(fitted.defences["Newcomer"] - prior_def) < sigma


def test_promoted_team_error_falls_with_matches_played() -> None:
    """The fitted newcomer converges to its true strength as k grows.

    The newcomer is a STRONG promoted team (surprise promotion): its
    blind prior is far from the truth, so the decline is unambiguous.
    Errors are averaged over 5 draw seeds: single small-k samples can be
    lucky, the mean trend may not be.
    """
    true_att, true_def = 0.30, -0.20
    established = generate_matches(rounds=25, seed=8, season=2020)
    established = [TrainingMatch(**{**m.__dict__,
                                    "kickoff": AS_OF - dt.timedelta(days=200)})
                   for m in established]
    truth = FittedModel(
        league="synthetic", as_of=AS_OF, xi=0.0, w=1.0, n_matches=0,
        attacks={**TRUE_ATTACKS, "Newcomer": true_att},
        defences={**TRUE_DEFENCES, "Newcomer": true_def},
        home_advantage=TRUE_GAMMA, rho=TRUE_RHO, intercept=0.0,
        converged=True, config_hash="synthetic",
    )
    mean_errors = []
    for k in (0, 2, 6, 24):
        errs = []
        for seed in (3, 4, 5, 6, 7):
            rng = np.random.default_rng(seed * 100 + k)
            extra = []
            for i in range(k):
                opp = TEAMS[i % len(TEAMS)]
                home, away = ("Newcomer", opp) if i % 2 == 0 else (opp, "Newcomer")
                m = truth.score_matrix(home, away)
                cell = int(rng.choice(m.size, p=m.ravel()))
                gh, ga = divmod(cell, m.shape[1])
                extra.append(TrainingMatch(
                    match_id=700 + i,
                    kickoff=AS_OF - dt.timedelta(days=30 - i), season=2021,
                    home=home, away=away, home_goals=gh, away_goals=ga,
                    home_xg=None, away_xg=None))
            fitted = fit_dixon_coles(
                established + extra, league="synthetic", as_of=AS_OF,
                team_universe=set(TEAMS) | {"Newcomer"},
                config=ModelConfig(xi=0.0, weight_xg=1.0),
            )
            errs.append(max(abs(fitted.attacks["Newcomer"] - true_att),
                            abs(fitted.defences["Newcomer"] - true_def)))
        mean_errors.append(float(np.mean(errs)))
    assert mean_errors[3] < mean_errors[0]   # 24 matches beat the prior
    assert mean_errors[3] < mean_errors[1]   # ... and beat 2 matches
    assert mean_errors[3] < mean_errors[2]   # ... and beat 6 matches


def test_suspicious_params_guard_flags_pathological_fits() -> None:
    from core.model.dixon_coles import _params_look_suspicious

    matches = generate_matches(rounds=5, seed=4, season=2021)
    ok = _params_look_suspicious(
        "synthetic",
        {t: 0.1 for t in TEAMS}, {t: -0.1 for t in TEAMS},
        mu0=0.2, gamma=0.2, matches=matches)
    assert ok is False
    crazy = {t: 0.1 for t in TEAMS}
    crazy["Golf"] = 3.5   # Golf IS a current-season team
    bad = _params_look_suspicious(
        "synthetic", crazy, {t: -0.1 for t in TEAMS},
        mu0=0.2, gamma=0.2, matches=matches)
    assert bad is True
    bad_rates = _params_look_suspicious(
        "synthetic",
        {t: 2.6 for t in TEAMS}, {t: 2.6 for t in TEAMS},
        mu0=0.2, gamma=0.2, matches=matches)
    assert bad_rates is True  # exp(0.2+2.6+2.6) far above 6


def test_last_completed_season_and_baseline_use_quiet_seasons_only() -> None:
    old = [TrainingMatch(
        match_id=i, kickoff=AS_OF - dt.timedelta(days=400 + i), season=2023,
        home="Alpha", away="Bravo", home_goals=1, away_goals=0,
        home_xg=None, away_xg=None) for i in range(6)]
    done = [TrainingMatch(
        match_id=100 + i, kickoff=AS_OF - dt.timedelta(days=120 + i),
        season=2024, home=h, away=a, home_goals=2, away_goals=1,
        home_xg=None, away_xg=None)
        for i, (h, a) in enumerate([("Alpha", "Bravo"), ("Bravo", "Alpha"),
                                    ("Alpha", "Charlie"), ("Charlie", "Alpha")])]
    live = [TrainingMatch(
        match_id=200, kickoff=AS_OF - dt.timedelta(days=5), season=2025,
        home="Alpha", away="Bravo", home_goals=1, away_goals=1,
        home_xg=None, away_xg=None)]
    matches = old + done + live
    assert _last_completed_season(matches, AS_OF) == 2024
    # y arrays consistent with the matches' goals (w=1)
    yh = np.array([1.0] * 6 + [2.0, 2.0, 2.0, 2.0] + [1.0])
    ya = np.array([0.0] * 6 + [1.0, 1.0, 1.0, 1.0] + [1.0])
    teams, pa, pd = _season_baseline(matches, yh, ya, 2024)
    # the baseline only sees season-2024 teams and data: dropping the old
    # (2023) and live (2025) matches must not change the prior at all
    teams_ref, pa_ref, pd_ref = _season_baseline(
        done, yh[6:10], ya[6:10], 2024)
    assert teams == teams_ref == {"Alpha", "Bravo", "Charlie"}
    assert pa == pytest.approx(pa_ref, abs=1e-12)
    assert pd == pytest.approx(pd_ref, abs=1e-12)
    # and a match exactly at as_of - lag boundary is never baseline data
    near_now = TrainingMatch(
        match_id=999, kickoff=AS_OF - dt.timedelta(minutes=149), season=2026,
        home="Alpha", away="Bravo", home_goals=1, away_goals=1,
        home_xg=None, away_xg=None)
    assert _last_completed_season(
        [*matches, near_now], AS_OF) == 2024  # 2026 is not quiet


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


def test_config_hash_covers_rho_bounds(monkeypatch) -> None:
    import core.model.dixon_coles as dc

    base = config_hash(ModelConfig())
    monkeypatch.setattr(dc, "RHO_BOUNDS", (-0.2, 0.2))
    assert config_hash(ModelConfig()) != base
    monkeypatch.setattr(dc, "RHO_BOUNDS", (-0.15, 0.15))
    assert config_hash(ModelConfig()) == base


def test_tau_does_not_move_totals_above_two() -> None:
    """The DC tau redistributes mass only inside the totals 0/1/2 bands
    while preserving the marginals, so P(total >= 3) - and with it
    over 2.5 / over 3.5 - is exactly rho-invariant; the lower overs do
    move."""
    from core.model.markets import total_over

    m0 = true_model(rho=0.0).score_matrix("Bravo", "Echo")
    m_rho = true_model(rho=-0.08).score_matrix("Bravo", "Echo")
    assert total_over(m0, 2.5) == pytest.approx(total_over(m_rho, 2.5),
                                                abs=1e-12)
    assert total_over(m0, 3.5) == pytest.approx(total_over(m_rho, 3.5),
                                                abs=1e-12)
    assert total_over(m0, 0.5) != pytest.approx(total_over(m_rho, 0.5),
                                                abs=1e-9)


def test_config_hash_is_stable_and_sensitive() -> None:
    base = ModelConfig()
    assert config_hash(base) == config_hash(ModelConfig())
    assert config_hash(base) != config_hash(ModelConfig(xi=0.006))
    assert config_hash(base) != config_hash(ModelConfig(weight_xg=0.6))


# --- phase-4 model options (part 2.1c) --------------------------------------


def _biased_xg_matches(rounds: int = 40, seed: int = 21, bias: float = 1.15):
    """Goals from the true DC model, xG systematically `bias` x higher."""
    rng = np.random.default_rng(seed)
    model = true_model()
    matches: list[TrainingMatch] = []
    mid = 0
    for _ in range(rounds):
        for i, h in enumerate(TEAMS):
            for a in TEAMS[i + 1:]:
                for home, away in ((h, a), (a, h)):
                    m = model.score_matrix(home, away)
                    k = int(rng.choice(m.size, p=m.ravel()))
                    gh, ga = divmod(k, m.shape[1])
                    lam, mu = model._rates(home, away)
                    mid += 1
                    matches.append(TrainingMatch(
                        match_id=mid,
                        kickoff=AS_OF - dt.timedelta(
                            days=float(rng.uniform(5, 300))),
                        season=2020, home=home, away=away,
                        home_goals=gh, away_goals=ga,
                        home_xg=float(rng.poisson(lam * bias)),
                        away_xg=float(rng.poisson(mu * bias))))
    return matches


def _mean_expected(model: FittedModel, pairs) -> float:
    return float(np.mean([model.expected_goals(h, a)[0]
                          + model.expected_goals(a, h)[0]
                          for h, a in pairs]))


def _mean_predicted_total(model: FittedModel) -> float:
    """Mean predicted goals per match over all ordered pairings."""
    pairs = [(h, a) for h in TEAMS for a in TEAMS if h != a]
    return float(np.mean([sum(model.expected_goals(h, a))
                          for h, a in pairs]))


def test_xg_goals_scaling_pulls_level_back_to_goals() -> None:
    matches = _biased_xg_matches(rounds=30, seed=21, bias=1.15)
    actual = float(np.mean([m.home_goals + m.away_goals for m in matches]))
    base = fit_dixon_coles(matches, league="s", as_of=AS_OF,
                           team_universe=set(TEAMS),
                           config=ModelConfig(xi=0.0, weight_xg=0.5))
    scaled = fit_dixon_coles(
        matches, league="s", as_of=AS_OF, team_universe=set(TEAMS),
        config=ModelConfig(xi=0.0, weight_xg=0.5, xg_goals_scaling=True))
    pred_base = _mean_predicted_total(base)
    pred_scaled = _mean_predicted_total(scaled)
    # biased xG inflates the unscaled level; scaling pulls it to goals
    assert pred_scaled < pred_base
    assert abs(pred_scaled - actual) < abs(pred_base - actual)


def test_level_from_goals_predicts_goals_not_mixture() -> None:
    matches = _biased_xg_matches(rounds=30, seed=21, bias=1.2)
    actual = float(np.mean([m.home_goals + m.away_goals for m in matches]))
    model = fit_dixon_coles(
        matches, league="s", as_of=AS_OF, team_universe=set(TEAMS),
        config=ModelConfig(xi=0.0, weight_xg=0.5, level_from_goals=True))
    assert model.intercept_mixed is not None
    assert model.intercept_mixed > model.intercept  # mixture above goals
    pred = _mean_predicted_total(model)
    assert abs(pred - actual) < 0.10, (pred, actual)


def test_fixed_params_pin_the_scalars() -> None:
    matches = generate_matches(rounds=20, seed=17)
    model = fit_dixon_coles(
        matches, league="s", as_of=AS_OF, team_universe=set(TEAMS),
        config=ModelConfig(xi=0.0),
        fixed_params={"rho": 0.05, "gamma": 0.20})
    assert model.rho == pytest.approx(0.05, abs=1e-12)
    assert model.home_advantage == pytest.approx(0.20, abs=1e-12)
    assert model.converged
    # unknown names are rejected
    with pytest.raises(ValueError, match="unknown fixed params"):
        fit_dixon_coles(matches, league="s", as_of=AS_OF,
                        team_universe=set(TEAMS), config=ModelConfig(xi=0.0),
                        fixed_params={"mu0": 0.1, "bogus": 1.0})


def test_two_level_gradient_matches_finite_differences() -> None:
    rng = np.random.default_rng(5)
    n_teams, n_matches = 5, 120
    home_idx = rng.integers(0, n_teams, n_matches)
    away_idx = np.array([(h + 1 + rng.integers(0, n_teams - 1)) % n_teams
                         for h in home_idx])
    yh = rng.gamma(3.0, 0.5, n_matches)
    ya = rng.gamma(2.5, 0.5, n_matches)
    gh = rng.integers(0, 4, n_matches)
    ga = rng.integers(0, 4, n_matches)
    names = ("mu0", "gamma", "mu0g", "gammag", "rho")
    ctx = _FitContext(
        home_idx=home_idx, away_idx=away_idx, yh=yh, ya=ya, gh=gh, ga=ga,
        time_w=np.ones(n_matches), ll_const=_gammaln(yh) + _gammaln(ya),
        m_attack=np.zeros(n_teams), m_defence=np.zeros(n_teams),
        prior_sigma=0.5, n_teams=n_teams, two_level=True,
        scalar_names=names)
    x = rng.normal(0, 0.1, 2 * n_teams - 2 + len(names))
    x[-1] = 0.04
    _, g = _neg_loglik(x, ctx)
    eps = 1e-6
    g_fd = np.zeros_like(x)
    for i in range(x.size):
        xp, xm = x.copy(), x.copy()
        xp[i] += eps
        xm[i] -= eps
        g_fd[i] = (_neg_loglik(xp, ctx)[0] - _neg_loglik(xm, ctx)[0]) \
            / (2 * eps)
    assert_allclose(g, g_fd, rtol=1e-5, atol=1e-6)
