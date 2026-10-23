"""The Odds API client: free /events, paid odds snapshots, /scores.

Paid-call protocol (review B1): every call goes through
``QuotaGuard.begin/settle/fail`` — atomic reservation before the request,
actual cost from ``x-requests-last`` after it; on a response-less failure
the reservation stays spent. Free calls are logged with credits=0.

Matching (reviews B6/B7): events are matched with the same deterministic
matcher as the loaders (exact -> normalized -> league-roster containment);
matched teams get a ``toa`` alias; pair-matched matches get the TOA event
id and their odds_snapshots re-linked. Postponement rules follow config:
a >15 min shift of a previously TOA-authoritative kickoff flags
postponed_suspected; a vanished event with a FUTURE kickoff flags it too
(past kickoffs are phase-7 settlement territory). A stable event clears
the flag.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.config import AppConfig
from core.db import session_scope
from core.http import HttpError, redact_text, retry_call
from core.ingest.aliases import AliasMatcher
from core.ingest.quota import QuotaGuard
from core.logging import get_logger
from core.orm import Match, OddsSnapshot, Result, Team, TeamAlias

logger = get_logger("ingest.odds_api")

BASE = "https://api.the-odds-api.com/v4"
TIER_LOWER_BOUND_MIN = {"T-48": 40 * 60, "T-24": 18 * 60, "T-3": 60,
                        "T-1": 30, "manual": 0, "off_window": 0}


@dataclass
class EventsReport:
    events_seen: int = 0
    kickoffs_updated: int = 0
    matches_created: int = 0
    postponed_suspected: int = 0
    un_suspected: int = 0
    teams_created: list[str] = field(default_factory=list)
    unmatched_teams: list[str] = field(default_factory=list)


def _as_utc(value: dt.datetime | None) -> dt.datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


def _to_int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


class OddsApiClient:
    def __init__(self, db_url: str, config: AppConfig, api_key: str,
                 client: httpx.Client | None = None) -> None:
        self._db_url = db_url
        self._config = config
        self._key = api_key
        self._http = client or httpx.Client(timeout=30.0)
        self.quota = QuotaGuard(db_url, config, provider="toa")

    # --- HTTP plumbing ---------------------------------------------------------

    def _free_get(self, endpoint: str,
                  params: dict) -> tuple[object, int | None]:
        """Free call; logged with credits=0; never raises with a URL."""
        attempts = self._config.ingest.retry_attempts

        def _fetch() -> httpx.Response:
            return self._http.get(f"{BASE}{endpoint}",
                                  params={**params, "apiKey": self._key})

        try:
            resp = retry_call(_fetch, attempts=attempts,
                              backoff_s=self._config.ingest.retry_backoff_s)
        except httpx.HTTPError as exc:
            raise HttpError(None, endpoint,
                            message=type(exc).__name__) from None
        remaining = _to_int(resp.headers.get("x-requests-remaining"))
        self.quota.record(endpoint, 0, remaining)
        if resp.status_code != 200:
            raise HttpError(resp.status_code, endpoint,
                            message=redact_text(resp.text[:80]))
        return resp.json(), remaining

    def _paid_get(self, endpoint: str, params: dict,
                  expected_cost: int) -> tuple[object, int | None]:
        """Paid call: reserve before, settle by headers after (B1)."""
        usage_id = self.quota.begin(endpoint, expected_cost)
        try:
            resp = self._http.get(f"{BASE}{endpoint}",
                                  params={**params, "apiKey": self._key})
        except httpx.HTTPError as exc:
            self.quota.fail(usage_id)
            raise HttpError(None, endpoint,
                            message=type(exc).__name__) from None
        remaining = _to_int(resp.headers.get("x-requests-remaining"))
        last = _to_int(resp.headers.get("x-requests-last"))
        actual = last if last is not None else expected_cost
        self.quota.settle(usage_id, actual, remaining)
        if resp.status_code != 200:
            raise HttpError(resp.status_code, endpoint,
                            message=redact_text(resp.text[:80]))
        return resp.json(), remaining

    # --- free: /events -----------------------------------------------------------

    def fetch_events(self, sport_key: str) -> list[dict]:
        body, _ = self._free_get(f"/sports/{sport_key}/events",
                                 {"regions": "eu", "dateFormat": "iso"})
        assert isinstance(body, list)
        return body

    def apply_events(self, sport_key: str, events: list[dict],
                     league_slug: str, season: int) -> EventsReport:
        cfg = self._config.odds_policy.postponement
        report = EventsReport()
        now = dt.datetime.now(dt.UTC)
        with session_scope(self._db_url) as s:
            matcher, names = self._matcher(s)
            roster = self._roster(s, league_slug, season)
            seen_event_ids: set[str] = set()
            for ev in events:
                report.events_seen += 1
                eid = str(ev["id"])
                seen_event_ids.add(eid)
                commence = dt.datetime.fromisoformat(
                    ev["commence_time"].replace("Z", "+00:00"))
                home = self._team_for(s, matcher, names, roster,
                                      ev["home_team"], league_slug, report)
                away = self._team_for(s, matcher, names, roster,
                                      ev["away_team"], league_slug, report)
                if home is None or away is None:
                    continue
                match = s.scalars(select(Match).where(
                    Match.toa_event_id == eid)).first()
                if match is None and league_slug != "ucl":
                    match = s.scalars(select(Match).where(
                        Match.league == league_slug,
                        Match.season == season,
                        Match.home_team_id == home.id,
                        Match.away_team_id == away.id,
                        Match.stage.is_(None))).first()
                if match is None and league_slug == "ucl":
                    match = self._find_ucl_by_pair(s, season, home.id,
                                                   away.id, commence)
                if match is None:
                    match = Match(
                        league=league_slug,
                        season=season,
                        stage="league_phase" if league_slug == "ucl" else None,
                        date_utc=commence.date(),
                        commence_time_utc=commence,
                        kickoff_source="toa", status="scheduled",
                        home_team_id=home.id, away_team_id=away.id,
                        toa_event_id=eid)
                    s.add(match)
                    s.flush()
                    report.matches_created += 1
                else:
                    was_linked = match.toa_event_id is not None
                    match.toa_event_id = eid
                    if not was_linked:
                        self._relink_snapshots(s, eid, match.id)
                # S10: a stable event clears a stale suspicion
                stored = _as_utc(match.commence_time_utc)
                if match.status == "postponed_suspected" and stored and \
                        abs((commence - stored).total_seconds()) / 60 <= \
                        cfg.shift_min:
                    match.status = "scheduled"
                    report.un_suspected += 1
                # postponement: only a previously TOA-authoritative kickoff
                # shifting by more than cfg minutes (S10/B6)
                if stored is not None and match.kickoff_source == "toa":
                    shift = abs((commence - stored).total_seconds()) / 60
                    if shift > cfg.shift_min and \
                            match.status in ("scheduled",
                                             "postponed_suspected"):
                        match.status = "postponed_suspected"
                        report.postponed_suspected += 1
                if match.commence_time_utc != commence:
                    match.commence_time_utc = commence
                    match.date_utc = commence.date()
                    match.kickoff_source = "toa"
                    report.kickoffs_updated += 1
            # vanished events: only FUTURE kickoffs (B3); past ones are
            # the KO+3h phase-7 settlement rule
            horizon_start = now - dt.timedelta(days=7)
            existing = s.scalars(select(Match).where(
                Match.toa_event_id.isnot(None),
                Match.league == league_slug)).all()
            for m in existing:
                if m.toa_event_id in seen_event_ids:
                    continue
                if m.status != "scheduled":
                    continue
                stored_m = _as_utc(m.commence_time_utc)
                if stored_m and stored_m > now + dt.timedelta(minutes=5) \
                        and horizon_start < stored_m:
                    m.status = "postponed_suspected"
                    report.postponed_suspected += 1
        return report

    # --- paid: odds snapshots ------------------------------------------------------

    def snapshot_sport_totals(self, sport_key: str, tier: str) -> dict:
        endpoint = f"/sports/{sport_key}/odds"
        body, remaining = self._paid_get(
            endpoint, {"regions": "eu", "markets": "totals",
                       "oddsFormat": "decimal"}, expected_cost=1)
        assert isinstance(body, list)
        written = self._store_events_odds(body, sport_key, "totals", tier)
        return {"events": len(body), "rows": written, "credits": 1,
                "remaining": remaining}

    def snapshot_event_markets(self, sport_key: str, event_id: str,
                               markets: list[str], tier: str) -> dict:
        endpoint = f"/sports/{sport_key}/events/{event_id}/odds"
        body, remaining = self._paid_get(
            endpoint, {"regions": "eu", "markets": ",".join(markets),
                       "oddsFormat": "decimal"}, expected_cost=len(markets))
        assert isinstance(body, (list, dict))
        written = 0
        for market in markets:
            written += self._store_events_odds(body, sport_key, market,
                                               tier, event_id)
        return {"event": event_id, "rows": written,
                "credits": len(markets), "remaining": remaining}

    def _store_events_odds(self, body: list | dict, sport_key: str,
                           market: str, tier: str,
                           only_event_id: str | None = None) -> int:
        events = body if isinstance(body, list) else [body]
        now = dt.datetime.now(dt.UTC)
        windows = self._config.odds_policy.tier_windows
        half_only = self._config.totals_lines.half_lines_only
        dropped_lines = 0
        written = 0
        with session_scope(self._db_url) as s:
            for ev in events:
                eid = str(ev.get("id", only_event_id))
                if only_event_id is not None and eid != only_event_id:
                    continue
                match = s.scalars(select(Match).where(
                    Match.toa_event_id == eid)).first()
                stored = _as_utc(match.commence_time_utc) if match else None
                actual = None
                tier_assigned = tier
                if stored is not None:
                    lead_h = (stored - now).total_seconds() / 3600
                    actual = int((stored - now).total_seconds() / 60)
                    bounds = {"T-48": windows.t48, "T-24": windows.t24,
                              "T-3": windows.t3}.get(tier)
                    if bounds and not (bounds[0] <= lead_h <= bounds[1]):
                        tier_assigned = "off_window"
                for bk in ev.get("bookmakers", []):
                    for mkt in bk.get("markets", []):
                        if mkt.get("key") != market:
                            continue
                        for o in mkt.get("outcomes", []):
                            price = o.get("price")
                            if price is None:
                                continue
                            point = o.get("point")
                            line = None
                            if point is not None:
                                # half lines only (docs/decisions.md S11)
                                if half_only and float(point) * 2 % 2 != 1:
                                    dropped_lines += 1
                                    continue
                                line = Decimal(str(point))
                            s.add(OddsSnapshot(
                                toa_sport_key=sport_key, toa_event_id=eid,
                                match_id=match.id if match else None,
                                bookmaker=str(bk.get("key") or bk.get("title")),
                                market=market, line=line,
                                outcome=str(o.get("name")),
                                price=Decimal(str(price)),
                                tier=tier_assigned,
                                planned_offset_min=TIER_LOWER_BOUND_MIN.get(
                                    tier, 0),
                                actual_offset_min=actual,
                                source="toa",
                                captured_at_utc=now))
                            written += 1
        if dropped_lines:
            logger.info("odds.lines.dropped", dropped=dropped_lines,
                        market=market)
        return written

    # --- paid: /scores (UCL results) -------------------------------------------------

    def fetch_scores(self, days_from: int = 3) -> list[dict]:
        endpoint = "/sports/soccer_uefa_champs_league/scores"
        body, _ = self._paid_get(endpoint, {"daysFrom": days_from},
                                 expected_cost=2)
        assert isinstance(body, list)
        return body

    def apply_scores(self, body: list[dict]) -> dict:
        """Finished events -> results rows + finished status. Missing UCL
        matches are created from the payload itself (same team rule as
        /events)."""
        written = 0
        teams_created: list[str] = []
        with session_scope(self._db_url) as s:
            for ev in body:
                if not ev.get("completed"):
                    continue
                eid = str(ev.get("id"))
                match = s.scalars(select(Match).where(
                    Match.toa_event_id == eid)).first()
                if match is None:
                    home_name = str(ev.get("home_team"))
                    away_name = str(ev.get("away_team"))
                    commence = ev.get("commence_time")
                    kickoff = dt.datetime.fromisoformat(
                        commence.replace("Z", "+00:00")) \
                        if commence else dt.datetime.now(dt.UTC)
                    season = kickoff.year if kickoff.month >= 7 \
                        else kickoff.year - 1
                    home = self._ensure_toa_team(s, home_name, "ucl",
                                                 teams_created)
                    away = self._ensure_toa_team(s, away_name, "ucl",
                                                 teams_created)
                    match = self._find_ucl_by_pair(s, season, home.id,
                                                   away.id, kickoff)
                    if match is None:
                        match = Match(league="ucl", season=season,
                                      stage="league_phase",
                                      date_utc=kickoff.date(),
                                      commence_time_utc=kickoff,
                                      kickoff_source="toa",
                                      status="scheduled",
                                      home_team_id=home.id,
                                      away_team_id=away.id,
                                      toa_event_id=eid)
                        s.add(match)
                        s.flush()
                    else:
                        match.toa_event_id = eid
                scores = {sc.get("name"): sc.get("score")
                          for sc in ev.get("scores", [])}
                hg_raw = scores.get(ev.get("home_team"))
                ag_raw = scores.get(ev.get("away_team"))
                if hg_raw is None or ag_raw is None:
                    continue
                try:
                    hg, ag = int(hg_raw), int(ag_raw)
                except (TypeError, ValueError):
                    continue
                existing = s.scalars(select(Result).where(
                    Result.match_id == match.id)).first()
                if existing is None:
                    s.add(Result(match_id=match.id, home_goals=hg,
                                 away_goals=ag, source="toa",
                                 settled_at_utc=dt.datetime.now(dt.UTC)))
                    written += 1
                match.status = "finished"
        return {"completed_seen": sum(1 for e in body if e.get("completed")),
                "results_written": written,
                "teams_created": teams_created}

    # --- shared helpers ------------------------------------------------------------

    def _matcher(self,
                 session: Session) -> tuple[AliasMatcher, dict[int, str]]:
        aliases = {(a.source, a.alias): a.team_id
                   for a in session.scalars(select(TeamAlias)).all()}
        names = {t.id: t.name for t in session.scalars(select(Team)).all()}
        return AliasMatcher(aliases, names), names

    @staticmethod
    def _roster(session: Session, league_slug: str, season: int) -> set[int]:
        rows = session.execute(
            select(Match.home_team_id, Match.away_team_id).where(
                Match.league == league_slug,
                Match.season == season)).all()
        roster: set[int] = set()
        for home_id, away_id in rows:
            roster.update((home_id, away_id))
        return roster

    def _team_for(self, session: Session, matcher: AliasMatcher,
                  names: dict[int, str], roster: set[int], name: str,
                  league_slug: str, report: EventsReport) -> Team | None:
        """B6: deterministic match (exact -> normalized -> roster
        containment); on hit remember the toa alias; otherwise create the
        team (same rule for /events and /scores)."""
        m = matcher.match("toa", name, roster=roster)
        team = None
        if m.matched is not None:
            team = session.scalars(select(Team).where(
                Team.name == m.matched)).first()
        if team is None:
            team = self._ensure_toa_team(session, name, league_slug,
                                         report.teams_created)
        exists = session.scalars(select(TeamAlias).where(
            TeamAlias.source == "toa", TeamAlias.alias == name)).first()
        if exists is None:
            session.add(TeamAlias(team_id=team.id, source="toa",
                                  alias=name))
            matcher._aliases[("toa", name)] = team.id  # keep snapshot fresh
        return team

    def _ensure_toa_team(self, session: Session, name: str,
                         league_slug: str,
                         teams_created: list[str]) -> Team:
        alias = session.scalars(select(TeamAlias).where(
            TeamAlias.source == "toa", TeamAlias.alias == name)).first()
        if alias is not None:
            team = session.get(Team, alias.team_id)
            if team is not None:
                return team
        team = session.scalars(select(Team).where(
            Team.name == name)).first()
        if team is None:
            team = Team(name=name,
                        league=None if league_slug == "ucl" else league_slug)
            session.add(team)
            session.flush()
            teams_created.append(name)
        return team

    @staticmethod
    def _find_ucl_by_pair(session: Session, season: int, home_id: int,
                          away_id: int, kickoff: dt.datetime) -> Match | None:
        """S3: search by pair (season, home, away) with date +-1 before
        creating a UCL match (stage is 'league_phase' for both sources)."""
        lo = (kickoff - dt.timedelta(days=1)).date()
        hi = (kickoff + dt.timedelta(days=1)).date()
        return session.scalars(select(Match).where(
            Match.league == "ucl", Match.season == season,
            Match.home_team_id == home_id, Match.away_team_id == away_id,
            Match.date_utc >= lo, Match.date_utc <= hi)).first()

    @staticmethod
    def _relink_snapshots(session: Session, event_id: str,
                          match_id: int) -> int:
        """B6: attach previously stored snapshots to the matched match."""
        snaps = session.scalars(select(OddsSnapshot).where(
            OddsSnapshot.toa_event_id == event_id,
            OddsSnapshot.match_id.is_(None))).all()
        for snap in snaps:
            snap.match_id = match_id
        return len(snaps)
