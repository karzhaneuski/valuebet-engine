"""ORM models — the full phase-1 schema (SPEC §9 + docs/decisions.md).

Design notes (docs/decisions.md, phase 0):
- matches carries external ids for THREE live sources (understat,
  football-data row identity is via team/date, TOA event id) plus
  commence_time_utc from TOA as the authoritative kickoff.
- API-Football is historical-only (free plan has no current season), so
  there is no api_football fixture id on matches; team_aliases still
  supports the api_football spelling for backfills.
- Kickoff/status vocabulary implements the postponement rules decided in
  phase 0: scheduled | postponed_suspected | postponed | finished | void.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# --- enums ------------------------------------------------------------------

LEAGUE_SOURCES = ("understat", "football_data", "toa", "api_football")
MATCH_STATUSES = ("scheduled", "postponed_suspected", "postponed", "finished",
                  "void")
KICKOFF_SOURCES = ("toa", "understat_placeholder")
ODD_MARKETS = ("totals", "btts", "correct_score", "alternate_totals")
ODD_TIERS = ("T-48", "T-24", "T-3", "T-1", "manual")
ODD_SOURCES = ("toa", "manual_fortuna")
CLV_SOURCES = ("avgc", "maxc", "own_snapshot_t3")
RECO_VARIANTS = ("base", "shadow_llm")
FINAL_ODDS_SOURCES = ("fortuna_manual", "fortuna_api")
PLAYER_STATUSES = ("out", "doubtful", "suspended")
PLAYER_SOURCES = ("api_football", "news_llm")


# --- teams & aliases --------------------------------------------------------

class Team(Base):
    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    # internal league slug from config.yaml (leagues[].slug)
    league: Mapped[str | None] = mapped_column(String(40))


class TeamAlias(Base):
    """Maps team spellings across sources (names differ everywhere)."""

    __tablename__ = "team_aliases"
    __table_args__ = (
        UniqueConstraint("source", "alias", name="uq_team_alias_source_alias"),
        Index("ix_team_aliases_team", "team_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    source: Mapped[str] = mapped_column(Enum(*LEAGUE_SOURCES, name="alias_source"))
    alias: Mapped[str] = mapped_column(String(120))


# --- matches ----------------------------------------------------------------

class Match(Base):
    __tablename__ = "matches"
    __table_args__ = (
        Index("ix_matches_date_league", "date_utc", "league"),
        UniqueConstraint("toa_event_id", name="uq_matches_toa_event"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    league: Mapped[str] = mapped_column(String(40))
    season: Mapped[int] = mapped_column(Integer)  # e.g. 2026 = 2026/27
    date_utc: Mapped[dt.date] = mapped_column(Date)
    # authoritative kickoff: TOA commence_time (docs/decisions.md, S5)
    commence_time_utc: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    kickoff_source: Mapped[str] = mapped_column(
        Enum(*KICKOFF_SOURCES, name="kickoff_source"))
    status: Mapped[str] = mapped_column(
        Enum(*MATCH_STATUSES, name="match_status"), default="scheduled")

    home_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    away_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))

    # external ids: understat game id; TOA event id (unique). football-data
    # rows are identified by (date, home/away aliases) — no stable id.
    understat_id: Mapped[int | None] = mapped_column(String(60), index=True)
    toa_event_id: Mapped[str | None] = mapped_column(String(60))


class MatchStat(Base):
    """Goals and xG per match, one row per source (understat / football-data)."""

    __tablename__ = "match_stats"
    __table_args__ = (
        UniqueConstraint("match_id", "source", name="uq_match_stats_source"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), index=True)
    source: Mapped[str] = mapped_column(String(20))
    home_goals: Mapped[int | None] = mapped_column(Integer)
    away_goals: Mapped[int | None] = mapped_column(Integer)
    home_xg: Mapped[Decimal | None] = mapped_column(Numeric(6, 3))
    away_xg: Mapped[Decimal | None] = mapped_column(Numeric(6, 3))


# --- odds -------------------------------------------------------------------

class OddsSnapshot(Base):
    __tablename__ = "odds_snapshots"
    __table_args__ = (
        Index("ix_odds_snapshots_event_market_time", "toa_event_id", "market",
              "captured_at_utc"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    toa_sport_key: Mapped[str] = mapped_column(String(40))
    toa_event_id: Mapped[str] = mapped_column(String(60))
    match_id: Mapped[int | None] = mapped_column(ForeignKey("matches.id"))
    bookmaker: Mapped[str] = mapped_column(String(60))
    market: Mapped[str] = mapped_column(Enum(*ODD_MARKETS, name="odd_market"))
    # line for totals/alternate_totals (2.5 etc.); NULL for btts/correct_score
    line: Mapped[Decimal | None] = mapped_column(Numeric(4, 2))
    # 'Over'/'Under'/'Yes'/'No', or 'H2|A0' for correct_score outcomes
    outcome: Mapped[str] = mapped_column(String(40))
    price: Mapped[Decimal] = mapped_column(Numeric(8, 3))
    tier: Mapped[str] = mapped_column(Enum(*ODD_TIERS, name="odd_tier"))
    # phase-5 firing discipline (docs/decisions.md, final round S1)
    planned_offset_min: Mapped[int | None] = mapped_column(Integer)
    actual_offset_min: Mapped[int | None] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(Enum(*ODD_SOURCES, name="odd_source"))
    captured_at_utc: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))


# --- availability & prefilter ----------------------------------------------

class PlayerAvailability(Base):
    __tablename__ = "player_availability"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), index=True)
    match_id: Mapped[int | None] = mapped_column(ForeignKey("matches.id"))
    player: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(
        Enum(*PLAYER_STATUSES, name="player_status"))
    source: Mapped[str] = mapped_column(
        Enum(*PLAYER_SOURCES, name="player_source"))
    source_url: Mapped[str | None] = mapped_column(String(500))
    quote_summary: Mapped[str | None] = mapped_column(Text)
    detected_at_utc: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))


class PrefilterDecision(Base):
    """Why a match did (not) become an event-market candidate (decisions S2)."""

    __tablename__ = "prefilter_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), index=True)
    decision_date: Mapped[dt.date] = mapped_column(Date)
    p_model_btts: Mapped[Decimal] = mapped_column(Numeric(5, 4))
    p_proxy_btts: Mapped[Decimal] = mapped_column(Numeric(5, 4))
    totals_edge: Mapped[Decimal] = mapped_column(Numeric(6, 4))
    accepted: Mapped[bool] = mapped_column(Boolean)
    reason: Mapped[str] = mapped_column(Text)


# --- recommendations & results ---------------------------------------------

class Recommendation(Base):
    __tablename__ = "recommendations"
    __table_args__ = (
        CheckConstraint("edge >= -1", name="ck_recommendations_edge"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), index=True)
    market: Mapped[str] = mapped_column(Enum(*ODD_MARKETS, name="reco_market"))
    line: Mapped[Decimal | None] = mapped_column(Numeric(4, 2))
    outcome: Mapped[str] = mapped_column(String(40))
    odds: Mapped[Decimal] = mapped_column(Numeric(8, 3))
    p_model: Mapped[Decimal] = mapped_column(Numeric(6, 4))
    edge: Mapped[Decimal] = mapped_column(Numeric(6, 4))
    kelly_quarter_capped: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    flags: Mapped[str] = mapped_column(String(200), default="")
    variant: Mapped[str] = mapped_column(
        Enum(*RECO_VARIANTS, name="reco_variant"), default="base")
    # CLV provenance per docs/decisions.md (CLV metric definition)
    clv: Mapped[Decimal | None] = mapped_column(Numeric(7, 4))
    clv_source: Mapped[str | None] = mapped_column(
        Enum(*CLV_SOURCES, name="clv_source"))
    # final edge is computed against Fortuna (manual input until it appears
    # in an API) — docs/decisions.md (S10)
    final_odds_source: Mapped[str | None] = mapped_column(
        Enum(*FINAL_ODDS_SOURCES, name="final_odds_source"))
    created_at_utc: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))


class Result(Base):
    __tablename__ = "results"
    __table_args__ = (UniqueConstraint("match_id", name="uq_results_match"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"))
    home_goals: Mapped[int] = mapped_column(Integer)
    away_goals: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(20))
    settled_at_utc: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True))


# --- quotas & LLM logging ---------------------------------------------------

class ApiQuotaUsage(Base):
    """One row per billable call (provider-side credits/limits)."""

    __tablename__ = "api_quota_usage"
    __table_args__ = (
        Index("ix_api_quota_usage_provider_time", "provider", "created_at_utc"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(30))  # toa | api_football | ...
    endpoint: Mapped[str] = mapped_column(String(120))
    credits: Mapped[int] = mapped_column(Integer, default=0)
    remaining_header: Mapped[int | None] = mapped_column(Integer)
    created_at_utc: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))


class QuotaCounter(Base):
    """Monthly counters, notably TOA's 500-credit quota (decisions S3)."""

    __tablename__ = "quota_counters"
    __table_args__ = (
        UniqueConstraint("provider", "period", name="uq_quota_provider_period"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(30))
    period: Mapped[str] = mapped_column(String(7))  # 'YYYY-MM'
    credits_used: Mapped[int] = mapped_column(Integer, default=0)
    updated_at_utc: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))


class LlmCall(Base):
    """Full LLM call log; model/version/thinking per decisions (final S5)."""

    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    purpose: Mapped[str] = mapped_column(String(40))  # extraction|reasoning|shadow
    prompt: Mapped[str] = mapped_column(Text)
    response: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str] = mapped_column(String(60))
    model_version: Mapped[str | None] = mapped_column(String(20))
    thinking_config: Mapped[str | None] = mapped_column(Text)  # JSON
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    thoughts_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    created_at_utc: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
