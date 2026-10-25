"""Typed configuration (config.yaml via pydantic-settings, secrets from .env).

Structure and defaults follow docs/decisions.md (phase 0) — this module only
declares shape and defaults; behaviour lives in the later phases.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class LeagueConfig(BaseModel):
    """One league with all external source codes."""

    slug: str
    name: str
    fd_code: str | None = None            # football-data.co.uk CSV code
    understat_league: str | None = None   # soccerdata league code
    toa_sport_key: str | None = None      # The Odds API sport key
    api_football_id: int | None = None    # historical backfill only
    ucl: bool = False
    experimental: bool = False            # UCL totals (SPEC §1, phase 11)
    # measured in phase 0 (odds_api.json); effective lines are the
    # intersection with `totals_lines.requested` — phase 5 logic
    totals_lines_available: list[float] = Field(default_factory=list)
    totals_edge_threshold: float | None = None  # reference-only (totals have no reco)


class DatabaseConfig(BaseModel):
    url: str = "sqlite:///./valuebet.db"


class ModelConfig(BaseModel):
    """Dixon-Coles hyperparameters; xi, w, prior_sigma and the lag are
    tuned in phase 4.

    home_advantage_prior / promoted_team_prior are STARTING VALUES for the
    phase-3 fitter, not tuned constants.
    """

    xi: float = 0.005          # time decay per day, exp(-xi * days)
    weight_xg: float = 0.5     # w in w*goals + (1-w)*xG
    max_score: int = 10        # score matrix 0..10 x 0..10
    home_advantage_prior: float = 0.25   # phase-3 starting value
    promoted_team_prior: str = "league_weakest_third"  # phase-3 starting rule
    # Gaussian prior on attack/defence (review B1): sigma ~ 1.5x the
    # empirical between-team sd of fitted attacks (~0.33 in the smoke fit)
    # so thin history (1-5 matches) is shrunk to its prior while >=15
    # matches dominate; phase-4 tunable
    prior_sigma: float = 0.5
    # a result only becomes usable for training this long after kickoff
    # (review S1): 90 min play + stoppages/reporting buffer
    result_lag_minutes: int = 150
    # --- phase-4 model options (decisions 2026-10-25 S2/S3), all off by
    # default; each affects the fit and is covered by config_hash
    # rescale understat xG by sum(goals)/sum(xG) over the training data
    # (strictly up to as_of) before mixing
    xg_goals_scaling: bool = False
    # estimate the scoring level (mu0, gamma) from GOALS while team
    # strengths still use the mixed target; the score matrix then uses
    # the goals level (predictions are about goals)
    level_from_goals: bool = False
    # one rho shared across leagues (computed by the backtest runner in
    # a two-pass fit, enforced through fixed_params)
    common_rho: bool = False
    # league-level params (mu0, gamma, rho) estimated with a slower time
    # decay: xi * multiplier in the first pass, then fixed (two-pass)
    league_decay_multiplier: float = 1.0


class ThresholdsConfig(BaseModel):
    # Uncalibrated initial starting thresholds (empirical tuning occurs via forward CLV)
    totals_edge: float | None = None  # reference-only (totals have no reco)
    multiscore_edge: float = 0.08     # uncalibrated starting threshold
    exact_score_edge: float = 0.12    # uncalibrated starting threshold
    exact_score_min_p: float = 0.04
    exact_score_max_odds: float = 21.0
    implausible_edge_exact_score: float = 0.40
    implausible_edge_multiscore: float = 0.25
    implausible_edge_goals_range: float = 0.25
    multiscore_max_odds: float = 15.0
    multiscore_min_p: float = 0.06
    goals_range_max_odds: float = 8.0
    goals_range_min_p: float = 0.15


class FortunaConfig(BaseModel):
    """Fortuna bookmaker grid configuration."""

    cs_grid_max_goals: int = 4  # 0..4 goals per team (25 scores + inny)


class RiskFlagsConfig(BaseModel):
    key_player_min_starts_share: float = 0.70
    key_player_last_n: int = 8
    absence_edge_multiplier: float = 1.5
    max_key_absences_per_team: int = 2


class FairMatrixConfig(BaseModel):
    """Parameters for fair market matrix construction (SPEC §4, decisions B6.1)."""

    rho: float = 0.0
    kappa_high: float = 0.20
    default_totals_line: float = 2.5
    anomaly_share_threshold: float = 0.15


class OddsTierWindows(BaseModel):
    """Tolerance windows in hours (docs/decisions.md, review B1)."""

    t48: tuple[float, float] = (40.0, 56.0)
    t24: tuple[float, float] = (18.0, 30.0)
    t3: tuple[float, float] = (1.0, 4.0)


class PacingConfig(BaseModel):
    """remaining/days_left pacing rule (decisions, final round S3)."""

    enabled: bool = True
    # projected burn = used + spent_per_day_so_far * days_left; degrade
    # early when projection would cross hard_stop + safety_margin
    safety_margin_credits: int = 25


class PostponementConfig(BaseModel):
    """Postponement rules (docs/decisions.md, phase 0 / S5)."""

    shift_min: int = 15            # commence_time moved by more than this
    no_result_after_h: int = 3     # no result by KO + this -> suspected
    void_after_h: int = 48         # recommendations older than this voided


DegradationStep = Literal[
    "cap_correct_score_5_week",
    "disable_t3_h2h",
    "t3_slot_to_day",
]


def _default_ladder() -> list[DegradationStep]:
    # Review B1: (a) correct_score -> 5/wk; (b) disable T-3 h2h; (c) T-3 slot -> day
    return ["cap_correct_score_5_week", "disable_t3_h2h", "t3_slot_to_day"]


class OddsPolicyConfig(BaseModel):
    """Snapshot policy (d) with per-slot T-3 as default (decisions, S1, review B1)."""

    tier_windows: OddsTierWindows = Field(default_factory=OddsTierWindows)
    # T-48 OFF by default (review B1)
    active_tiers: list[str] = Field(default_factory=lambda: ["t24", "t3"])
    tier_markets: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "t24": ["totals", "h2h"],
            "t3": ["totals", "h2h"],
        }
    )
    pinnacle_correct_score_max_weekly: int = 10  # Phase 4b B1 budget cap
    t3_granularity: Literal["slot", "day"] = "slot"  # day = degradation only
    max_firing_delay_min: int = 15        # fire <= lower bound + this
    retry_on_error: int = 1
    t1_bet_market: bool = False           # optional T-1 candidate snapshot
    monthly_quota: int = 500              # TOA free tier
    hard_stop_credits: int = 450          # of monthly_quota
    # degradation ladder at the stop (decisions, final round S3, review B1); the order
    # is fixed, T-3 is never disabled
    degradation_ladder: list[DegradationStep] = Field(
        default_factory=_default_ladder)
    pacing: PacingConfig = Field(default_factory=PacingConfig)
    t4_recheck: bool = True               # recheck /events at T-4 per candidate
    postponement: PostponementConfig = Field(default_factory=PostponementConfig)
    scores_poll_after_matchday: bool = True
    scores_poll_weekly: bool = True


class PrefilterConfig(BaseModel):
    """Candidate prefilter before buying event-market odds (decisions S2)."""

    delta_btts: float = 0.05      # |P_model(BTTS) - P_proxy(BTTS)| >= delta
    edge_fraction: float = 0.5    # model edge >= fraction * market threshold
    weekly_cap: int = 15


class BooksConfig(BaseModel):
    """Bookmaker roles (decisions S10: Fortuna is the only bettable book)."""

    # bookmaker identifiers are TOA bookmaker KEYS (odds_api.json), not
    # display names; "consensus_of_allowed" is our own consensus marker
    market_reference_books: list[str] = Field(default_factory=lambda: [
        "pinnacle", "consensus_of_allowed",
    ])
    bookmaker_whitelist: list[str] = Field(default_factory=lambda: ["fortuna"])
    excluded_books: list[str] = Field(default_factory=lambda: [
        "onexbet", "betonlineag", "mybookieag", "gtbets",  # offshore
        "betfair", "matchbook",                            # exchanges
    ])


class TotalsLinesConfig(BaseModel):
    """Half lines only in v1 (decisions S11); intersection is phase-5 logic."""

    requested: list[float] = Field(default_factory=lambda: [
        0.5, 1.5, 2.5, 3.5, 4.5,
    ])
    half_lines_only: bool = True


class LlmModelsConfig(BaseModel):
    """Chosen by name-stability + availability probe (decisions, final S5)."""

    extraction_model: str = "gemini-3.8-flash"
    reasoning_model: str = "gemini-3.8-flash"
    reasoning_thinking_budget: int = 4096
    fallback_model: str = "gemini-3.7-flash"
    api_base: str = "https://generativelanguage.googleapis.com/v1beta"


class IngestConfig(BaseModel):
    """Loader tuning (review B4/S2)."""

    fd_current_season_ttl_hours: int = 12
    retry_attempts: int = 3
    retry_backoff_s: float = 2.0


class SchedulerConfig(BaseModel):
    """Cron entries; real jobs arrive in phase 7.

    events_poll_cron polls ONLY the free TOA /events endpoint (kickoffs and
    postponements); paid odds snapshots are event-driven via tier windows —
    flat 6h odds polling was rejected in docs/decisions.md.
    """

    timezone: str = "UTC"
    refresh_history_cron: str = "30 5 * * *"
    events_poll_cron: str = "0 */6 * * *"
    retrain_model_cron: str = "0 6 * * *"
    news_windows: list[str] = Field(default_factory=lambda: ["T-24h", "T-2h"])


class Secrets(BaseSettings):
    """Loaded from .env — never committed."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    api_football_key: str = ""
    odds_api_key: str = ""
    tavily_api_key: str = ""
    brave_search_api_key: str = ""
    gemini_api_key: str = ""
    api_token: str = ""


