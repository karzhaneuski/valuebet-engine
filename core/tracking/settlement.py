"""User bets settlement logic by official match score (SPEC §7).

Handles all betting market types:
- exact_score (including Fortuna 'inny' grid overflow >4 goals)
- multiscore (cluster sets e.g. 1-0, 2-0, 3-0)
- goals_range (e.g. 2-3, 4-6, 7+)
- totals (half lines and integer lines with push return)
- btts (both teams to score yes/no)
- h2h (1X2 match outcome)
- team_totals (individual team over/under with push)
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from decimal import Decimal

from core.orm import UserBet

logger = logging.getLogger(__name__)


def parse_totals_line(selection_key: str, default_line: Decimal | None = None) -> tuple[str, float]:
    """Extract direction (over/under) and numeric line from selection_key or line column."""
    s = selection_key.strip().lower()
    direction = "over" if "over" in s or s.startswith("o") else "under"

    # Search for numeric line inside string
    match = re.search(r"(\d+(?:\.\d+)?)", s)
    if match:
        line_val = float(match.group(1))
    elif default_line is not None:
        line_val = float(default_line)
    else:
        line_val = 2.5

    return direction, line_val


def evaluate_bet_outcome(
    market: str,
    selection_key: str,
    line: Decimal | None,
    home_goals: int,
    away_goals: int,
    cs_grid_max_goals: int = 4,
) -> str:
    """Evaluate whether a bet is 'won', 'lost', or 'push'.

    Returns status string: 'won', 'lost', or 'push'.
    """
    m = market.strip().lower()
    sel = selection_key.strip().lower()
    total_goals = home_goals + away_goals

    if m == "exact_score":
        if sel in ("inny", "other"):
            overflow = home_goals > cs_grid_max_goals or away_goals > cs_grid_max_goals
            return "won" if overflow else "lost"

        clean_score = sel.replace(":", "-").replace(" ", "")
        target = f"{home_goals}-{away_goals}"
        return "won" if clean_score == target else "lost"

    if m == "multiscore":
        # Format: "1-0, 2-0, 3-0" or "1:0, 2:0, 3:0"
        scores = [
            s.strip().replace(":", "-")
            for s in sel.split(",")
        ]
        target = f"{home_goals}-{away_goals}"
        return "won" if target in scores else "lost"

    if m == "goals_range":
        if "+" in sel:
            min_g = int(sel.replace("+", "").strip())
            return "won" if total_goals >= min_g else "lost"
        parts = sel.split("-")
        if len(parts) == 2:
            lo, hi = int(parts[0].strip()), int(parts[1].strip())
            return "won" if lo <= total_goals <= hi else "lost"
        return "lost"

    if m == "totals":
        direction, line_val = parse_totals_line(sel, default_line=line)
        # Check integer push
        if abs(total_goals - line_val) < 1e-6:
            return "push"
        if direction == "over":
            return "won" if total_goals > line_val else "lost"
        return "won" if total_goals < line_val else "lost"

    if m == "btts":
        both_scored = home_goals > 0 and away_goals > 0
        if "yes" in sel or sel == "y":
            return "won" if both_scored else "lost"
        return "won" if not both_scored else "lost"

    if m in ("h2h", "1x2"):
        if home_goals > away_goals:
            actual = "1"
        elif home_goals == away_goals:
            actual = "x"
        else:
            actual = "2"

        normalized_sel = sel.replace("draw", "x").replace("home", "1").replace("away", "2")
        return "won" if normalized_sel == actual else "lost"

    if m == "team_totals":
        is_home = "home" in sel or sel.startswith("h")
        team_goals = home_goals if is_home else away_goals
        direction, line_val = parse_totals_line(sel, default_line=line)
        if abs(team_goals - line_val) < 1e-6:
            return "push"
        if direction == "over":
            return "won" if team_goals > line_val else "lost"
        return "won" if team_goals < line_val else "lost"

    logger.warning("Unknown market type for settlement: %s", market)
    return "lost"


def settle_user_bet(
    bet: UserBet,
    home_goals: int,
    away_goals: int,
    settled_at_utc: dt.datetime | None = None,
    cs_grid_max_goals: int = 4,
) -> UserBet:
    """Settle user bet with official match score and update P/L and payout units."""
    if bet.status in ("void", "cancelled"):
        bet.payout_units = bet.stake_units
        bet.pnl_units = Decimal("0.00")
        bet.settled_at_utc = settled_at_utc or dt.datetime.now(dt.UTC)
        return bet

    outcome = evaluate_bet_outcome(
        market=bet.market,
        selection_key=bet.selection_key,
        line=bet.line,
        home_goals=home_goals,
        away_goals=away_goals,
        cs_grid_max_goals=cs_grid_max_goals,
    )

    bet.status = outcome
    stake = bet.stake_units
    odds = bet.odds

    if outcome == "won":
        payout = Decimal(str(round(float(stake * odds), 3)))
        bet.payout_units = payout
        bet.pnl_units = payout - stake
    elif outcome == "push":
        bet.payout_units = stake
        bet.pnl_units = Decimal("0.00")
    else:  # lost
        bet.payout_units = Decimal("0.00")
        bet.pnl_units = -stake

    bet.settled_at_utc = settled_at_utc or dt.datetime.now(dt.UTC)
    return bet
