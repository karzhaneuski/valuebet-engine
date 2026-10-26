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
from core.value.engine import match_team_side

logger = logging.getLogger(__name__)

STANDARD_FORTUNA_HOME_CLUSTER = {
    (1, 0), (2, 0), (3, 0), (4, 0), (5, 0), (6, 0),
    (2, 1), (3, 1), (4, 1), (5, 1),
    (3, 2), (4, 2), (4, 3),
}
STANDARD_FORTUNA_AWAY_CLUSTER = {
    (0, 1), (0, 2), (0, 3), (0, 4), (0, 5), (0, 6),
    (1, 2), (1, 3), (1, 4), (1, 5),
    (2, 3), (2, 4), (3, 4),
}


def parse_totals_line(selection_key: str, default_line: Decimal | None = None) -> tuple[str, float]:
    """Extract direction (over/under) and numeric line from selection_key or line column."""
    s = selection_key.strip().lower()
    direction = "over" if "over" in s or s.startswith("o") or s.startswith("+") else "under"

    # Search for numeric line inside string (e.g. totals:over:2.5 or over 2.5)
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
    home_name: str = "",
    away_name: str = "",
    cs_grid_max_goals: int = 4,
) -> str:
    """Evaluate whether a bet is 'won', 'lost', 'push', or 'void'.

    Returns status string: 'won', 'lost', 'push', or 'void'.
    """
    m = market.strip().lower()
    sel = selection_key.strip().lower()
    total_goals = home_goals + away_goals

    if m == "exact_score":
        if sel in ("score:other", "inny", "other"):
            overflow = home_goals > cs_grid_max_goals or away_goals > cs_grid_max_goals
            return "won" if overflow else "lost"

        clean_score = sel.removeprefix("score:").replace(":", "-").replace(" ", "")
        target = f"{home_goals}-{away_goals}"
        return "won" if clean_score == target else "lost"

    if m == "multiscore":
        if sel == "multi:remis":
            return "won" if home_goals == away_goals else "lost"
        if sel == "multi:home_other":
            is_ho = (home_goals > away_goals) and (
                (home_goals, away_goals) not in STANDARD_FORTUNA_HOME_CLUSTER
            )
            return "won" if is_ho else "lost"
        if sel == "multi:away_other":
            is_ao = (away_goals > home_goals) and (
                (home_goals, away_goals) not in STANDARD_FORTUNA_AWAY_CLUSTER
            )
            return "won" if is_ao else "lost"

        # e.g. "multi:1:0,2:0,3:0" or "1-0, 2-0, 3-0"
        clean = sel.removeprefix("multi:")
        scores = [s.strip().replace(":", "-") for s in clean.split(",")]
        target = f"{home_goals}-{away_goals}"
        return "won" if target in scores else "lost"

    if m == "goals_range":
        clean = sel.removeprefix("range:")
        if "+" in clean:
            min_g = int(clean.replace("+", "").strip())
            return "won" if total_goals >= min_g else "lost"
        parts = clean.replace(":", "-").split("-")
        if len(parts) == 2:
            try:
                lo, hi = int(parts[0].strip()), int(parts[1].strip())
                return "won" if lo <= total_goals <= hi else "lost"
            except ValueError:
                logger.warning("Malformed goals_range key: %s", selection_key)
                return "void"
        logger.warning("Unrecognized goals_range format: %s", selection_key)
        return "void"

    if m == "totals":
        clean = sel.removeprefix("totals:")
        direction, line_val = parse_totals_line(clean, default_line=line)
        if abs(total_goals - line_val) < 1e-6:
            return "push"
        if direction == "over":
            return "won" if total_goals > line_val else "lost"
        return "won" if total_goals < line_val else "lost"

    if m == "team_totals":
        # Format can be team_totals:<team>:over:1.5 or <team>_over or home/away
        clean = sel.removeprefix("team_totals:")
        parts = clean.split(":")
        team_str = parts[0] if len(parts) >= 2 else clean
        side: str | None = None
        if home_name and away_name:
            side = match_team_side(team_str, home_name, away_name)
        if side is None:
            if "home" in team_str or team_str.startswith("1"):
                side = "home"
            elif "away" in team_str or team_str.startswith("2"):
                side = "away"
            else:
                logger.warning(
                    "Could not resolve team side for team_totals: %s (home=%s, away=%s)",
                    selection_key,
                    home_name,
                    away_name,
                )
                return "void"

        team_goals = home_goals if side == "home" else away_goals
        direction, line_val = parse_totals_line(clean, default_line=line)
        if abs(team_goals - line_val) < 1e-6:
            return "push"
        if direction == "over":
            return "won" if team_goals > line_val else "lost"
        return "won" if team_goals < line_val else "lost"

    if m in ("h2h", "1x2"):
        clean = sel.removeprefix("h2h:").upper()
        if clean in ("1", "HOME"):
            return "won" if home_goals > away_goals else "lost"
        if clean in ("X", "DRAW", "REMIS"):
            return "won" if home_goals == away_goals else "lost"
        if clean in ("2", "AWAY"):
            return "won" if away_goals > home_goals else "lost"
        logger.warning("Unknown h2h outcome key: %s", selection_key)
        return "void"

    if m == "btts":
        clean = sel.removeprefix("btts:").lower()
        both_scored = home_goals > 0 and away_goals > 0
        if clean in ("yes", "y", "tak", "1"):
            return "won" if both_scored else "lost"
        if clean in ("no", "n", "nie", "0"):
            return "won" if not both_scored else "lost"
        logger.warning("Unknown btts outcome key: %s", selection_key)
        return "void"

    logger.warning("Unknown market type for settlement: %s (key=%s)", market, selection_key)
    return "void"


def settle_user_bet(
    bet: UserBet,
    home_goals: int,
    away_goals: int,
    home_name: str = "",
    away_name: str = "",
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
        home_name=home_name,
        away_name=away_name,
        cs_grid_max_goals=cs_grid_max_goals,
    )

    bet.status = outcome
    stake = bet.stake_units
    odds = bet.odds

    if outcome == "won":
        payout = Decimal(str(round(float(stake * odds), 3)))
        bet.payout_units = payout
        bet.pnl_units = payout - stake
    elif outcome in ("push", "void"):
        bet.payout_units = stake
        bet.pnl_units = Decimal("0.00")
    else:  # lost
        bet.payout_units = Decimal("0.00")
        bet.pnl_units = -stake

    bet.settled_at_utc = settled_at_utc or dt.datetime.now(dt.UTC)
    return bet
