"""Pre-kickoff Closing Line Value (CLV) calculation (SPEC §7, decisions S10).

Calculates CLV against the closing fair market matrix:
    CLV_fair = odds_placed * p_fair,closing + p_push * 1.0 - 1.0

Where p_fair,closing is derived from the latest market snapshot strictly BEFORE kickoff
(match.commence_time_utc), or from the devigged closing sharp quotes (football-data PC/AvgC).
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.config import AppConfig, load_config
from core.orm import ClosingOdds, Match, UserBet
from core.value.fair_matrix import (
    FairMatrix,
    MarketInputs,
    MarketOdds1X2,
    MarketTotalsLine,
    build_fair_matrix,
)
from core.value.persistence import load_market_inputs_for_match

logger = logging.getLogger(__name__)


def evaluate_fair_probs_for_bet(
    matrix: FairMatrix,
    market: str,
    selection_key: str,
    line: Decimal | None,
    cs_grid_max_goals: int = 4,
) -> tuple[float, float]:
    """Calculate fair win probability and push probability for user bet from FairMatrix.

    Returns (p_win, p_push).
    """
    m = market.strip().lower()
    sel = selection_key.strip().lower()
    p_win = 0.0
    p_push = 0.0

    if m == "exact_score":
        if sel in ("inny", "other"):
            # All scores exceeding grid (0..4 goals)
            p_grid = 0.0
            for h in range(cs_grid_max_goals + 1):
                for a in range(cs_grid_max_goals + 1):
                    p_grid += matrix.exact_score(h, a)
            p_win = max(0.0, 1.0 - p_grid)
        else:
            clean = sel.replace(":", "-").replace(" ", "")
            parts = clean.split("-")
            if len(parts) == 2:
                h_g, a_g = int(parts[0]), int(parts[1])
                p_win = matrix.exact_score(h_g, a_g)

    elif m == "multiscore":
        # Format: "1-0, 2-0, 3-0"
        score_tuples: list[tuple[int, int]] = []
        for s in sel.split(","):
            parts = s.strip().replace(":", "-").split("-")
            if len(parts) == 2:
                score_tuples.append((int(parts[0]), int(parts[1])))
        p_win = matrix.multiscore(score_tuples)

    elif m == "goals_range":
        if "+" in sel:
            min_g = int(sel.replace("+", "").strip())
            p_win = sum(
                matrix.exact_score(h, a)
                for h in range(11)
                for a in range(11)
                if h + a >= min_g
            )
        else:
            parts = sel.split("-")
            if len(parts) == 2:
                lo, hi = int(parts[0].strip()), int(parts[1].strip())
                p_win = matrix.goals_range(lo, hi)

    elif m == "totals":
        direction = "over" if "over" in sel or sel.startswith("o") else "under"
        target_line = float(line) if line is not None else 2.5
        is_integer = abs(target_line - round(target_line)) < 1e-6
        int_line = round(target_line)

        if is_integer:
            # Push when total goals exactly equal line
            p_push = sum(
                matrix.exact_score(h, a)
                for h in range(11)
                for a in range(11)
                if h + a == int_line
            )
            if direction == "over":
                p_win = sum(
                    matrix.exact_score(h, a)
                    for h in range(11)
                    for a in range(11)
                    if h + a > int_line
                )
            else:
                p_win = sum(
                    matrix.exact_score(h, a)
                    for h in range(11)
                    for a in range(11)
                    if h + a < int_line
                )
        else:
            if direction == "over":
                p_win = matrix.total_over(target_line)
            else:
                p_win = matrix.total_under(target_line)

    elif m == "btts":
        yes = "yes" in sel or sel == "y"
        p_win = matrix.btts(yes=yes)

    elif m in ("h2h", "1x2"):
        if sel in ("1", "home"):
            p_win = matrix.p_home_devig
        elif sel in ("x", "draw"):
            p_win = matrix.p_draw_devig
        elif sel in ("2", "away"):
            p_win = matrix.p_away_devig

    elif m == "team_totals":
        is_home = "home" in sel or sel.startswith("h")
        direction = "over" if "over" in sel else "under"
        target_line = float(line) if line is not None else 1.5
        is_integer = abs(target_line - round(target_line)) < 1e-6
        int_line = round(target_line)

        if is_integer:
            p_push = sum(
                matrix.exact_score(h, a)
                for h in range(11)
                for a in range(11)
                if (h if is_home else a) == int_line
            )

        marg = matrix.matrix.sum(axis=1) if is_home else matrix.matrix.sum(axis=0)
        if direction == "over":
            p_win = float(sum(marg[g] for g in range(len(marg)) if g > target_line))
        else:
            p_win = float(sum(marg[g] for g in range(len(marg)) if g < target_line))

    return p_win, p_push


def load_closing_market_inputs(
    session: Session,
    match: Match,
) -> MarketInputs | None:
    """Load closing market odds for a match.

    Prioritizes:
    1. football-data closing_odds (PC = Pinnacle closing, AvgC = Consensus closing).
    2. Latest OddsSnapshot captured strictly BEFORE kickoff (commence_time_utc).
    3. football-data prematch_odds as final fallback.
    """
    home_name = match.home_team.name if match.home_team else f"Team_{match.home_team_id}"
    away_name = match.away_team.name if match.away_team else f"Team_{match.away_team_id}"
    m_name = f"{home_name} - {away_name}"

    # 1. Check closing_odds (football-data closing)
    c_odds = list(
        session.scalars(
            select(ClosingOdds)
            .where(
                ClosingOdds.match_id == match.id,
                ClosingOdds.bookmaker.in_(["PC", "AvgC"]),
            )
        ).all()
    )

    if c_odds:
        c_odds.sort(key=lambda r: (0 if r.bookmaker == "PC" else 1, r.bookmaker))
        totals_by_book: dict[str, list[MarketTotalsLine]] = {}
        h2h_by_book: dict[str, MarketOdds1X2] = {}

        for row in c_odds:
            b_key = "pinnacle" if row.bookmaker == "PC" else "consensus"
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
            return MarketInputs(
                source="fd_closing",
                snapshot_time=match.commence_time_utc,
                totals_by_book=totals_by_book,
                h2h_by_book=h2h_by_book,
                match_name=m_name,
            )

    # 2. Fall back to latest pre-kickoff market inputs via persistence loader
    cutoff = match.commence_time_utc or dt.datetime.combine(match.date_utc, dt.time(23, 59))
    try:
        inputs = load_market_inputs_for_match(session, match.id, as_of=cutoff)
        if not inputs.is_empty():
            return inputs
    except Exception as exc:
        logger.warning("Could not load pre-kickoff market inputs for match %s: %s", match.id, exc)

    return None


def calculate_user_bet_clv(
    session: Session,
    bet: UserBet,
    config: AppConfig | None = None,
) -> tuple[Decimal | None, str | None]:
    """Calculate pre-kickoff CLV against closing fair market matrix.

    Returns (clv_fair, clv_source).
    """
    match = session.get(Match, bet.match_id)
    if match is None:
        return None, None

    cfg = config or load_config()
    closing_inputs = load_closing_market_inputs(session, match)
    if closing_inputs is None or closing_inputs.is_empty():
        return None, None

    fair_matrix = build_fair_matrix(
        inputs=closing_inputs,
        config=cfg,
    )

    if fair_matrix.status != "ok":
        return None, None

    p_win, p_push = evaluate_fair_probs_for_bet(
        matrix=fair_matrix,
        market=bet.market,
        selection_key=bet.selection_key,
        line=bet.line,
        cs_grid_max_goals=cfg.fortuna.cs_grid_max_goals,
    )

    odds_val = float(bet.odds)
    clv_val = p_win * odds_val + p_push * 1.0 - 1.0

    source_label = "fair_market_matrix"
    clv_dec = Decimal(str(round(clv_val, 4)))
    return clv_dec, source_label
