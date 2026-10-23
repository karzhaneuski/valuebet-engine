"""Understat loader: schedule, results, xG, lineups (via soccerdata, pinned).

- Seasons inside soccerdata's discovery window (last 5) go through the
  public ``read_schedule``/``read_player_match_stats`` API with the library's
  file cache.
- Older seasons (2021/22) are fetched ONCE through the public
  ``getLeagueData/{slug}/{season}`` JSON endpoint (see understat_archive.py)
  and stored forever under data/raw/understat/.
- All writes are idempotent upserts keyed on the natural unique constraints.
- soccerdata does its own throttling; additionally we sleep between lineup
  fetches (1 request per match, cached by the library).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select

from core.config import AppConfig
from core.db import session_scope
from core.ingest.aliases import AliasMatch, AliasMatcher
from core.ingest.understat_archive import ArchiveRow, archive_rows, is_archived, store_archive
from core.logging import get_logger
from core.orm import Match, MatchLineup, MatchStat, Team, TeamAlias

logger = get_logger("ingest.understat")

# soccerdata league code -> our league slug (from config)
LINEUP_SLEEP_S = 1.0


@dataclass
class UnderstatRow:
    league_slug: str
    season: int
    understat_id: str
    home: str
    away: str
    naive_start: dt.datetime  # UTC (phase-0 alignment: median delta 0 min)
    is_result: bool
    home_goals: int | None
    away_goals: int | None
    home_xg: float | None = None
    away_xg: float | None = None


@dataclass
class UnderstatReport:
    matches_created: int = 0
    matches_existing: int = 0
    stats_written: int = 0
    teams_created: list[str] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)

    def merge(self, other: UnderstatReport) -> None:
        self.matches_created += other.matches_created
        self.matches_existing += other.matches_existing
        self.stats_written += other.stats_written
        self.teams_created += other.teams_created
        self.unmatched += other.unmatched


class UnderstatLoader:
    def __init__(self, db_url: str, config: AppConfig,
                 cache_dir: Path = Path(".cache/understat"),
                 archive_dir: Path = Path("data/raw/understat")) -> None:
        self._db_url = db_url
        self._config = config
        self._cache_dir = cache_dir
        self._archive_dir = archive_dir

    # --- data acquisition ---------------------------------------------------

    def schedule_rows(self, league_slug: str,
                      season: int) -> list[UnderstatRow]:
        """Rows for one league-season: archive for pre-window seasons,
        soccerdata read_schedule otherwise."""
        league = self._league(league_slug)
        if is_archived(self._archive_dir, league.understat_league, season):
            return [self._row_from_archive(a, league_slug, season)
                    for a in archive_rows(self._archive_dir,
                                          league.understat_league, season)]
        import soccerdata

        reader = soccerdata.Understat(
            leagues=[league.understat_league], seasons=[season],
            data_dir=self._cache_dir)
        sched = reader.read_schedule().reset_index()
        rows: list[UnderstatRow] = []
        for _, r in sched.iterrows():
            rows.append(UnderstatRow(
                league_slug=league_slug, season=season,
                understat_id=str(int(r["game_id"])),
                home=str(r["home_team"]), away=str(r["away_team"]),
                naive_start=r["date"].to_pydatetime(),
                is_result=bool(r["is_result"]),
                home_goals=int(r["home_goals"])
                if r["is_result"] and r["home_goals"] is not None else None,
                away_goals=int(r["away_goals"])
                if r["is_result"] and r["away_goals"] is not None else None,
                home_xg=float(r["home_xg"])
                if r["is_result"] and r["home_xg"] is not None else None,
                away_xg=float(r["away_xg"])
                if r["is_result"] and r["away_xg"] is not None else None,
            ))
        return rows

    @staticmethod
    def _row_from_archive(a: ArchiveRow, league_slug: str,
                          season: int) -> UnderstatRow:
        return UnderstatRow(
            league_slug=league_slug, season=season,
            understat_id=a.understat_id, home=a.home, away=a.away,
            naive_start=a.naive_start, is_result=a.is_result,
            home_goals=a.home_goals, away_goals=a.away_goals,
            home_xg=a.home_xg, away_xg=a.away_xg)

    def backfill_archive(self, league_slug: str, season: int) -> None:
        """One-time download of a pre-window season into the permanent store."""
        league = self._league(league_slug)
        assert league.understat_league is not None
        store_archive(self._archive_dir, league.understat_league, season,
                      self._cache_dir)

    def lineup_rows(self, understat_id: int) -> list[dict]:
        """Per-player rows for one match (public soccerdata API, cached)."""
        import soccerdata

        reader = soccerdata.Understat(data_dir=self._cache_dir)
        players = reader.read_player_match_stats(match_id=understat_id)
        flat = players.reset_index()
        out = []
        for _, p in flat.iterrows():
            out.append({
                "team": str(p["team"]),
                "player": str(p["player"]),
                "started": str(p["position"]) != "Sub",
                "minutes": int(p["minutes"])
                if p["minutes"] is not None else None,
            })
        return out

    # --- persistence ---------------------------------------------------------

    def upsert_schedule(self, rows: list[UnderstatRow]) -> UnderstatReport:
        report = UnderstatReport()
        with session_scope(self._db_url) as s:
            matcher = self._matcher(s)
            for row in rows:
                home = self._team(s, matcher, row.home, row.league_slug,
                                  report)
                away = self._team(s, matcher, row.away, row.league_slug,
                                  report)
                if home is None or away is None:
                    continue
                match = s.scalars(
                    select(Match).where(
                        Match.understat_id == int(row.understat_id))).first()
                if match is None:
                    match = s.scalars(
                        select(Match).where(
                            Match.league == row.league_slug,
                            Match.season == row.season,
                            Match.home_team_id == home.id,
                            Match.away_team_id == away.id,
                            Match.stage.is_(None))).first()
                if match is None:
                    match = Match(
                        league=row.league_slug, season=row.season,
                        date_utc=row.naive_start.date(),
                        commence_time_utc=row.naive_start.replace(
                            tzinfo=dt.UTC),
                        kickoff_source="understat_placeholder",
                        status="finished" if row.is_result else "scheduled",
                        home_team_id=home.id, away_team_id=away.id,
                        understat_id=int(row.understat_id))
                    s.add(match)
                    s.flush()
                    report.matches_created += 1
                else:
                    report.matches_existing += 1
                    if match.understat_id is None:
                        match.understat_id = int(row.understat_id)
                if row.is_result and row.home_goals is not None:
                    existing = s.scalars(select(MatchStat).where(
                        MatchStat.match_id == match.id,
                        MatchStat.source == "understat")).first()
                    if existing is None:
                        s.add(MatchStat(
                            match_id=match.id, source="understat",
                            home_goals=row.home_goals,
                            away_goals=row.away_goals,
                            home_xg=row.home_xg, away_xg=row.away_xg))
                        report.stats_written += 1
        return report

    def upsert_lineups(self, match: Match, rows: list[dict]) -> int:
        """Returns the number of player rows written (0 if already loaded)."""
        written = 0
        with session_scope(self._db_url) as s:
            existing = s.scalars(select(MatchLineup).where(
                MatchLineup.match_id == match.id)).first()
            if existing is not None:
                return 0
            db_match = s.get(Match, match.id)
            if db_match is None:
                return 0
            by_name = {t.name: t
                       for t in (db_match.home_team, db_match.away_team)}
            for r in rows:
                team = by_name.get(r["team"])
                if team is None:
                    report_team = None
                    for name in by_name:
                        if r["team"].lower() in name.lower():
                            report_team = by_name[name]
                    team = report_team
                if team is None:
                    logger.warning("lineup.team.unmatched", team=r["team"])
                    continue
                s.add(MatchLineup(match_id=match.id, team_id=team.id,
                                  player=r["player"], started=r["started"],
                                  minutes=r["minutes"]))
                written += 1
        return written

    # --- helpers --------------------------------------------------------------

    def _league(self, slug: str):
        league = next((lg for lg in self._config.leagues
                       if lg.slug == slug and lg.understat_league), None)
        if league is None:
            raise ValueError(f"no understat league for slug {slug!r}")
        return league

    def _matcher(self, session) -> AliasMatcher:
        aliases = {
            (a.source, a.alias): a.team_id
            for a in session.scalars(select(TeamAlias)).all()}
        names = {t.id: t.name
                 for t in session.scalars(select(Team)).all()}
        return AliasMatcher(aliases, names)

    def _team(self, session, matcher: AliasMatcher, name: str, league_slug: str,
              report: UnderstatReport):
        """Resolve an Understat team name. Understat is a source of team
        identity: unknown names create a new team (canonical = Understat
        spelling); TOA canonical names win where an alias already links
        them."""
        m: AliasMatch = matcher.match("understat", name)
        team = None
        if m.matched is not None:
            team = session.scalars(
                select(Team).where(Team.name == m.matched)).first()
        if team is None:
            # the matcher is a snapshot taken before this loop; re-check
            # by name so a team created seconds ago is not duplicated
            team = session.scalars(
                select(Team).where(Team.name == name)).first()
        if team is None:
            team = Team(name=name, league=league_slug)
            session.add(team)
            session.flush()
            report.teams_created.append(name)
        exists = session.scalars(select(TeamAlias).where(
            TeamAlias.source == "understat",
            TeamAlias.alias == name)).first()
        if exists is None:
            session.add(TeamAlias(team_id=team.id, source="understat",
                                  alias=name))
        if team.league is None:
            team.league = league_slug
        return team
