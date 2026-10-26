"""Pre-kickoff Closing Line Value (CLV) calculation (SPEC §7, decisions S10).

Calculates CLV against the closing fair market matrix:
    CLV_fair = odds_placed * p_fair,closing + p_push * 1.0 - 1.0

Where p_fair,closing is derived from the latest market snapshot strictly BEFORE kickoff
(match.commence_time_utc), or from the devigged closing sharp quotes (football-data PC/AvgC).
"""

from __future__ import annotations

import contextlib
import datetime as dt
import logging
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.config import AppConfig, load_config
from core.orm import ClosingOdds, Match, UserBet
from core.tracking.settlement import (
    STANDARD_FORTUNA_AWAY_CLUSTER,
    STANDARD_FORTUNA_HOME_CLUSTER,
)
from core.value.fair_matrix import (
    FairMatrix,
    MarketInputs,
    MarketOdds1X2,
    MarketTotalsLine,
    build_fair_matrix,
)

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
        sel_clean = sel.removeprefix("score:")
        if sel_clean in ("inny", "other"):
            # All scores exceeding grid (0..4 goals)
            p_grid = 0.0
            for h in range(cs_grid_max_goals + 1):
                for a in range(cs_grid_max_goals + 1):
                    p_grid += matrix.exact_score(h, a)
            p_win = max(0.0, 1.0 - p_grid)
        else:
            clean = sel_clean.replace(":", "-").replace(" ", "")
            parts = clean.split("-")
            if len(parts) == 2:
                try:
                    h_g, a_g = int(parts[0]), int(parts[1])
                    p_win = matrix.exact_score(h_g, a_g)
                except ValueError:
                    p_win = 0.0

    elif m == "multiscore":
        sel_clean = sel.removeprefix("multi:")
        if sel_clean == "remis":
            p_win = sum(matrix.exact_score(g, g) for g in range(11))
        elif sel_clean == "home_other":
            p_win = sum(
                matrix.exact_score(h, a)
                for h in range(11)
                for a in range(11)
                if h > a and (h, a) not in STANDARD_FORTUNA_HOME_CLUSTER
            )
        elif sel_clean == "away_other":
            p_win = sum(
                matrix.exact_score(h, a)
                for h in range(11)
                for a in range(11)
                if a > h and (h, a) not in STANDARD_FORTUNA_AWAY_CLUSTER
            )
        else:
            # Format: "1-0, 2-0, 3-0"
            score_tuples: list[tuple[int, int]] = []
            for s in sel_clean.split(","):
                parts = s.strip().replace(":", "-").split("-")
                if len(parts) == 2:
                    with contextlib.suppress(ValueError):
                        score_tuples.append((int(parts[0]), int(parts[1])))
            p_win = matrix.multiscore(score_tuples)

    elif m == "goals_range":
        sel_clean = sel.removeprefix("range:")
        if "+" in sel_clean:
            try:
                min_g = int(sel_clean.replace("+", "").strip())
                p_win = sum(
                    matrix.exact_score(h, a)
                    for h in range(11)
                    for a in range(11)
                    if h + a >= min_g
                )
            except ValueError:
                p_win = 0.0
        else:
            parts = sel_clean.replace(":", "-").split("-")
            if len(parts) == 2:
                try:
                    lo, hi = int(parts[0].strip()), int(parts[1].strip())
                    p_win = matrix.goals_range(lo, hi)
                except ValueError:
                    p_win = 0.0

    elif m == "totals":
        sel_clean = sel.removeprefix("totals:")
        direction = "over" if "over" in sel_clean or sel_clean.startswith("o") else "under"
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
        sel_clean = sel.removeprefix("btts:")
        yes = "yes" in sel_clean or sel_clean == "y"
        p_win = matrix.btts(yes=yes)

    elif m in ("h2h", "1x2"):
        sel_clean = sel.removeprefix("h2h:")
        if sel_clean in ("1", "home"):
            p_win = matrix.p_home_devig
        elif sel_clean in ("x", "draw"):
            p_win = matrix.p_draw_devig
        elif sel_clean in ("2", "away"):
            p_win = matrix.p_away_devig

    elif m == "team_totals":
        sel_clean = sel.removeprefix("team_totals:")
        is_home = "home" in sel_clean or sel_clean.startswith("h")
        direction = "over" if "over" in sel_clean else "under"
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
) -> tuple[MarketInputs | None, str | None]:
    """Load closing market odds for a match to compute CLV.

    Rule (SPEC §7, decisions):
    1. Primary: Latest TOA snapshot (totals+h2h) captured strictly BEFORE kickoff
       (commence_time_utc). Source label: 'fair_market_matrix'.
    2. Fallback: football-data closing_odds ('PC' Pinnacle closing, 'AvgC' Consensus closing).
       Source label: 'football_data_closing'.
    """
    home_name = match.home_team.name if match.home_team else f"Team_{match.home_team_id}"
    away_name = match.away_team.name if match.away_team else f"Team_{match.away_team_id}"
    m_name = f"{home_name} - {away_name}"
    end_of_day = dt.datetime.combine(match.date_utc, dt.time(23, 59, tzinfo=dt.UTC))
    cutoff = match.commence_time_utc or end_of_day

    # 1. Primary: TOA snapshots strictly before kickoff
    try:
        from core.value.persistence import load_market_inputs_for_match
        toa_inputs = load_market_inputs_for_match(session, match.id, as_of=cutoff)
        if not toa_inputs.is_empty() and toa_inputs.source == "toa_snapshot":
            return toa_inputs, "fair_market_matrix"
    except Exception as exc:
        logger.warning("Could not load pre-kickoff TOA snapshot for match %s: %s", match.id, exc)

    # 2. Fallback: football-data closing_odds
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
            fd_inputs = MarketInputs(
                source="fd_closing",
                snapshot_time=match.commence_time_utc,
                totals_by_book=totals_by_book,
                h2h_by_book=h2h_by_book,
                match_name=m_name,
            )
            return fd_inputs, "football_data_closing"

    return None, None


def calculate_user_bet_clv(
    session: Session,
    bet: UserBet,
    config: AppConfig | None = None,
    now: dt.datetime | None = None,
) -> tuple[Decimal | None, str | None]:
    """Calculate pre-kickoff CLV against closing fair market matrix.

    Returns (clv_fair, clv_source).
    For matches before kickoff, CLV is not fixed and returns (None, None).
    """
    match = session.get(Match, bet.match_id)
    if match is None:
        return None, None

    curr_now = now or dt.datetime.now(dt.UTC)
    if match.commence_time_utc and match.status != "finished":
        ko_time = (
            match.commence_time_utc
            if match.commence_time_utc.tzinfo
            else match.commence_time_utc.replace(tzinfo=dt.UTC)
        )
        if curr_now < ko_time:
            return None, None

    cfg = config or load_config()
    closing_inputs, clv_source = load_closing_market_inputs(session, match)
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

    if p_win <= 0.0:
        return None, None

    odds_val = float(bet.odds)
    clv_val = p_win * odds_val + p_push * 1.0 - 1.0
    clv_dec = Decimal(str(round(clv_val, 4)))
    return clv_dec, clv_source
