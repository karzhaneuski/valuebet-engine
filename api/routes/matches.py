"""FastAPI routes for matches and fair market pricing."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.auth import verify_bearer_token
from api.db import get_db
from api.matching import search_matches_by_query
from api.schemas import (
    BTTSOdds,
    ExactScorePrice,
    FairPricesResponse,
    GoalsRangeItem,
    H2HOdds,
    MatchListResponse,
    MatchSummary,
    MultiscoreItem,
    TeamTotalsItem,
    TotalsLinePrice,
)
from core.orm import ClosingOdds, Match, OddsSnapshot, PrematchOdds
from core.tracking.clv import evaluate_fair_probs_for_bet
from core.value.fair_matrix import build_fair_matrix
from core.value.persistence import load_market_inputs_for_match

router = APIRouter(tags=["matches"])

STANDARD_MULTISCORES: list[tuple[str, list[tuple[int, int]]]] = [
    ("1:0, 2:0, 3:0", [(1, 0), (2, 0), (3, 0)]),
    ("0:1, 0:2, 0:3", [(0, 1), (0, 2), (0, 3)]),
    ("0:0, 1:1, 2:2", [(0, 0), (1, 1), (2, 2)]),
    ("2:1, 3:1, 4:1", [(2, 1), (3, 1), (4, 1)]),
    ("1:2, 1:3, 1:4", [(1, 2), (1, 3), (1, 4)]),
]

STANDARD_GOALS_RANGES: list[tuple[str, int, int]] = [
    ("0-1", 0, 1),
    ("2-3", 2, 3),
    ("4-6", 4, 6),
    ("7+", 7, 20),
]

STANDARD_TOTALS_LINES = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.5]
STANDARD_TEAM_TOTALS_LINES = [0.5, 1.5, 2.5]


def _check_has_lines(db: Session, match_id: int) -> bool:
    has_closing = db.scalar(
        select(func.count(ClosingOdds.id)).where(ClosingOdds.match_id == match_id)
    )
    if has_closing and has_closing > 0:
        return True
    has_prematch = db.scalar(
        select(func.count(PrematchOdds.id)).where(PrematchOdds.match_id == match_id)
    )
    if has_prematch and has_prematch > 0:
        return True
    has_snap = db.scalar(
        select(func.count(OddsSnapshot.id)).where(OddsSnapshot.match_id == match_id)
    )
    return bool(has_snap and has_snap > 0)


@router.get("/matches", response_model=MatchListResponse)
def list_matches(
    days: int = Query(7, ge=1, le=60, description="Horizon in days from today"),
    league: str | None = Query(None, description="League filter (e.g. epl, laliga)"),
    status_filter: str = Query(
        "scheduled", alias="status", description="Match status (scheduled, finished, all)"
    ),
    search: str | None = Query(
        None, description="Team search query (e.g. 'Arsenal - Chelsea' or 'Liverpool')"
    ),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    auth_token: str = Depends(verify_bearer_token),
) -> MatchListResponse:
    """Retrieve football matches with market lines status and filtering."""
    if search:
        matches = list(search_matches_by_query(db, search, status=status_filter, league=league))
        total = len(matches)
        paged_matches = matches[offset : offset + limit]
    else:
        stmt = select(Match)
        if status_filter != "all":
            stmt = stmt.where(Match.status == status_filter)
        if league:
            stmt = stmt.where(Match.league == league)
        if status_filter == "scheduled":
            today = dt.date.today()
            max_date = today + dt.timedelta(days=days)
            stmt = stmt.where(Match.date_utc >= today, Match.date_utc <= max_date)

        total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        stmt = stmt.order_by(Match.date_utc.asc(), Match.commence_time_utc.asc())
        stmt = stmt.offset(offset).limit(limit)
        paged_matches = list(db.scalars(stmt).all())

    summaries: list[MatchSummary] = []
    for m in paged_matches:
        h_name = m.home_team.name if m.home_team else f"Team_{m.home_team_id}"
        a_name = m.away_team.name if m.away_team else f"Team_{m.away_team_id}"
        has_lines = _check_has_lines(db, m.id)
        avail = ["1X2", "totals", "exact_score", "btts"] if has_lines else []
        summaries.append(
            MatchSummary(
                id=m.id,
                league=m.league,
                season=m.season,
                home_team=h_name,
                away_team=a_name,
                date_utc=m.date_utc,
                kickoff_utc=m.commence_time_utc,
                status=m.status,
                has_market_lines=has_lines,
                available_markets=avail,
            )
        )

    return MatchListResponse(matches=summaries, total=total)


@router.get("/matches/{match_id}/fair-prices", response_model=FairPricesResponse)
def get_match_fair_prices(
    match_id: int,
    db: Session = Depends(get_db),
    auth_token: str = Depends(verify_bearer_token),
) -> FairPricesResponse:
    """Compute fair probability distribution and fair odds across all markets for a match."""
    match = db.get(Match, match_id)
    if match is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Матч с ID {match_id} не найден в базе данных",
        )

    h_name = match.home_team.name if match.home_team else f"Team_{match.home_team_id}"
    a_name = match.away_team.name if match.away_team else f"Team_{match.away_team_id}"

    inputs = load_market_inputs_for_match(db, match.id)
    if inputs is None or inputs.is_empty():
        msg = f"Котировки для матча '{h_name} - {a_name}' пока не загружены"
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=msg,
        )

    fair_matrix = build_fair_matrix(inputs, home_team=h_name, away_team=a_name)

    # 1. Exact Scores (0..4) and inny
    exact_scores: list[ExactScorePrice] = []
    p_grid = 0.0
    for h in range(5):
        for a in range(5):
            p = fair_matrix.exact_score(h, a)
            p_grid += p
            fo = round(1.0 / p, 2) if p > 0.0001 else 999.0
            exact_scores.append(ExactScorePrice(score=f"{h}:{a}", prob=round(p, 4), fair_odds=fo))

    p_inny = max(0.0, 1.0 - p_grid)
    fo_inny = round(1.0 / p_inny, 2) if p_inny > 0.0001 else 999.0
    inny_price = ExactScorePrice(score="inny (>4 голов)", prob=round(p_inny, 4), fair_odds=fo_inny)

    # 2. Multiscores
    multiscores: list[MultiscoreItem] = []
    for label, tuples in STANDARD_MULTISCORES:
        p = fair_matrix.multiscore(tuples)
        fo = round(1.0 / p, 2) if p > 0.0001 else 999.0
        multiscores.append(MultiscoreItem(combination=label, prob=round(p, 4), fair_odds=fo))

    # 3. Goals ranges
    goals_ranges: list[GoalsRangeItem] = []
    for label, lo, hi in STANDARD_GOALS_RANGES:
        p = sum(
            fair_matrix.exact_score(h, a)
            for h in range(11)
            for a in range(11)
            if lo <= h + a <= hi
        )
        fo = round(1.0 / p, 2) if p > 0.0001 else 999.0
        goals_ranges.append(GoalsRangeItem(range=label, prob=round(p, 4), fair_odds=fo))

    # 4. Totals lines
    totals: list[TotalsLinePrice] = []
    for line in STANDARD_TOTALS_LINES:
        p_over, p_push_over = evaluate_fair_probs_for_bet(
            fair_matrix, "totals", "over", Decimal(str(line))
        )
        p_under, p_push_under = evaluate_fair_probs_for_bet(
            fair_matrix, "totals", "under", Decimal(str(line))
        )
        fo_over = (
            round((1.0 - p_push_over) / p_over, 2)
            if p_over > 0.0001
            else 999.0
        )
        fo_under = (
            round((1.0 - p_push_under) / p_under, 2)
            if p_under > 0.0001
            else 999.0
        )
        totals.append(
            TotalsLinePrice(
                line=line,
                over_prob=round(p_over, 4),
                under_prob=round(p_under, 4),
                push_prob=round(p_push_over, 4),
                over_fair_odds=fo_over,
                under_fair_odds=fo_under,
            )
        )

    # 5. BTTS
    p_btts_yes = fair_matrix.btts(yes=True)
    p_btts_no = fair_matrix.btts(yes=False)
    fo_btts_yes = round(1.0 / p_btts_yes, 2) if p_btts_yes > 0.0001 else 999.0
    fo_btts_no = round(1.0 / p_btts_no, 2) if p_btts_no > 0.0001 else 999.0
    btts = BTTSOdds(
        yes_prob=round(p_btts_yes, 4),
        yes_fair_odds=fo_btts_yes,
        no_prob=round(p_btts_no, 4),
        no_fair_odds=fo_btts_no,
    )

    # 6. H2H (1X2)
    p_h, _ = evaluate_fair_probs_for_bet(fair_matrix, "h2h", "1", None)
    p_d, _ = evaluate_fair_probs_for_bet(fair_matrix, "h2h", "X", None)
    p_a, _ = evaluate_fair_probs_for_bet(fair_matrix, "h2h", "2", None)
    h2h = H2HOdds(
        home_prob=round(p_h, 4),
        home_fair_odds=round(1.0 / p_h, 2) if p_h > 0.0001 else 999.0,
        draw_prob=round(p_d, 4),
        draw_fair_odds=round(1.0 / p_d, 2) if p_d > 0.0001 else 999.0,
        away_prob=round(p_a, 4),
        away_fair_odds=round(1.0 / p_a, 2) if p_a > 0.0001 else 999.0,
    )

    # 7. Team Totals
    team_totals: list[TeamTotalsItem] = []
    for team_label in ("home", "away"):
        for line in STANDARD_TEAM_TOTALS_LINES:
            sel_over = f"{team_label}_over"
            sel_under = f"{team_label}_under"
            p_o, push_o = evaluate_fair_probs_for_bet(
                fair_matrix, "team_totals", sel_over, Decimal(str(line))
            )
            p_u, push_u = evaluate_fair_probs_for_bet(
                fair_matrix, "team_totals", sel_under, Decimal(str(line))
            )
            fo_o = round((1.0 - push_o) / p_o, 2) if p_o > 0.0001 else 999.0
            fo_u = round((1.0 - push_u) / p_u, 2) if p_u > 0.0001 else 999.0
            team_totals.append(
                TeamTotalsItem(
                    team=h_name if team_label == "home" else a_name,
                    line=line,
                    over_prob=round(p_o, 4),
                    under_prob=round(p_u, 4),
                    push_prob=round(push_o, 4),
                    over_fair_odds=fo_o,
                    under_fair_odds=fo_u,
                )
            )

    caveats = [
        "Справедливая матрица построена из девигованных котировок острых букмекеров.",
        "Bivariate Poisson с ρ = 0 (неотличимо от ρ из рыночной ничьей на валидации n=3504).",
        "Инфляция вероятности ничьих κ_high = 1.05 применена на диагонали от 2:2 и выше.",
        "Вероятность исхода 'inny' (>4 голов) рассчитана как остаток вероятности сетки 5x5.",
        "Для целочисленных тоталов справедливый коэффициент учитывает push: (1 - P(push))/P(win).",
    ]

    return FairPricesResponse(
        match_id=match.id,
        home_team=h_name,
        away_team=a_name,
        kickoff_utc=match.commence_time_utc,
        league=match.league,
        exact_score=exact_scores,
        inny=inny_price,
        multiscore=multiscores,
        goals_range=goals_ranges,
        totals=totals,
        btts=btts,
        h2h=h2h,
        team_totals=team_totals,
        methodological_caveats=caveats,
        anomaly_flags=[],
    )
