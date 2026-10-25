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

from core.orm import Match, OddsSnapshot, PrematchOdds, Recommendation, Team, TeamAlias
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
    """Save recommended outcomes into the recommendations table with lifecycle management.

    Active unique key: (match_id, market, selection_key, variant) WHERE status = 'active'.
    Lifecycle transitions:
    - If recommendation is already active and (odds, edge) unchanged: preserve row
      (idempotent, no update to created_at or odds).
    - If price or market changed and still passes: mark old row as 'superseded',
      insert new row as 'active'.
    - If price dropped below edge or outcome no longer passes: mark old active row as 'withdrawn'.
    - Never overwrite created_at_utc or initial odds of historical recommendations.

    Returns the count of active recommendations.
    """
    now = created_at_utc or dt.datetime.now(dt.UTC)
    active_count = 0

    # 1. Fetch current active recommendations for this match & variant
    active_rows = list(
        session.scalars(
            select(Recommendation).where(
                Recommendation.match_id == match_id,
                Recommendation.variant == variant,
                Recommendation.status == "active",
            )
        ).all()
    )
    active_by_key: dict[str, Recommendation] = {r.selection_key: r for r in active_rows}

    # 2. Process all evaluated outcomes
    for out in result.outcomes:
        raw = out.raw_price
        sel_key = out.selection_key or (
            raw.selection_key if raw else f"{out.market}:{out.selection_display}"
        )
        outcome_disp = "other" if out.market == "other" else out.selection_display
        market_val = "exact_score" if out.market == "other" else out.market

        scores_set_json: str | None = None
        goals_lo: int | None = None
        goals_hi: int | None = None
        line_val: Decimal | None = None

        if raw is not None:
            if market_val == "multiscore" and isinstance(raw.selection, (frozenset, set, list)):
                scores_list = [[sc[0], sc[1]] for sc in sorted(raw.selection)]
                scores_set_json = json.dumps(scores_list)
            elif (
                market_val == "goals_range"
                and isinstance(raw.selection, tuple)
                and len(raw.selection) == 2
            ):
                goals_lo = int(raw.selection[0])
                goals_hi = int(raw.selection[1])

        if out.recommended:
            # Check if there is already an active recommendation for this key
            if sel_key in active_by_key:
                curr = active_by_key.pop(sel_key)
                odds_diff = abs(float(curr.odds) - out.odds_fortuna)
                edge_diff = abs(float(curr.edge) - out.edge)
                if odds_diff < 1e-4 and edge_diff < 1e-4 and curr.input_hash == result.input_hash:
                    # Exactly identical, preserve as active without changing created_at or odds
                    active_count += 1
                    continue

                # Price or market changed, but still recommended: mark old as superseded
                curr.status = "superseded"

            # Pure market matrix has p_model = None (probabilities come from market)
            # Fallback matrix has p_market_devig = None (probabilities come from model)
            is_model = result.source == "model_fallback"
            d_p_model = Decimal(f"{out.p_fair:.4f}") if is_model else None
            d_p_market = None if is_model else Decimal(f"{out.p_fair:.4f}")

            rec = Recommendation(
                match_id=match_id,
                market=market_val,
                selection_key=sel_key,
                status="active",
                line=line_val,
                outcome=outcome_disp,
                scores_set=scores_set_json,
                goals_range_lo=goals_lo,
                goals_range_hi=goals_hi,
                bookmaker="fortuna",
                odds=Decimal(f"{out.odds_fortuna:.3f}"),
                p_model=d_p_model,
                p_market_devig=d_p_market,
                edge=Decimal(f"{out.edge:.4f}"),
                kelly_quarter_capped=Decimal(f"{out.kelly_quarter_capped:.4f}"),
                flags=", ".join(out.flags),
                variant=variant,
                matrix_source=result.source,
                input_hash=result.input_hash,
                final_odds_source="fortuna_manual",
                clv=None,
                clv_source=None,
                created_at_utc=now,
            )
            session.add(rec)
            active_count += 1
        else:
            # Outcome is NOT recommended. If it was active, withdraw it
            if sel_key in active_by_key:
                curr = active_by_key.pop(sel_key)
                curr.status = "withdrawn"

    # 3. Any active recommendations for keys that were not evaluated in this run -> mark withdrawn
    for remaining in active_by_key.values():
        remaining.status = "withdrawn"

    session.flush()
    return active_count


def load_market_inputs_for_match(
    session: Session,
    match_id: int,
    as_of: dt.datetime | None = None,
) -> MarketInputs:
    """Load latest market totals and 1X2 lines for a match before as_of cutoff.

    Checks:
    1. odds_snapshots (TOA live snapshots <= cutoff).
    2. prematch_odds (football-data pre-match odds; 'P' = Pinnacle, 'Avg' = Consensus;
       explicitly ignores 'Max').
    If no valid 1X2 odds are found, returns empty MarketInputs with specific reason
    ('нет 1X2 в снимках TOA — появится в фазе 7' or 'нет котировок 1X2 и тоталов в базе данных').
    """
    match = session.get(Match, match_id)
    if match is None:
        raise ValueError(f"Match ID {match_id} not found in database.")

    end_of_day = dt.datetime.combine(match.date_utc, dt.time(23, 59))
    cutoff = as_of or match.commence_time_utc or end_of_day

    # Determine match name for metadata
    home_name = match.home_team.name if match.home_team else f"Team_{match.home_team_id}"
    away_name = match.away_team.name if match.away_team else f"Team_{match.away_team_id}"
    m_name = f"{home_name} - {away_name}"

    # 1. Check odds_snapshots (TOA live snapshots <= cutoff)
    toa_snaps = list(
        session.scalars(
            select(OddsSnapshot)
            .where(
                OddsSnapshot.match_id == match_id,
                OddsSnapshot.captured_at_utc <= cutoff,
            )
            .order_by(OddsSnapshot.captured_at_utc.desc())
        ).all()
    )

    # 2. Check prematch_odds (football-data pre-match odds)
    # Bookmaker 'P' is Pinnacle, 'Avg' is consensus. Explicitly ignore 'Max'!
    fd_odds = list(
        session.scalars(
            select(PrematchOdds)
            .where(
                PrematchOdds.match_id == match_id,
                PrematchOdds.bookmaker.in_(["P", "Avg"]),
            )
        ).all()
    )

    totals_by_book: dict[str, list[MarketTotalsLine]] = {}
    h2h_by_book: dict[str, MarketOdds1X2] = {}

    # Deterministic sort: Pinnacle ('P') first, then Consensus ('Avg')
    fd_odds.sort(key=lambda r: (0 if r.bookmaker == "P" else 1, r.bookmaker))

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

    if totals_by_book and h2h_by_book:
        # For football-data, exact snapshot time is unknown (decisions S10 / SPEC §4)
        return MarketInputs(
            source="fd_prematch",
            snapshot_time=None,  # Real snapshot age: "время снимка неизвестно"
            totals_by_book=totals_by_book,
            h2h_by_book=h2h_by_book,
            match_name=m_name,
        )

    # Fallback / Empty market with explicit diagnostic reason
    if toa_snaps and not h2h_by_book:
        logger.info(
            "Match %d: Found %d TOA snapshots, but no 1X2 market (TOA 1X2 scheduled for Phase 7)",
            match_id,
            len(toa_snaps),
        )

    return MarketInputs(
        source="toa_snapshot",
        snapshot_time=None,
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
