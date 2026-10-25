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
from core.http import retry_call
from core.ingest.aliases import AliasMatcher
from core.logging import get_logger
from core.orm import (
    ClosingOdds,
    Match,
    MatchStat,
    PrematchOdds,
    Team,
    TeamAlias,
)

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
    # pre-match totals (phase 4b): Avg/P/Max columns without the C suffix
    avg_over: Decimal | None = None
    avg_under: Decimal | None = None
    p_over: Decimal | None = None
    p_under: Decimal | None = None
    max_over: Decimal | None = None
    max_under: Decimal | None = None

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
    quarantined: list[str] = field(default_factory=list)      # B5: no write
    inverted_pairs: list[str] = field(default_factory=list)    # B5
    stats_written: int = 0
    stats_updated: int = 0
    closing_written: int = 0
    closing_updated: int = 0
    prematch_written: int = 0
    prematch_updated: int = 0
    alias_report: dict[str, dict] = field(default_factory=dict)  # fuzzy etc.


class FootballDataLoader:
    def __init__(self, db_url: str, config: AppConfig,
                 cache_dir: Path = Path("data/raw/football_data"),
                 session: requests.Session | None = None) -> None:
        self._db_url = db_url
        self._config = config
        self._cache_dir = cache_dir
        self._retry_attempts = config.ingest.retry_attempts
        self._retry_backoff_s = config.ingest.retry_backoff_s
        self._session = session or requests.Session()
        self._session.headers.setdefault(
            "User-Agent", "Mozilla/5.0 (valuebet-engine)")

    # --- download & parse ---------------------------------------------------

    def csv_path(self, league_slug: str, season: int) -> Path:
        league = self._league(league_slug)
        return self._cache_dir / f"{league.fd_code}_{SEASON_CODES[season]}.csv"

    def download(self, league_slug: str, season: int,
                 current_season: int | None = None,
                 ttl_hours: int = 12) -> Path:
        """Past seasons are cached forever; the CURRENT season is
        re-downloaded when its cache file is older than ttl_hours (B4)."""
        path = self.csv_path(league_slug, season)
        if path.is_file():
            if current_season is None or season != current_season:
                return path
            age_h = (dt.datetime.now() - dt.datetime.fromtimestamp(
                path.stat().st_mtime)).total_seconds() / 3600
            if age_h < ttl_hours:
                return path
            path.unlink()
        league = self._league(league_slug)
        url = FD_BASE.format(season=SEASON_CODES[season], code=league.fd_code)
        attempts = self._retry_attempts

        def _fetch():
            resp = self._session.get(url, timeout=30)
            resp.raise_for_status()
            return resp

        resp = retry_call(_fetch, attempts=attempts,
                          backoff_s=self._retry_backoff_s)
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
                avg_over=dec(row, "Avg>2.5"), avg_under=dec(row, "Avg<2.5"),
                p_over=dec(row, "P>2.5"), p_under=dec(row, "P<2.5"),
                max_over=dec(row, "Max>2.5"), max_under=dec(row, "Max<2.5"),
            ))
        return out

    # --- persistence ----------------------------------------------------------

    def upsert(self, rows: list[FdRow]) -> FdReport:
        report = FdReport()
        with session_scope(self._db_url) as s:
            aliases = {(a.source, a.alias): a.team_id
                       for a in s.scalars(select(TeamAlias)).all()}
            names = {t.id: t.name for t in s.scalars(select(Team)).all()}
            matcher = AliasMatcher(aliases, names)
            league_slugs = {r.league_slug for r in rows}
            seasons = {r.season for r in rows}
            roster: set[int] = set()
            for lg in league_slugs:
                for sn in seasons:
                    for h, a in s.execute(
                            select(Match.home_team_id, Match.away_team_id)
                            .where(Match.league == lg,
                                   Match.season == sn)).all():
                        roster.update((h, a))
            for row in rows:
                report.rows += 1
                home_m = matcher.match("football_data", row.home,
                                       roster=roster)
                away_m = matcher.match("football_data", row.away,
                                       roster=roster)
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
                    # B5: inverted pair on (nearly) the same date must NOT
                    # be bound to the return leg — report and skip
                    inverted = s.scalars(select(Match).where(
                        Match.league == row.league_slug,
                        Match.season == row.season,
                        Match.home_team_id == away.id,
                        Match.away_team_id == home.id,
                        Match.stage.is_(None),
                        Match.date_utc >= row.date - dt.timedelta(days=1),
                        Match.date_utc <= row.date + dt.timedelta(days=1))
                        .order_by(Match.date_utc)).first()
                    label = (f"{row.season}/{row.league_slug}: fd {row.home} "
                             f"v {row.away} ({row.date})")
                    if inverted is not None:
                        report.inverted_pairs.append(
                            f"{label} -> db match#{inverted.id} "
                            f"{inverted.date_utc} (inverted)")
                        continue
                    report.missing_match.append(label)
                    continue
                report.matched += 1
                # ±1 day validation only (decisions.md rule); the
                # date_mismatches journal entry is added AFTER the
                # inverted/quarantine resolution below, so a row that is
                # already classified (inverted pair / quarantine) does not
                # also dangle in date_mismatches
                kickoff = row.kickoff_utc
                delta = None
                if kickoff is not None and match.date_utc is not None:
                    delta = abs(kickoff.date() - match.date_utc).days
                if delta is not None and delta > 1 and kickoff is not None:
                    # B5: a same-date INVERTED fixture exists -> the fd row
                    # belongs to it, not to this far-away pairing (the
                    # Rennes/PSG case: never bind to the return leg)
                    inverted = s.scalars(select(Match).where(
                        Match.league == row.league_slug,
                        Match.season == row.season,
                        Match.home_team_id == away.id,
                        Match.away_team_id == home.id,
                        Match.stage.is_(None),
                        Match.date_utc >= row.date - dt.timedelta(days=1),
                        Match.date_utc <= row.date + dt.timedelta(days=1))
                        .order_by(Match.date_utc)).first()
                    if inverted is not None:
                        report.inverted_pairs.append(
                            f"fd {row.home} v {row.away} ({row.date}) -> "
                            f"inverted db match#{inverted.id} "
                            f"{inverted.date_utc}; NOT bound to match#"
                            f"{match.id} {match.date_utc}")
                        continue
                    # otherwise write stats/closing only when the match is
                    # already finished (per Understat) AND the score agrees;
                    # else quarantine (report, no write)
                    us_stat = s.scalars(select(MatchStat).where(
                        MatchStat.match_id == match.id,
                        MatchStat.source == "understat")).first()
                    if us_stat is None or us_stat.home_goals is None or \
                            (us_stat.home_goals, us_stat.away_goals) != \
                            (row.fthg, row.ftag):
                        report.quarantined.append(
                            f"match#{match.id}: fd {kickoff.date()} vs db "
                            f"{match.date_utc}, score "
                            f"{row.fthg}:{row.ftag}")
                        continue
                if delta is not None and delta > 1 and kickoff is not None:
                    # survived the checks above (or delta check passed):
                    # record the date discrepancy for a row we actually wrote
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
                elif (stat.home_goals, stat.away_goals) != \
                        (row.fthg, row.ftag):
                    stat.home_goals = row.fthg
                    stat.away_goals = row.ftag
                    report.stats_updated += 1
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
                    elif (closing.over_price, closing.under_price) != \
                            (over, under):
                        closing.over_price = over
                        closing.under_price = under
                        report.closing_updated += 1
                for book, over, under in (
                        ("Avg", row.avg_over, row.avg_under),
                        ("P", row.p_over, row.p_under),
                        ("Max", row.max_over, row.max_under)):
                    if over is None and under is None:
                        continue
                    pre = s.scalars(select(PrematchOdds).where(
                        PrematchOdds.match_id == match.id,
                        PrematchOdds.bookmaker == book)).first()
                    if pre is None:
                        s.add(PrematchOdds(match_id=match.id, bookmaker=book,
                                           over_price=over, under_price=under))
                        report.prematch_written += 1
                    elif (pre.over_price, pre.under_price) != (over, under):
                        pre.over_price = over
                        pre.under_price = under
                        report.prematch_updated += 1
        return report

    def _league(self, slug: str):
        league = next((lg for lg in self._config.leagues
                       if lg.slug == slug and lg.fd_code), None)
        if league is None:
            raise ValueError(f"no football-data league for slug {slug!r}")
        return league
