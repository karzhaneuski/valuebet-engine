"""Team name normalization and fuzzy/substring match resolution for REST API."""

from __future__ import annotations

import re
from collections.abc import Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from core.orm import Match, Team, TeamAlias

_NOISE_TOKENS = {
    "fc",
    "cf",
    "afc",
    "fk",
    "ac",
    "sc",
    "bsc",
    "cd",
    "ud",
    "club",
    "de",
    "futbol",
}


def normalize_team_name(name: str) -> str:
    """Normalize team name by stripping punctuation, noise tokens, and extra whitespace."""
    clean = re.sub(r"[^\w\s]", " ", name.lower())
    tokens = [t for t in clean.split() if t not in _NOISE_TOKENS]
    return " ".join(tokens) if tokens else clean.strip()


def find_matching_teams(session: Session, query: str) -> set[int]:
    """Find team IDs matching query via direct name or alias substring search."""
    clean = query.strip().lower()
    norm = normalize_team_name(clean)
    if not clean:
        return set()

    # Search in Team.name
    team_ids = set(
        session.scalars(
            select(Team.id).where(
                or_(
                    func.lower(Team.name).contains(clean),
                    func.lower(Team.name).contains(norm),
                )
            )
        ).all()
    )

    # Search in TeamAlias.alias
    alias_ids = set(
        session.scalars(
            select(TeamAlias.team_id).where(
                or_(
                    func.lower(TeamAlias.alias).contains(clean),
                    func.lower(TeamAlias.alias).contains(norm),
                )
            )
        ).all()
    )

    # Also search token by token if multi-word
    for token in norm.split():
        if len(token) >= 4:
            t_ids = set(
                session.scalars(
                    select(Team.id).where(func.lower(Team.name).contains(token))
                ).all()
            )
            team_ids |= t_ids

    return team_ids | alias_ids


def search_matches_by_query(
    session: Session,
    query: str,
    status: str | None = None,
    league: str | None = None,
) -> Sequence[Match]:
    """Find matches matching a text query (e.g. 'Arsenal - Chelsea' or 'Liverpool').

    Splits query by common delimiters (' - ', ' vs ', ' — ', ' v ').
    If both teams are parsed, returns matches where both teams match.
    If only one team is parsed, returns all matches involving that team.
    """
    clean_q = query.strip()
    delims = [" - ", " — ", " – ", " vs ", " vs. ", " v "]
    home_part = ""
    away_part = ""

    for d in delims:
        if d in clean_q:
            parts = clean_q.split(d, 1)
            home_part = parts[0].strip()
            away_part = parts[1].strip()
            break

    if not home_part and "-" in clean_q:
        parts = clean_q.split("-", 1)
        home_part = parts[0].strip()
        away_part = parts[1].strip()
    elif not home_part:
        home_part = clean_q

    home_ids = find_matching_teams(session, home_part)
    away_ids = find_matching_teams(session, away_part) if away_part else set()

    stmt = select(Match)
    if league:
        stmt = stmt.where(Match.league == league)
    if status and status != "all":
        stmt = stmt.where(Match.status == status)

    if home_ids and away_ids:
        # Match where home is in home_ids and away is in away_ids, or vice-versa
        stmt = stmt.where(
            or_(
                (Match.home_team_id.in_(home_ids) & Match.away_team_id.in_(away_ids)),
                (Match.home_team_id.in_(away_ids) & Match.away_team_id.in_(home_ids)),
            )
        )
    elif home_ids:
        stmt = stmt.where(
            or_(
                Match.home_team_id.in_(home_ids),
                Match.away_team_id.in_(home_ids),
            )
        )
    elif away_ids:
        stmt = stmt.where(
            or_(
                Match.home_team_id.in_(away_ids),
                Match.away_team_id.in_(away_ids),
            )
        )
    else:
        return []

    stmt = stmt.order_by(Match.date_utc.desc())
    return session.scalars(stmt).all()