class AppConfig(BaseModel):
    leagues: list[LeagueConfig]
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    model: ModelConfig = Field(default_factory=ModelConfig)
    thresholds: ThresholdsConfig = Field(default_factory=ThresholdsConfig)
    risk_flags: RiskFlagsConfig = Field(default_factory=RiskFlagsConfig)
    fair_matrix: FairMatrixConfig = Field(default_factory=FairMatrixConfig)
    odds_policy: OddsPolicyConfig = Field(default_factory=OddsPolicyConfig)
    prefilter: PrefilterConfig = Field(default_factory=PrefilterConfig)
    books: BooksConfig = Field(default_factory=BooksConfig)
    totals_lines: TotalsLinesConfig = Field(default_factory=TotalsLinesConfig)
    llm: LlmModelsConfig = Field(default_factory=LlmModelsConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    ingest: IngestConfig = Field(default_factory=IngestConfig)
    fortuna: FortunaConfig = Field(default_factory=FortunaConfig)


def load_config(path: str | Path | None = None) -> AppConfig:
    """Parse config.yaml into the typed AppConfig model.

    The DATABASE_URL environment variable overrides database.url (the same
    override exists in alembic/env.py) so containers can point at a volume
    without touching the committed config.
    """
    cfg_path = Path(path) if path else Path(__file__).resolve().parents[1] / "config.yaml"
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    if os.environ.get("DATABASE_URL"):
        data.setdefault("database", {})["url"] = os.environ["DATABASE_URL"]
    return AppConfig.model_validate(data)


def load_secrets() -> Secrets:
    return Secrets()


@lru_cache(maxsize=1)
def cached_config(path: str | None = None) -> AppConfig:
    return load_config(path)
