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
                kelly_quarter_capped=Decimal("0.0000"),
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
    if as_of is not None:
        if match.commence_time_utc and as_of == match.commence_time_utc:
            time_cond = OddsSnapshot.captured_at_utc < as_of
        else:
            time_cond = OddsSnapshot.captured_at_utc <= as_of
    elif match.commence_time_utc:
        time_cond = OddsSnapshot.captured_at_utc < match.commence_time_utc
    else:
        time_cond = OddsSnapshot.captured_at_utc <= end_of_day

    # Determine match name for metadata
    home_name = match.home_team.name if match.home_team else f"Team_{match.home_team_id}"
    away_name = match.away_team.name if match.away_team else f"Team_{match.away_team_id}"
    m_name = f"{home_name} - {away_name}"

    # 1. Check odds_snapshots (TOA live snapshots)
    toa_where = [
        (OddsSnapshot.match_id == match_id)
        | (OddsSnapshot.toa_event_id == match.toa_event_id if match.toa_event_id else False),
        time_cond,
        OddsSnapshot.source == "toa",
    ]
    toa_snaps = list(
        session.scalars(
            select(OddsSnapshot)
            .where(*toa_where)
            .order_by(OddsSnapshot.captured_at_utc.desc())
        ).all()
    )

    if toa_snaps:
        toa_totals: dict[str, list[MarketTotalsLine]] = {}
        toa_h2h: dict[str, MarketOdds1X2] = {}
        bookmakers = {s.bookmaker for s in toa_snaps}
        latest_capture_time: dt.datetime | None = None

        for bk in bookmakers:
            bk_snaps = [s for s in toa_snaps if s.bookmaker == bk]
            totals_by_line: dict[float, dict[str, float]] = {}
            for s in bk_snaps:
                if s.market == "totals" and s.line is not None:
                    l_val = float(s.line)
                    out_dir = "over" if s.outcome.strip().lower() in ("over", "o") else "under"
                    totals_by_line.setdefault(l_val, {})
                    if out_dir not in totals_by_line[l_val]:
                        totals_by_line[l_val][out_dir] = float(s.price)
                        if latest_capture_time is None or s.captured_at_utc > latest_capture_time:
                            latest_capture_time = s.captured_at_utc

            lines_list: list[MarketTotalsLine] = []
            for l_val, prices in totals_by_line.items():
                if "over" in prices and "under" in prices:
                    lines_list.append(
                        MarketTotalsLine(line=l_val, over=prices["over"], under=prices["under"])
                    )
            if lines_list:
                toa_totals[bk] = lines_list

            h2h_prices: dict[str, float] = {}
            for s in bk_snaps:
                if s.market == "h2h":
                    out_name = s.outcome.strip().lower()
                    if out_name in ("home", "1") or (
                        match.home_team and s.outcome == match.home_team.name
                    ):
                        if "home" not in h2h_prices:
                            h2h_prices["home"] = float(s.price)
                    elif out_name in ("draw", "x"):
                        if "draw" not in h2h_prices:
                            h2h_prices["draw"] = float(s.price)
                    elif (
                        out_name in ("away", "2")
                        or (match.away_team and s.outcome == match.away_team.name)
                    ) and "away" not in h2h_prices:
                        h2h_prices["away"] = float(s.price)
                    if latest_capture_time is None or s.captured_at_utc > latest_capture_time:
                        latest_capture_time = s.captured_at_utc

            if "home" in h2h_prices and "draw" in h2h_prices and "away" in h2h_prices:
                toa_h2h[bk] = MarketOdds1X2(
                    home=h2h_prices["home"],
                    draw=h2h_prices["draw"],
                    away=h2h_prices["away"],
                )

        common_books = set(toa_totals.keys()) & set(toa_h2h.keys())
        if common_books:
            filt_totals = {b: toa_totals[b] for b in common_books}
            filt_h2h = {b: toa_h2h[b] for b in common_books}
            return MarketInputs(
                source="toa_snapshot",
                snapshot_time=latest_capture_time,
                totals_by_book=filt_totals,
                h2h_by_book=filt_h2h,
                match_name=m_name,
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
