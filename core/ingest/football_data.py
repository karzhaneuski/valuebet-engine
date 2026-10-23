"""football-data.co.uk loader: results + closing O/U 2.5 for the top-5.

Matching rule (docs/decisions.md, phase-1 review S4 — binding for phase 2):
the join key is the TEAM PAIR within a league season (via team_aliases);
``Date``+``Time`` are parsed as Europe/London and converted to UTC and used
ONLY as a ±1-day validation of the pairing, never as the join key.
Matches themselves come from the Understat schedule loader; if a pair is
unknown there, the row goes to the report (never silently skipped).

Closing totals are stored per bookmaker column family: AvgC (primary CLV
reference), MaxC (secondary); PC is stored where present (unreliable since
2025-26, kept for completeness).
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from sqlalchemy import select

from core.config import AppConfig
from core.db import session_scope
from core.ingest.aliases import AliasMatcher
from core.logging import get_logger
from core.orm import ClosingOdds, Match, MatchStat, Team, TeamAlias

logger = get_logger("ingest.football_data")

FD_BASE = "https://www.football-data.co.uk/mmz4281/{season}/{code}.csv"
SEASON_CODES = {2021: "2122", 2022: "2223", 2023: "2324", 2024: "2425",
                2025: "2526", 2026: "2627"}
LONDON = ZoneInfo("Europe/London")


@dataclass
class FdRow:
    league_slug: str
    season: int
    date: dt.date
    time: dt.time | None
    home: str
    away: str
    fthg: int
    ftag: int
    avgc_over: Decimal | None
    avgc_under: Decimal | None
    maxc_over: Decimal | None
    maxc_under: Decimal | None
    pc_over: Decimal | None
    pc_under: Decimal | None

    @property
    def kickoff_utc(self) -> dt.datetime | None:
        if self.time is None:
            return None
        local = dt.datetime.combine(self.date, self.time, tzinfo=LONDON)
        return local.astimezone(dt.UTC)


@dataclass
class FdReport:
    rows: int = 0
    matched: int = 0
    missing_match: list[str] = field(default_factory=list)   # pair not loaded yet
    unmatched_teams: list[str] = field(default_factory=list)  # alias unknown
    date_mismatches: list[str] = field(default_factory=list)  # >±1 day off
    stats_written: int = 0
    closing_written: int = 0
    alias_report: dict[str, dict] = field(default_factory=dict)  # fuzzy etc.


class FootballDataLoader:
    def __init__(self, db_url: str, config: AppConfig,
                 cache_dir: Path = Path("data/raw/football_data"),
                 session: requests.Session | None = None) -> None:
        self._db_url = db_url
        self._config = config
        self._cache_dir = cache_dir
        self._session = session or requests.Session()
        self._session.headers.setdefault(
            "User-Agent", "Mozilla/5.0 (valuebet-engine)")

    # --- download & parse ---------------------------------------------------

    def csv_path(self, league_slug: str, season: int) -> Path:
        league = self._league(league_slug)
        return self._cache_dir / f"{league.fd_code}_{SEASON_CODES[season]}.csv"

    def download(self, league_slug: str, season: int) -> Path:
        """Download once; the cached file is reused forever (results are
        final; the current season is re-downloaded on --refresh)."""
        path = self.csv_path(league_slug, season)
        if path.is_file():
            return path
        league = self._league(league_slug)
        url = FD_BASE.format(season=SEASON_CODES[season], code=league.fd_code)
        resp = self._session.get(url, timeout=30)
        resp.raise_for_status()
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_bytes(resp.content)
        logger.info("fd.downloaded", league=league_slug, season=season,
                    bytes=len(resp.content))
        return path

    def parse(self, league_slug: str, season: int,
              raw: bytes | None = None) -> list[FdRow]:
        if raw is None:
            raw = self.csv_path(league_slug, season).read_bytes()
        text = raw.decode("utf-8", errors="replace").lstrip("\ufeff")
        rows = [r for r in csv.reader(io.StringIO(text))
                if any(c.strip() for c in r)]
        header = rows[0]
        idx = {name: i for i, name in enumerate(header)}

        def col(name: str) -> int | None:
            return idx.get(name)

        def dec(row: list[str], name: str) -> Decimal | None:
            i = col(name)
            if i is None or i >= len(row) or not row[i].strip():
                return None
            assert i is not None
            return Decimal(row[i].strip())

        def num(row: list[str], name: str) -> int | None:
            d = dec(row, name)
            return int(d) if d is not None else None

        out: list[FdRow] = []
        t_idx = col("Time")
        date_i = col("Date")
        home_i = col("HomeTeam")
        away_i = col("AwayTeam")
        fthg_i = col("FTHG")
        ftag_i = col("FTAG")
        if None in (date_i, home_i, away_i, fthg_i, ftag_i):
            return out
        assert date_i is not None and home_i is not None
        assert away_i is not None and fthg_i is not None and ftag_i is not None
        needed = max(date_i, home_i, away_i, fthg_i, ftag_i)
        for row in rows[1:]:
            if len(row) <= needed:
                continue
            try:
                date = dt.datetime.strptime(row[date_i], "%d/%m/%Y").date()
            except ValueError:
                continue
            time_v: dt.time | None = None
            if t_idx is not None and t_idx < len(row) and row[t_idx].strip():
                try:
                    time_v = dt.time.fromisoformat(row[t_idx].strip())
                except ValueError:
                    time_v = None
            out.append(FdRow(
                league_slug=league_slug, season=season, date=date,
                time=time_v, home=row[home_i], away=row[away_i],
                fthg=num(row, "FTHG") or 0, ftag=num(row, "FTAG") or 0,
                avgc_over=dec(row, "AvgC>2.5"), avgc_under=dec(row, "AvgC<2.5"),
                maxc_over=dec(row, "MaxC>2.5"), maxc_under=dec(row, "MaxC<2.5"),
                pc_over=dec(row, "PC>2.5"), pc_under=dec(row, "PC<2.5"),
            ))
        return out

    # --- persistence ----------------------------------------------------------

    def upsert(self, rows: list[FdRow]) -> FdReport:
        report = FdReport()
        with session_scope(self._db_url) as s:
            matcher = AliasMatcher(
                {(a.source, a.alias): a.team_id
                 for a in s.scalars(select(TeamAlias)).all()},
                {t.id: t.name for t in s.scalars(select(Team)).all()})
            for row in rows:
                report.rows += 1
                home_m = matcher.match("football_data", row.home)
                away_m = matcher.match("football_data", row.away)
                for name, m in ((row.home, home_m), (row.away, away_m)):
                    if m.matched is None and name not in report.unmatched_teams:
                        report.unmatched_teams.append(name)
                        report.alias_report[name] = {
                            "candidates": m.candidates, "decision": "manual"}
                if home_m.matched is None or away_m.matched is None:
                    continue
                home = s.scalars(select(Team).where(
                    Team.name == home_m.matched)).first()
                away = s.scalars(select(Team).where(
                    Team.name == away_m.matched)).first()
                if home is None or away is None:
                    report.missing_match.append(
                        f"{row.season}/{row.league_slug}: {row.home} v "
                        f"{row.away} (team alias without Team row)")
                    continue
                # remember new exact/normalized spellings for next runs
                for team, _m, name in ((home, home_m, row.home),
                                      (away, away_m, row.away)):
                    if team is not None:
                        exists = s.scalars(select(TeamAlias).where(
                            TeamAlias.source == "football_data",
                            TeamAlias.alias == name)).first()
                        if exists is None:
                            s.add(TeamAlias(team_id=team.id,
                                            source="football_data",
                                            alias=name))
                match = s.scalars(select(Match).where(
                    Match.league == row.league_slug,
                    Match.season == row.season,
                    Match.home_team_id == home.id,
                    Match.away_team_id == away.id,
                    Match.stage.is_(None))).first()
                if match is None:
                    report.missing_match.append(
                        f"{row.season}/{row.league_slug}: {row.home} v "
                        f"{row.away} ({row.date})")
                    continue
                report.matched += 1
                # ±1 day validation only (decisions.md rule)
                kickoff = row.kickoff_utc
                if kickoff is not None and match.commence_time_utc is not None:
                    delta = abs(kickoff.date() - match.date_utc).days
                    if delta > 1:
                        report.date_mismatches.append(
                            f"match#{match.id}: fd {kickoff.date()} vs db "
                            f"{match.date_utc}")
                stat = s.scalars(select(MatchStat).where(
                    MatchStat.match_id == match.id,
                    MatchStat.source == "football_data")).first()
                if stat is None:
                    s.add(MatchStat(match_id=match.id,
                                    source="football_data",
                                    home_goals=row.fthg,
                                    away_goals=row.ftag))
                    report.stats_written += 1
                for book, over, under in (
                        ("AvgC", row.avgc_over, row.avgc_under),
                        ("MaxC", row.maxc_over, row.maxc_under),
                        ("PC", row.pc_over, row.pc_under)):
                    if over is None and under is None:
                        continue
                    closing = s.scalars(select(ClosingOdds).where(
                        ClosingOdds.match_id == match.id,
                        ClosingOdds.bookmaker == book)).first()
                    if closing is None:
                        s.add(ClosingOdds(match_id=match.id,
                                          bookmaker=book,
                                          over_price=over,
                                          under_price=under))
                        report.closing_written += 1
        return report

    def _league(self, slug: str):
        league = next((lg for lg in self._config.leagues
                       if lg.slug == slug and lg.fd_code), None)
        if league is None:
            raise ValueError(f"no football-data league for slug {slug!r}")
        return league
