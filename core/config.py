"""Typed configuration (config.yaml via pydantic-settings, secrets from .env).

Structure and defaults follow docs/decisions.md (phase 0) — this module only
declares shape and defaults; behaviour lives in the later phases.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

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
    totals_edge_threshold: float = 0.04   # UCL starts higher (SPEC §1)


class DatabaseConfig(BaseModel):
    url: str = "sqlite:///./valuebet.db"


class ModelConfig(BaseModel):
    """Dixon-Coles hyperparameters; xi and w are tuned in phase 4."""

    xi: float = 0.005          # time decay per day, exp(-xi * days)
    weight_xg: float = 0.5     # w in w*goals + (1-w)*xG
    max_score: int = 10        # score matrix 0..10 x 0..10
    home_advantage_prior: float = 0.25
    promoted_team_prior: str = "league_weakest_third"


class ThresholdsConfig(BaseModel):
    totals_edge: float = 0.04
    multiscore_edge: float = 0.08
    exact_score_edge: float = 0.12
    exact_score_min_p: float = 0.04
    exact_score_max_odds: float = 21.0


class RiskFlagsConfig(BaseModel):
    key_player_min_starts_share: float = 0.70
    key_player_last_n: int = 8
    absence_edge_multiplier: float = 1.5
    max_key_absences_per_team: int = 2


class OddsTierWindows(BaseModel):
    """Tolerance windows in hours (docs/decisions.md, review B1)."""

    t48: tuple[float, float] = (40.0, 56.0)
    t24: tuple[float, float] = (18.0, 30.0)
    t3: tuple[float, float] = (1.0, 4.0)


class OddsPolicyConfig(BaseModel):
    """Snapshot policy (d) with per-slot T-3 as default (decisions, S1)."""

    tier_windows: OddsTierWindows = Field(default_factory=OddsTierWindows)
    t3_granularity: str = "slot"          # slot | day (day = degradation)
    max_firing_delay_min: int = 15        # fire <= lower bound + this
    retry_on_error: int = 1
    t1_bet_market: bool = False           # optional T-1 candidate snapshot
    hard_stop_credits: int = 450          # of 500 monthly
    # degradation ladder at the stop (decisions, final round S3)
    degradation_ladder: list[str] = Field(default_factory=lambda: [
        "cap_additional_markets_top5_week",
        "disable_t48",
        "disable_t24",
        "t3_slot_to_day",
    ])
    scores_poll_after_matchday: bool = True
    scores_poll_weekly: bool = True


class PrefilterConfig(BaseModel):
    """Candidate prefilter before buying event-market odds (decisions S2)."""

    delta_btts: float = 0.05      # |P_model(BTTS) - P_proxy(BTTS)| >= delta
    edge_fraction: float = 0.5    # model edge >= fraction * market threshold
    weekly_cap: int = 15


class BooksConfig(BaseModel):
    """Bookmaker roles (decisions S10: Fortuna is the only bettable book)."""

    market_reference_books: list[str] = Field(default_factory=lambda: [
        "Pinnacle", "consensus_of_allowed",
    ])
    bookmaker_whitelist: list[str] = Field(default_factory=lambda: ["fortuna"])
    excluded_books: list[str] = Field(default_factory=lambda: [
        "1xBet", "BetOnline.ag", "MyBookie.ag", "GTbets",  # offshore
        "Betfair", "Matchbook",                            # exchanges
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


class SchedulerConfig(BaseModel):
    """Cron entries; real jobs arrive in phase 7."""

    timezone: str = "UTC"
    refresh_history_cron: str = "30 5 * * *"
    refresh_odds_cron: str = "0 */6 * * *"
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
    odds_policy: OddsPolicyConfig = Field(default_factory=OddsPolicyConfig)
    prefilter: PrefilterConfig = Field(default_factory=PrefilterConfig)
    books: BooksConfig = Field(default_factory=BooksConfig)
    totals_lines: TotalsLinesConfig = Field(default_factory=TotalsLinesConfig)
    llm: LlmModelsConfig = Field(default_factory=LlmModelsConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)


def load_config(path: str | Path | None = None) -> AppConfig:
    """Parse config.yaml into the typed AppConfig model."""
    cfg_path = Path(path) if path else Path(__file__).resolve().parents[1] / "config.yaml"
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    return AppConfig.model_validate(data)


def load_secrets() -> Secrets:
    return Secrets()


@lru_cache(maxsize=1)
def cached_config(path: str | None = None) -> AppConfig:
    return load_config(path)
