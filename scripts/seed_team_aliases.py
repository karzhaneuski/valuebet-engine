"""Seed teams + team_aliases from the phase-0 TOA<->Understat alignment.

Source of truth: reports/recon/odds_budget.json ->
understat_alignment.team_name_pairs (82 unique TOA-name -> Understat-name
team pairs verified in phase 0). Canonical team name is the TOA spelling
(authoritative kickoff source, docs/decisions.md S5); the Understat
spelling goes to team_aliases.

teams.league stays NULL here: the phase-0 report stores pairs without the
sport key, and league membership is backfilled in phase 2 when fixtures
load. Idempotent: safe to re-run (upsert by source+alias). football-data
spellings are also added by phase 2.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from core.config import load_config
from core.db import session_scope
from core.orm import Team, TeamAlias

RECON = Path("reports/recon/odds_budget.json")

# Known bad pairs produced by the phase-0 fuzzy matcher (matched on the
# token "Club"): the real pairs are Athletic Bilbao <-> Athletic Club and
# Real Racing Club de Santander <-> Racing Santander.
EXCLUDED_PAIRS: set[tuple[str, str]] = {
    ("Real Racing Club de Santander", "Athletic Club"),
}


def load_pairs(recon_path: Path) -> list[list[str]]:
    data = json.loads(recon_path.read_text())
    return data["understat_alignment"]["team_name_pairs"]  # [toa, understat]


class AliasConflictError(RuntimeError):
    """An alias is already bound to a different team — never skip silently."""


def seed(pairs: list[list[str]], db_url: str) -> dict:
    """Upsert teams and aliases; returns counts for reporting/tests.

    Raises AliasConflictError if an alias from the pair list is already
    bound to a DIFFERENT team: that means either a bad pair (like the
    excluded fuzzy-match mistakes) or dirty data, and must be resolved
    manually, not skipped.
    """
    pairs = [p for p in pairs
             if (p[0], p[1]) not in EXCLUDED_PAIRS]
    teams_created = aliases_created = 0
    conflicts: list[str] = []
    with session_scope(db_url) as session:
        for toa_name, understat_name in pairs:
            team = session.scalars(
                select(Team).where(Team.name == toa_name)).first()
            if team is None:
                team = Team(name=toa_name)
                session.add(team)
                session.flush()
                teams_created += 1
            for source, alias in (("toa", toa_name),
                                  ("understat", understat_name)):
                existing = session.scalars(
                    select(TeamAlias).where(TeamAlias.source == source,
                                            TeamAlias.alias == alias)).first()
                if existing is None:
                    session.add(TeamAlias(team_id=team.id, source=source,
                                          alias=alias))
                    aliases_created += 1
                elif existing.team_id != team.id:
                    conflicts.append(
                        f"{source}:{alias!r} already bound to team "
                        f"{existing.team_id}, pair wants team {team.id} "
                        f"({toa_name!r})")
    if conflicts:
        raise AliasConflictError("; ".join(conflicts))
    return {"teams_created": teams_created, "aliases_created": aliases_created}


def main() -> None:
    recon = Path(sys.argv[1]) if len(sys.argv) > 1 else RECON
    db_url = load_config().database.url
    stats = seed(load_pairs(recon), db_url)
    print(f"seeded: {stats}")


if __name__ == "__main__":
    main()
