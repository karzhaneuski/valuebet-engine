"""Persistence layer and market data loading for Fair Pricing and Recommendations.

Handles:
1. Storing recommendations into the `recommendations` table with idempotency key
   (match_id, market, outcome, variant, input_hash).
2. Loading market inputs for a match from `odds_snapshots` (TOA) and `prematch_odds`.
3. Finding matches by name with ambiguous candidate listing.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.orm import Match, PrematchOdds, Recommendation, Team, TeamAlias
from core.value.engine import EvaluationResult
from core.value.fair_matrix import (
    MarketInputs,
    MarketOdds1X2,
    MarketTotalsLine,
)

logger = logging.getLogger(__name__)


def save_recommendations_to_db(
    session: Session,
    match_id: int,
    result: EvaluationResult,
    variant: str = "base",
    created_at_utc: dt.datetime | None = None,
) -> int:
    """Save recommended outcomes into the recommendations table with idempotency.

    Idempotency key: (match_id, market, outcome, variant, input_hash).
    If a recommendation already exists for this outcome and input state, updates
    its odds and edge rather than inserting a duplicate.
    Returns the number of saved/updated recommendations.
    """
    now = created_at_utc or dt.datetime.now(dt.UTC)
    saved_count = 0

    for out in result.outcomes:
        if not out.recommended:
            continue

        raw = out.raw_price
        outcome_disp = "other" if out.market == "other" else out.selection_display
        market_val = "exact_score" if out.market == "other" else out.market

        # Format scores_set or goals range
        scores_set_json: str | None = None
        goals_lo: int | None = None
        goals_hi: int | None = None
        line_val: Decimal | None = None

        if market_val == "multiscore" and isinstance(raw.selection, (frozenset, set, list)):
            scores_list = [[sc[0], sc[1]] for sc in sorted(raw.selection)]
            scores_set_json = json.dumps(scores_list)
        elif market_val == "goals_range" and isinstance(raw.selection, tuple):
            goals_lo = int(raw.selection[0])
            goals_hi = int(raw.selection[1])

        # Idempotency lookup
        existing = session.scalar(
            select(Recommendation).where(
                Recommendation.match_id == match_id,
                Recommendation.market == market_val,
                Recommendation.outcome == outcome_disp,
                Recommendation.variant == variant,
                Recommendation.input_hash == result.input_hash,
            )
        )

        d_odds = Decimal(f"{out.odds_fortuna:.3f}")
        d_p_model = Decimal(f"{out.p_fair:.4f}")
        d_p_market = Decimal(f"{out.p_fair:.4f}")
        d_edge = Decimal(f"{out.edge:.4f}")
        d_kelly = Decimal(f"{out.kelly_quarter_capped:.4f}")
        flags_str = ", ".join(out.flags)

        if existing is not None:
            existing.odds = d_odds
            existing.p_model = d_p_model
            existing.p_market_devig = d_p_market
            existing.edge = d_edge
            existing.kelly_quarter_capped = d_kelly
            existing.flags = flags_str
            existing.created_at_utc = now
        else:
            rec = Recommendation(
                match_id=match_id,
                market=market_val,
                line=line_val,
                outcome=outcome_disp,
                scores_set=scores_set_json,
                goals_range_lo=goals_lo,
                goals_range_hi=goals_hi,
                bookmaker="fortuna",
                odds=d_odds,
                p_model=d_p_model,
                p_market_devig=d_p_market,
                edge=d_edge,
                kelly_quarter_capped=d_kelly,
                flags=flags_str,
                variant=variant,
                matrix_source=result.source,
                input_hash=result.input_hash,
                final_odds_source="fortuna_manual",
                clv=None,
                clv_source=None,
                created_at_utc=now,
            )
            session.add(rec)
        saved_count += 1

    session.flush()
    return saved_count


def load_market_inputs_for_match(
    session: Session,
    match_id: int,
    as_of: dt.datetime | None = None,
) -> MarketInputs:
    """Load latest market totals and 1X2 lines for a match before as_of cutoff.

    Checks:
    1. odds_snapshots (TOA live snapshots).
    2. prematch_odds (football-data pre-match odds; 'P' = Pinnacle, 'Avg' = Consensus).
    If no valid 1X2 odds are found, returns empty MarketInputs (which triggers status='no_market').
    """
    match = session.get(Match, match_id)
    if match is None:
        raise ValueError(f"Match ID {match_id} not found in database.")

    end_of_day = dt.datetime.combine(match.date_utc, dt.time(23, 59))
    cutoff = as_of or match.commence_time_utc or end_of_day

    # 1. Check prematch_odds (football-data)
    # Bookmaker 'P' is Pinnacle, 'Avg' is consensus
    fd_odds = list(
        session.scalars(
            select(PrematchOdds)
            .where(PrematchOdds.match_id == match_id)
            .order_by(PrematchOdds.bookmaker == "P", PrematchOdds.bookmaker == "Avg")
        ).all()
    )

    totals_by_book: dict[str, list[MarketTotalsLine]] = {}
    h2h_by_book: dict[str, MarketOdds1X2] = {}

    for row in fd_odds:
        b_key = "pinnacle" if row.bookmaker == "P" else "consensus"
        if row.over_price is not None and row.under_price is not None:
            totals_by_book[b_key] = [
                MarketTotalsLine(
                    line=2.5,
                    over=float(row.over_price),
                    under=float(row.under_price),
                )
            ]
        if (
            row.home_price is not None
            and row.draw_price is not None
            and row.away_price is not None
        ):
            h2h_by_book[b_key] = MarketOdds1X2(
                home=float(row.home_price),
                draw=float(row.draw_price),
                away=float(row.away_price),
            )

    # Determine match name for metadata
    home_name = match.home_team.name if match.home_team else f"Team_{match.home_team_id}"
    away_name = match.away_team.name if match.away_team else f"Team_{match.away_team_id}"
    m_name = f"{home_name} - {away_name}"

    if totals_by_book and h2h_by_book:
        return MarketInputs(
            source="fd_prematch",
            snapshot_time=cutoff,
            totals_by_book=totals_by_book,
            h2h_by_book=h2h_by_book,
            match_name=m_name,
        )

    # Fallback / Empty market
    return MarketInputs(
        source="toa_snapshot",
        snapshot_time=cutoff,
        totals_by_book=totals_by_book,
        h2h_by_book=h2h_by_book,
        match_name=m_name,
    )


def find_matches_by_query(
    session: Session,
    query: str,
    match_date: dt.date | None = None,
) -> list[Match]:
    """Find matches matching a team query (e.g. 'Arsenal - Everton', 'Chelsea vs Spurs').

    Splits query by standard delimiters ('-', '—', '–', 'vs', 'v').
    Searches team names and aliases with case-insensitive matching.
    """
    delims = [" - ", " — ", " – ", " vs ", " vs. ", " v "]
    home_part = ""
    away_part = ""

    for d in delims:
        if d in query:
            parts = query.split(d, 1)
            home_part = parts[0].strip()
            away_part = parts[1].strip()
            break

    if not home_part or not away_part:
        if "-" in query:
            parts = query.split("-", 1)
            home_part = parts[0].strip()
            away_part = parts[1].strip()
        else:
            home_part = query.strip()
            away_part = ""

    def _resolve_team_ids(name_str: str) -> set[int]:
        if not name_str:
            return set()
        clean = name_str.lower()
        # Direct name match
        t_ids = set(
            session.scalars(
                select(Team.id).where(func.lower(Team.name).contains(clean))
            ).all()
        )
        # Alias match
        a_ids = set(
            session.scalars(
                select(TeamAlias.team_id).where(func.lower(TeamAlias.alias).contains(clean))
            ).all()
        )
        return t_ids | a_ids

    home_ids = _resolve_team_ids(home_part)
    away_ids = _resolve_team_ids(away_part) if away_part else set()

    stmt = select(Match)

    if home_ids and away_ids:
        stmt = stmt.where(
            Match.home_team_id.in_(home_ids),
            Match.away_team_id.in_(away_ids),
        )
    elif home_ids:
        stmt = stmt.where(
            (Match.home_team_id.in_(home_ids)) | (Match.away_team_id.in_(home_ids))
        )
    else:
        return []

    if match_date is not None:
        stmt = stmt.where(Match.date_utc == match_date)

    stmt = stmt.order_by(Match.date_utc.desc())
    return list(session.scalars(stmt).all())
