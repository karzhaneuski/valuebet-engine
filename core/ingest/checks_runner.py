"""Database invariants for `python -m core.ingest checks` (review B2).

Every check prints PASS/FAIL with numbers; the command exits non-zero on
any FAIL.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.config import AppConfig
from core.db import session_scope
from core.ingest.aliases import normalize
from core.ingest.checks import future_kickoff_data_violations
from core.orm import Match, OddsSnapshot, Team

# Ligue 1 dropped to 18 teams from 2023/24; Bundesliga has 18 throughout
EIGHTEEN_TEAM = {"bundesliga"}


def expected_matches(league_slug: str, season: int) -> int:
    if league_slug in EIGHTEEN_TEAM or \
            (league_slug == "ligue_1" and season >= 2023):
        return 306
    return 380


def check_league_season_counts(s: Session) -> tuple[bool, str]:
    rows = s.execute(select(Match.league, Match.season, func.count())
                     .where(Match.league != "ucl")
                     .group_by(Match.league, Match.season)).all()
    bad = [f"{lg}|{se}: {n}!={expected_matches(lg, se)}"
           for lg, se, n in rows if n != expected_matches(lg, se)]
    return not bad, f"{len(rows)} league-seasons, mismatches: " + (
        "; ".join(bad) if bad else "none")


def check_unique_pairings(s: Session) -> tuple[bool, str]:
    league_dups = s.execute(
        select(Match.league, Match.season, Match.home_team_id,
               Match.away_team_id, func.count())
        .where(Match.stage.is_(None))
        .group_by(Match.league, Match.season, Match.home_team_id,
                  Match.away_team_id)
        .having(func.count() > 1)).all()
    ucl_dups = s.execute(
        select(Match.season, Match.stage, Match.home_team_id,
               Match.away_team_id, func.count())
        .where(Match.league == "ucl")
        .group_by(Match.season, Match.stage, Match.home_team_id,
                  Match.away_team_id)
        .having(func.count() > 1)).all()
    ok = not league_dups and not ucl_dups
    return ok, (f"league dup pairs={len(league_dups)}, "
                f"ucl dup pairs={len(ucl_dups)}")


def check_normalized_team_uniqueness(s: Session) -> tuple[bool, str]:
    seen: set[str] = set()
    dups: list[str] = []
    for name in s.scalars(select(Team.name)).all():
        norm = normalize(name)
        if norm in seen:
            dups.append(name)
        seen.add(norm)
    return not dups, f"collisions: {dups or 'none'}"


def check_future_kickoff_clean(db_url: str,
                               now: dt.datetime | None) -> tuple[bool, str]:
    violations = future_kickoff_data_violations(db_url, now=now)
    return not violations, (f"violations: {violations[:5] or 'none'} "
                            f"(total {len(violations)})")


def check_toa_snapshots_linked(s: Session) -> tuple[bool, str]:
    total = s.scalar(select(func.count()).select_from(OddsSnapshot)
                     .where(OddsSnapshot.source == "toa"))
    unlinked = s.scalar(select(func.count()).select_from(OddsSnapshot)
                        .where(OddsSnapshot.source == "toa",
                               OddsSnapshot.match_id.is_(None)))
    return unlinked == 0, f"unlinked={unlinked} of {total}"


def check_foreign_keys(db_url: str) -> tuple[bool, str]:
    if not db_url.startswith("sqlite:///"):
        return True, "non-sqlite database: foreign_key_check skipped"
    path = Path(db_url[len("sqlite:///"):])
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        conn.close()
    return not violations, (f"violations: {violations[:5] or 'none'} "
                            f"(total {len(violations)})")


def check_events_report(
        path: Path | None = None) -> tuple[bool, str]:
    """Last events report: top-5 unmatched empty AND zero top-5 teams
    created during the run."""
    p = path or Path("reports/ingest/events_report.json")
    if not p.is_file():
        return False, "no events_report.json"
    report = json.loads(p.read_text())
    unmatched = {lg: r.get("unmatched", []) for lg, r in report.items()
                 if lg != "ucl" and r.get("unmatched")}
    created = {lg: r.get("teams_created") for lg, r in report.items()
               if lg != "ucl"}
    # a report without the teams_created key is pre-B2 and does not prove
    # the invariant — treat missing keys as failures
    missing = [lg for lg, v in created.items() if v is None]
    ok = not unmatched and not missing and \
        not [v for v in created.values() if v]
    return ok, (f"unmatched={unmatched or 'none'}, "
                f"teams_created={ {k: v for k, v in created.items() if v} or 'none'}, "
                f"missing_key={missing or 'none'}")


def check_stale_scheduled(db_url: str,
                          now: dt.datetime | None) -> tuple[bool, str]:
    """WARN-level: kickoff more than 48h ago but still 'scheduled'. Not a
    FAIL (phase 7 settles it), but the list must be visible."""
    now = now or dt.datetime.now(dt.UTC)
    with session_scope(db_url) as s:
        stale = s.scalars(select(Match).where(
            Match.status == "scheduled",
            Match.commence_time_utc < now - dt.timedelta(hours=48))
            .order_by(Match.commence_time_utc)).all()
    names = [f"match#{m.id} {m.home_team.name} v {m.away_team.name} "
             f"{m.commence_time_utc:%Y-%m-%d}" for m in stale]
    return True, f"stale scheduled matches ({len(stale)}): " + (
        "; ".join(names) if names else "none")


def run_all_checks(db_url: str, config: AppConfig,
                   now: dt.datetime | None = None) -> int:
    """Prints PASS/FAIL per invariant; returns the number of FAILs."""
    results: list[tuple[str, bool, str]] = []
    with session_scope(db_url) as s:
        results.append(("league_season_counts", *check_league_season_counts(s)))
        results.append(("unique_pairings", *check_unique_pairings(s)))
        results.append(("normalized_team_uniqueness",
                        *check_normalized_team_uniqueness(s)))
        results.append(("toa_snapshots_linked", *check_toa_snapshots_linked(s)))
    results.append(("future_kickoff_clean",
                    *check_future_kickoff_clean(db_url, now)))
    results.append(("foreign_key_check", *check_foreign_keys(db_url)))
    results.append(("events_report_top5", *check_events_report()))
    warn_name, warn_ok, warn_detail = "stale_scheduled", *check_stale_scheduled(
        db_url, now)
    results.append((warn_name, warn_ok, warn_detail))
    failed = 0
    for name, ok, detail in results:
        level = "PASS" if ok else "FAIL"
        if name == "stale_scheduled" and ok:
            level = "WARN" if "matches (0)" not in detail else "PASS"
        print(f"{level} {name}: {detail}")
        failed += 0 if ok else 1
    return failed
