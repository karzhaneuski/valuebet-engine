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
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy import text as sa_text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class UTCDateTime(TypeDecorator):
    """DateTime that always reads back as timezone-aware UTC (SQLite loses
    the tzinfo on storage; S7)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=dt.UTC)
        return value


def sa_enum(*values: str, name: str) -> Enum:
    """Portable enum: VARCHAR + CHECK constraint (SQLite has no native enum)."""
    return Enum(*values, name=name, native_enum=False, create_constraint=True,
                validate_strings=True)



# --- enums ------------------------------------------------------------------

LEAGUE_SOURCES = ("understat", "football_data", "toa", "api_football")
MATCH_STATUSES = ("scheduled", "postponed_suspected", "postponed", "finished",
                  "void")
KICKOFF_SOURCES = ("toa", "understat_placeholder")
# TOA market keys, as they come from the API (odds_snapshots)
ODD_MARKETS = ("totals", "btts", "correct_score", "alternate_totals")
# SPEC §1 market vocabulary (recommendations); correct_score is the TOA
# spelling of the exact-score market, exact_score/multiscore/goals_range
# are our internal names
RECO_MARKETS = ("exact_score", "multiscore", "goals_range", "totals", "btts")
ODD_TIERS = ("T-48", "T-24", "T-3", "T-1", "manual", "off_window")
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
    source: Mapped[str] = mapped_column(sa_enum(*LEAGUE_SOURCES, name="alias_source"))
    alias: Mapped[str] = mapped_column(String(120))


# --- matches ----------------------------------------------------------------

class Match(Base):
    __tablename__ = "matches"
    __table_args__ = (
        Index("ix_matches_date_league", "date_utc", "league"),
        UniqueConstraint("toa_event_id", name="uq_matches_toa_event"),
        # one home/away pairing per league season; UCL teams can meet in
        # several stages, so UCL rows carry a stage and the pairing unique
        # includes it (partial unique indexes: SQLite treats NULLs as
        # distinct, so the league case needs its own index)
        Index("uq_matches_league_pairing", "league", "season", "home_team_id",
              "away_team_id", unique=True,
              sqlite_where=sa_text("stage IS NULL")),
        Index("uq_matches_ucl_pairing", "league", "season", "stage",
              "home_team_id", "away_team_id", unique=True,
              sqlite_where=sa_text("stage IS NOT NULL")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    league: Mapped[str] = mapped_column(String(40))
    season: Mapped[int] = mapped_column(Integer)  # e.g. 2026 = 2026/27
    date_utc: Mapped[dt.date] = mapped_column(Date)
    # authoritative kickoff: TOA commence_time (docs/decisions.md, S5)
    commence_time_utc: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    kickoff_source: Mapped[str] = mapped_column(
        sa_enum(*KICKOFF_SOURCES, name="kickoff_source"))
    status: Mapped[str] = mapped_column(
        sa_enum(*MATCH_STATUSES, name="match_status"), default="scheduled")

    home_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    away_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    home_team: Mapped[Team] = relationship(foreign_keys=[home_team_id])
    away_team: Mapped[Team] = relationship(foreign_keys=[away_team_id])

    # UCL stage (league_phase, r16, ...) — NULL for league matches
    stage: Mapped[str | None] = mapped_column(String(30))
    # S12: postponement must never vanish through a polling loop — the
    # pre-shift kickoff and the moment the shift was detected are kept
    original_kickoff_utc: Mapped[dt.datetime | None] = mapped_column(
        UTCDateTime)
    rescheduled_at_utc: Mapped[dt.datetime | None] = mapped_column(
        UTCDateTime)
    # external ids: understat game id; TOA event id (unique). football-data
    # rows are identified by (date, home/away aliases) — no stable id.
    understat_id: Mapped[int | None] = mapped_column(Integer, unique=True)
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
        # TOA ids are required for toa-sourced snapshots; manual Fortuna
        # input has none
        CheckConstraint(
            "source = 'manual_fortuna' OR "
            "(toa_sport_key IS NOT NULL AND toa_event_id IS NOT NULL)",
            name="ck_odds_snapshots_toa_ids"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    toa_sport_key: Mapped[str | None] = mapped_column(String(40))
    toa_event_id: Mapped[str | None] = mapped_column(String(60))
    match_id: Mapped[int | None] = mapped_column(ForeignKey("matches.id"))
    bookmaker: Mapped[str] = mapped_column(String(60))
    market: Mapped[str] = mapped_column(sa_enum(*ODD_MARKETS, name="odd_market"))
    # line for totals/alternate_totals (2.5 etc.); NULL for btts/correct_score
    line: Mapped[Decimal | None] = mapped_column(Numeric(4, 2))
    # 'Over'/'Under'/'Yes'/'No', or 'H2|A0' for correct_score outcomes
    outcome: Mapped[str] = mapped_column(String(40))
    price: Mapped[Decimal] = mapped_column(Numeric(8, 3))
    tier: Mapped[str] = mapped_column(sa_enum(*ODD_TIERS, name="odd_tier"))
    # phase-5 firing discipline (docs/decisions.md, final round S1)
    planned_offset_min: Mapped[int | None] = mapped_column(Integer)
    actual_offset_min: Mapped[int | None] = mapped_column(Integer)
    # true when the degradation ladder downgraded T-3 from per-kickoff-slot
    # to per-day (docs/decisions.md, final round S3)
    t3_day_degraded: Mapped[bool] = mapped_column(Boolean, default=False,
                                                  nullable=False)
    source: Mapped[str] = mapped_column(sa_enum(*ODD_SOURCES, name="odd_source"))
    captured_at_utc: Mapped[dt.datetime] = mapped_column(UTCDateTime)


# --- availability & prefilter ----------------------------------------------

class PlayerAvailability(Base):
    __tablename__ = "player_availability"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), index=True)
    match_id: Mapped[int | None] = mapped_column(ForeignKey("matches.id"))
    player: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(
        sa_enum(*PLAYER_STATUSES, name="player_status"))
    source: Mapped[str] = mapped_column(
        sa_enum(*PLAYER_SOURCES, name="player_source"))
    source_url: Mapped[str | None] = mapped_column(String(500))
    quote_summary: Mapped[str | None] = mapped_column(Text)
    detected_at_utc: Mapped[dt.datetime] = mapped_column(UTCDateTime)


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
        # shadow_llm recommendations must reference their LLM call
        CheckConstraint("variant != 'shadow_llm' OR llm_call_id IS NOT NULL",
                        name="ck_recommendations_shadow_llm_call"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), index=True)
    market: Mapped[str] = mapped_column(sa_enum(*RECO_MARKETS, name="reco_market"))
    line: Mapped[Decimal | None] = mapped_column(Numeric(4, 2))
    outcome: Mapped[str] = mapped_column(String(40))
    # multiscore: JSON array like [[1,0],[2,0],[2,1]]; goals_range: lo-hi
    scores_set: Mapped[str | None] = mapped_column(Text)
    goals_range_lo: Mapped[int | None] = mapped_column(Integer)
    goals_range_hi: Mapped[int | None] = mapped_column(Integer)
    bookmaker: Mapped[str | None] = mapped_column(String(60))
    odds: Mapped[Decimal] = mapped_column(Numeric(8, 3))
    p_model: Mapped[Decimal] = mapped_column(Numeric(6, 4))
    p_market_devig: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    edge: Mapped[Decimal] = mapped_column(Numeric(6, 4))
    kelly_quarter_capped: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    flags: Mapped[str] = mapped_column(String(200), default="")
    variant: Mapped[str] = mapped_column(
        sa_enum(*RECO_VARIANTS, name="reco_variant"), default="base")
    # CLV provenance per docs/decisions.md (CLV metric definition)
    clv: Mapped[Decimal | None] = mapped_column(Numeric(7, 4))
    clv_source: Mapped[str | None] = mapped_column(
        sa_enum(*CLV_SOURCES, name="clv_source"))
    # final edge is computed against Fortuna (manual input until it appears
    # in an API) — docs/decisions.md (S10)
    final_odds_source: Mapped[str | None] = mapped_column(
        sa_enum(*FINAL_ODDS_SOURCES, name="final_odds_source"))
    llm_call_id: Mapped[int | None] = mapped_column(ForeignKey("llm_calls.id"))
    created_at_utc: Mapped[dt.datetime] = mapped_column(UTCDateTime)


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
    # reserved -> settled | failed (B1); imported for one-time backfills
    status: Mapped[str] = mapped_column(String(10), default="settled")
    created_at_utc: Mapped[dt.datetime] = mapped_column(UTCDateTime)


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
    updated_at_utc: Mapped[dt.datetime] = mapped_column(UTCDateTime)


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
    created_at_utc: Mapped[dt.datetime] = mapped_column(UTCDateTime)


class MatchLineup(Base):
    """Per-match player usage (Understat): starters vs subs + minutes.

    Feeds the phase-5 key-player rule (>=70% starts in last 8 matches).
    """

    __tablename__ = "match_lineups"
    __table_args__ = (
        UniqueConstraint("match_id", "team_id", "player",
                         name="uq_match_lineups_row"),
        UniqueConstraint("match_id", "understat_player_id",
                         name="uq_match_lineups_understat_pid"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), index=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), index=True)
    player: Mapped[str] = mapped_column(String(120))
    understat_player_id: Mapped[int | None] = mapped_column(Integer)
    # Understat marks substitutes with position == 'Sub' (phase-0 finding)
    started: Mapped[bool] = mapped_column(Boolean, nullable=False)
    minutes: Mapped[int | None] = mapped_column(Integer)


class ClosingOdds(Base):
    """football-data closing O/U 2.5 prices — the CLV reference (decisions)."""

    __tablename__ = "closing_odds"
    __table_args__ = (
        UniqueConstraint("match_id", "bookmaker",
                         name="uq_closing_odds_match_book"),
        CheckConstraint("bookmaker IN ('AvgC', 'MaxC', 'PC')",
                        name="ck_closing_odds_bookmaker"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), index=True)
    bookmaker: Mapped[str] = mapped_column(String(10))
    over_price: Mapped[Decimal | None] = mapped_column(Numeric(8, 3))
    under_price: Mapped[Decimal | None] = mapped_column(Numeric(8, 3))
