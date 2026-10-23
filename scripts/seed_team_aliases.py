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

from sqlalchemy import select  # noqa: E402

from core.config import load_config  # noqa: E402
from core.db import session_scope  # noqa: E402
from core.orm import Team, TeamAlias  # noqa: E402

RECON = Path("reports/recon/odds_budget.json")


def load_pairs(recon_path: Path) -> list[list[str]]:
    data = json.loads(recon_path.read_text())
    return data["understat_alignment"]["team_name_pairs"]  # [toa, understat]


def seed(pairs: list[list[str]], db_url: str) -> dict:
    """Upsert teams and aliases; returns counts for reporting/tests."""
    teams_created = aliases_created = 0
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
                exists = session.scalars(
                    select(TeamAlias).where(TeamAlias.source == source,
                                            TeamAlias.alias == alias)).first()
                if exists is None:
                    session.add(TeamAlias(team_id=team.id, source=source,
                                          alias=alias))
                    aliases_created += 1
    return {"teams_created": teams_created, "aliases_created": aliases_created}


def main() -> None:
    recon = Path(sys.argv[1]) if len(sys.argv) > 1 else RECON
    db_url = load_config().database.url
    stats = seed(load_pairs(recon), db_url)
    print(f"seeded: {stats}")


if __name__ == "__main__":
    main()
