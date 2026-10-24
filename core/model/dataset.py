"""Training-data selection for the Dixon-Coles model (phase 3).

Selection rules (PROMPTS phase 3 + phase-2 outcomes):
- only matches with status='finished' AND a known score;
  postponed*, void and scheduled are excluded by that single filter;
- B5 quarantine (docs/decisions.md, phase 2 review): matches whose
  football-data row was refused (date mismatch > 1 day + score
  disagreement) are passed in as ``excluded_match_ids`` and skipped —
  the quarantine itself lives only in the ingest report, never in the DB;
- strict ``kickoff_utc < as_of``: kickoff is ``commence_time_utc`` (the
  authoritative kickoff, UTC). A finished match with NULL commence time
  is skipped: we cannot prove it kicked off before as_of, and guessing
  would leak the future (matters for the phase-4 walk-forward);
- goals come from the match score: understat stats first, football-data
  stats as fallback; xG comes from understat only. Matches without xG
  are kept and trained on goals alone (w=1 for that match, SPEC §4);
- the team universe (for promoted-team priors) is every team appearing
  in ANY match row of the league, regardless of status or kickoff —
  that is how a promoted team is discovered before it ever plays.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import aliased

from core.db import session_scope
from core.orm import Match, MatchStat, Team

log = structlog.get_logger(__name__)

_MATCH_ID_RE = re.compile(r"match#(\d+)")


@dataclass(frozen=True)
class TrainingMatch:
    """One finished match as seen by the fitter (raw values, no mixing)."""

    match_id: int
    kickoff: dt.datetime          # tz-aware UTC, strictly < as_of
    home: str
    away: str
    home_goals: int
    away_goals: int
    home_xg: float | None         # understat xG; None -> goals-only match
    away_xg: float | None


@dataclass
class LoadStats:
    """What the loader did — surfaced in the fit report for traceability."""

    league: str
    as_of: dt.datetime
    n_selected: int = 0
    n_excluded_quarantine: int = 0
    n_skipped_no_kickoff: int = 0     # finished, but commence_time_utc is NULL
    n_skipped_no_score: int = 0       # finished before as_of, score unknown
    n_goals_from_football_data: int = 0
    n_missing_xg: int = 0             # trained on goals with w=1
    warnings: list[str] = field(default_factory=list)


@dataclass
class TrainingData:
    matches: list[TrainingMatch]
    team_universe: set[str]
    stats: LoadStats


def quarantined_match_ids(report: dict[str, Any]) -> set[int]:
    """Parse B5-quarantined match ids from a backfill report (dict form).

    The quarantine entries look like ``"match#123: fd 2026-02-06 vs db
    2026-02-08, score 0:0"`` (core/ingest/football_data.py). Only the
    ``quarantined`` lists are read; ``inverted_pairs`` entries name a
    DIFFERENT db match that stays valid and are ignored.
    """
    entries = report.get("football_data", [])
    if not isinstance(entries, list):
        entries = [entries]
    ids: set[int] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for item in entry.get("quarantined", []):
            m = _MATCH_ID_RE.search(item)
            if m is not None:
                ids.add(int(m.group(1)))
    return ids


def load_quarantined_match_ids(report_path: str | Path) -> set[int]:
    """Read quarantined ids from a backfill report JSON file."""
    data = json.loads(Path(report_path).read_text(encoding="utf-8"))
    return quarantined_match_ids(data)


def league_team_universe(db_url: str, league: str) -> set[str]:
    """Every team name appearing in any match row of the league."""
    away = aliased(Team)
    with session_scope(db_url) as s:
        names = set(
            s.scalars(
                select(Team.name).join(Match, Match.home_team_id == Team.id).where(
                    Match.league == league))
        )
        names.update(
            s.scalars(
                select(away.name).join(Match, Match.away_team_id == away.id).where(
                    Match.league == league))
        )
    return names


def load_training_data(
    db_url: str,
    league: str,
    as_of: dt.datetime,
    excluded_match_ids: Collection[int] = frozenset(),
) -> TrainingData:
    """Select finished, scored, pre-as_of matches of one league.

    The as_of filter runs inside SQL (``commence_time_utc < as_of``), so a
    match with kickoff at/after as_of can never enter the training set even
    if its row claims status='finished'.
    """
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    stats = LoadStats(league=league, as_of=as_of)
    excluded = set(excluded_match_ids)

    with session_scope(db_url) as s:
        away = aliased(Team)
        rows = s.execute(
            select(Match, Team.name, away.name)
            .join(Team, Match.home_team_id == Team.id)
            .join(away, Match.away_team_id == away.id)
            .where(
                Match.league == league,
                Match.status == "finished",
                Match.commence_time_utc < as_of,
            )
            .order_by(Match.commence_time_utc, Match.id)
        ).all()

        match_ids = [m.id for m, _, _ in rows]
        stat_rows: dict[int, dict[str, MatchStat]] = {}
        if match_ids:
            for st in s.scalars(
                select(MatchStat).where(
                    MatchStat.match_id.in_(match_ids),
                    MatchStat.source.in_(("understat", "football_data")),
                )
            ):
                stat_rows.setdefault(st.match_id, {})[st.source] = st

        n_no_kickoff = s.scalar(
            select(func.count()).select_from(Match).where(
                Match.league == league,
                Match.status == "finished",
                Match.commence_time_utc.is_(None),
            )
        ) or 0

    stats.n_skipped_no_kickoff = int(n_no_kickoff)
    if stats.n_skipped_no_kickoff:
        stats.warnings.append(
            f"{stats.n_skipped_no_kickoff} finished matches of {league} have no "
            "commence_time_utc; excluded (cannot prove kickoff < as_of)")

    matches: list[TrainingMatch] = []
    for m, home_name, away_name in rows:
        if m.id in excluded:
            stats.n_excluded_quarantine += 1
            continue
        kickoff = m.commence_time_utc
        assert kickoff is not None  # guaranteed by the SQL filter
        by_source = stat_rows.get(m.id, {})
        us = by_source.get("understat")
        fd = by_source.get("football_data")
        hg, ag, source = _score(us, fd)
        if hg is None or ag is None:
            stats.n_skipped_no_score += 1
            continue
        if source == "football_data":
            stats.n_goals_from_football_data += 1
        home_xg = float(us.home_xg) if us is not None and us.home_xg is not None \
            else None
        away_xg = float(us.away_xg) if us is not None and us.away_xg is not None \
            else None
        if home_xg is None or away_xg is None:
            stats.n_missing_xg += 1
            home_xg = away_xg = None  # goals-only match: w=1, no partial xG
        matches.append(TrainingMatch(
            match_id=m.id,
            kickoff=kickoff,
            home=home_name,
            away=away_name,
            home_goals=hg,
            away_goals=ag,
            home_xg=home_xg,
            away_xg=away_xg,
        ))

    stats.n_selected = len(matches)
    universe = league_team_universe(db_url, league)
    log.info("training_data_loaded", league=league, as_of=as_of.isoformat(),
             selected=stats.n_selected, universe=len(universe), **{
                 "excluded_quarantine": stats.n_excluded_quarantine,
                 "skipped_no_score": stats.n_skipped_no_score,
                 "missing_xg": stats.n_missing_xg,
             })
    return TrainingData(matches=matches, team_universe=universe, stats=stats)


def _score(
    us: MatchStat | None, fd: MatchStat | None
) -> tuple[int | None, int | None, str]:
    """Match score: understat first, football-data fallback."""
    if us is not None and us.home_goals is not None and us.away_goals is not None:
        return us.home_goals, us.away_goals, "understat"
    if fd is not None and fd.home_goals is not None and fd.away_goals is not None:
        return fd.home_goals, fd.away_goals, "football_data"
    return None, None, "none"
