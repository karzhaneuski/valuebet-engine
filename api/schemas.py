"""Pydantic schemas for ValueBet Engine REST API."""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, Field


class MatchSummary(BaseModel):
    id: int
    league: str
    season: str
    home_team: str
    away_team: str
    date_utc: dt.date
    kickoff_utc: dt.datetime | None = None
    status: str
    has_market_lines: bool
    available_markets: list[str] = Field(default_factory=list)


class MatchListResponse(BaseModel):
    matches: list[MatchSummary]
    total: int


class ExactScorePrice(BaseModel):
    score: str
    prob: float
    fair_odds: float


class TotalsLinePrice(BaseModel):
    line: float
    over_prob: float
    under_prob: float
    push_prob: float
    over_fair_odds: float
    under_fair_odds: float


class H2HOdds(BaseModel):
    home_prob: float
    home_fair_odds: float
    draw_prob: float
    draw_fair_odds: float
    away_prob: float
    away_fair_odds: float


class BTTSOdds(BaseModel):
    yes_prob: float
    yes_fair_odds: float
    no_prob: float
    no_fair_odds: float


class MultiscoreItem(BaseModel):
    combination: str
    prob: float
    fair_odds: float


class GoalsRangeItem(BaseModel):
    range: str
    prob: float
    fair_odds: float


class TeamTotalsItem(BaseModel):
    team: str
    line: float
    over_prob: float
    under_prob: float
    push_prob: float
    over_fair_odds: float
    under_fair_odds: float


class FairPricesResponse(BaseModel):
    match_id: int
    home_team: str
    away_team: str
    kickoff_utc: dt.datetime | None
    league: str
    exact_score: list[ExactScorePrice]
    inny: ExactScorePrice
    multiscore: list[MultiscoreItem]
    goals_range: list[GoalsRangeItem]
    totals: list[TotalsLinePrice]
    btts: BTTSOdds
    h2h: H2HOdds
    team_totals: list[TeamTotalsItem]
    methodological_caveats: list[str]
    anomaly_flags: list[str] = Field(default_factory=list)


class FortunaEvalRequest(BaseModel):
    match_id: int | None = None
    query: str | None = None
    raw_text: str


class FortunaPriceEvaluation(BaseModel):
    market: str
    selection: str
    bookmaker_odds: float
    fair_odds: float
    fair_prob: float
    edge_pct: float
    price_above_fair: bool
    section_margin: float | None = None
    status_note: str


class FortunaEvalResponse(BaseModel):
    match_id: int | None
    match_name: str
    section_margins: dict[str, float]
    prices_above_fair_count: int
    evaluations: list[FortunaPriceEvaluation]
    caveats: list[str]
    disclaimer: str


class UserBetCreate(BaseModel):
    match_id: int
    market: str
    outcome: str
    line: float | None = None
    odds: float = Field(gt=1.0)
    stake: float = Field(gt=0.0)
    bookmaker: str = "Fortuna"
    placed_at_utc: dt.datetime | None = None


class UserBetItem(BaseModel):
    id: int
    match_id: int
    match_name: str
    market: str
    outcome: str
    line: float | None = None
    odds: float
    stake: float
    bookmaker: str
    status: str
    pnl: float | None = None
    clv: float | None = None
    clv_pct: float | None = None
    clv_source: str | None = None
    placed_at_utc: dt.datetime
    settled_at_utc: dt.datetime | None = None


class UserBetListResponse(BaseModel):
    bets: list[UserBetItem]
    total: int


class BootstrapCI(BaseModel):
    lower: float
    upper: float


class PerformanceResponse(BaseModel):
    total_bets: int
    settled_bets: int
    pending_bets: int
    turnover: float
    total_profit: float
    roi: float
    hit_rate: float
    avg_clv: float
    clv_positive_share: float
    brier_score: float | None
    bootstrap_ci_roi_95: BootstrapCI | None = None
    bootstrap_ci_clv_95: BootstrapCI | None = None
    sample_size_warning: str | None = None
    disclaimer: str


class HealthResponse(BaseModel):
    status: str
    version: str
    database_connected: bool
    database_url_redacted: str | None = None
    last_odds_snapshot_utc: dt.datetime | None = None
    toa_credits_remaining: int | None = None
    search_quota: dict[str, Any] = Field(default_factory=dict)
    pacing_stage: int = 0
    pacing_degraded: bool = False
    pacing_actions: list[str] = Field(default_factory=list)
