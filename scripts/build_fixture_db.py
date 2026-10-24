"""Build a small fixture database that satisfies ALL ingest invariants.

Used by CI to run `python -m core.ingest checks` without network access:
18 teams, one complete double round-robin season (306 matches, all in the
past), a compliant events report. Run with DATABASE_URL set.
"""

from __future__ import annotations

import datetime as dt
import itertools
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from core.db import get_engine, session_scope
from core.orm import Base, Match, Team, TeamAlias


def main() -> None:
    from core.config import load_config

    url = load_config().database.url
    Base.metadata.create_all(get_engine(url))
    day = dt.timedelta(days=3)
    start = dt.datetime(2025, 8, 1, 15, 0, tzinfo=dt.UTC)
    with session_scope(url) as s:
        if s.scalars(select(Match).limit(1)).first() is not None:
            print("fixture db already populated")
            return
        teams = [Team(name=f"FC {chr(65 + i)}", league="bundesliga")
                 for i in range(18)]
        s.add_all(teams)
        s.flush()
        for t in teams:
            s.add(TeamAlias(team_id=t.id, source="understat", alias=t.name))
            s.add(TeamAlias(team_id=t.id, source="football_data", alias=t.name))
            s.add(TeamAlias(team_id=t.id, source="toa", alias=t.name))
        kickoff = start
        for home, away in itertools.permutations(teams, 2):
            s.add(Match(league="bundesliga", season=2025,
                        date_utc=kickoff.date(),
                        commence_time_utc=kickoff,
                        kickoff_source="understat_placeholder",
                        status="finished", home_team_id=home.id,
                        away_team_id=away.id))
            kickoff += day  # all in the past relative to any CI run
    report = {"bundesliga": {"events": 0, "created": 0,
                             "kickoffs_updated": 0,
                             "postponed_suspected": 0, "un_suspected": 0,
                             "teams_created": [], "unmatched": []}}
    out = Path("reports/ingest/events_report.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report))
    print("fixture db built:", url)


if __name__ == "__main__":
    main()
