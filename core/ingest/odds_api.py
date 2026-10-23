"""The Odds API client: free /events, paid odds snapshots, /scores.

Cost model (phase 0): /sports and /events are free; each /odds call costs
markets x regions (1 credit per market for one region); /scores with
daysFrom costs 2 credits. Every paid call goes through QuotaGuard (hard
stop 450, pacing remaining/days_left) and is logged to api_quota_usage.

Alternate totals keep only half lines (x.5); whole and quarter lines are
dropped and counted (docs/decisions.md S11).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

import httpx
from sqlalchemy import select

from core.config import AppConfig
from core.db import session_scope
from core.ingest.quota import QuotaGuard
from core.logging import get_logger
from core.orm import Match, OddsSnapshot, Result, TeamAlias

logger = get_logger("ingest.odds_api")

BASE = "https://api.the-odds-api.com/v4"
TIER_LOWER_BOUND_MIN = {"T-48": 40 * 60, "T-24": 18 * 60, "T-3": 60,
                        "T-1": 30, "manual": 0}


@dataclass
class EventsReport:
    events_seen: int = 0
    kickoffs_updated: int = 0
    matches_created: int = 0
    postponed_suspected: int = 0
    unmatched_teams: list[str] = field(default_factory=list)


class OddsApiClient:
    def __init__(self, db_url: str, config: AppConfig, api_key: str,
                 client: httpx.Client | None = None) -> None:
        self._db_url = db_url
        self._config = config
        self._key = api_key
        self._http = client or httpx.Client(timeout=30.0)
        self.quota = QuotaGuard(db_url, config, provider="toa")

    # --- free: events ---------------------------------------------------------

    def fetch_events(self, sport_key: str) -> list[dict]:
        resp = self._http.get(
            f"{BASE}/sports/{sport_key}/events",
            params={"apiKey": self._key, "regions": "eu", "dateFormat": "iso"})
        resp.raise_for_status()
        return resp.json()

    def apply_events(self, sport_key: str, events: list[dict],
                     league_slug: str, season: int) -> EventsReport:
        """Upsert kickoffs (authoritative), create missing matches (UCL),
        apply postponement rules from config."""
        cfg = self._config.odds_policy.postponement
        report = EventsReport(events_seen=len(events))
        now = dt.datetime.now(dt.UTC)
        with session_scope(self._db_url) as s:
            aliases = {(a.source, a.alias): a.team_id
                       for a in s.scalars(select(TeamAlias)).all()}
            seen_event_ids: set[str] = set()
            for ev in events:
                report.events_seen += 1
                eid = str(ev["id"])
                seen_event_ids.add(eid)
                commence = dt.datetime.fromisoformat(
                    ev["commence_time"].replace("Z", "+00:00"))
                match = s.scalars(select(Match).where(
                    Match.toa_event_id == eid)).first()
                if match is None:
                    home_id = aliases.get(("toa", ev["home_team"]))
                    away_id = aliases.get(("toa", ev["away_team"]))
                    if home_id is None or away_id is None:
                        for nm in (ev["home_team"], ev["away_team"]):
                            if aliases.get(("toa", nm)) is None and \
                                    nm not in report.unmatched_teams:
                                report.unmatched_teams.append(nm)
                        continue
                    match = s.scalars(select(Match).where(
                        Match.league == league_slug,
                        Match.season == season,
                        Match.home_team_id == home_id,
                        Match.away_team_id == away_id)).first() \
                        if league_slug != "ucl" else None
                    if match is None:
                        match = Match(
                            league=league_slug, season=season,
                            date_utc=commence.date(),
                            commence_time_utc=commence,
                            kickoff_source="toa", status="scheduled",
                            home_team_id=home_id, away_team_id=away_id,
                            toa_event_id=eid)
                        s.add(match)
                        s.flush()
                        report.matches_created += 1
                # postponement: commence shifted by more than cfg minutes
                stored = _as_utc(match.commence_time_utc)
                if stored is not None:
                    shift = abs((commence - stored).total_seconds() / 60)
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
            # event disappeared from /events while its kickoff window is in
            # the near horizon -> postponed suspected (the KO+3h no-result
            # rule is settlement logic and belongs to phase 7)
            horizon_start = now - dt.timedelta(days=7)
            existing = s.scalars(select(Match).where(
                Match.toa_event_id.isnot(None),
                Match.league == league_slug)).all()
            for m in existing:
                if m.toa_event_id in seen_event_ids or m.status != "scheduled":
                    continue
                stored_m = _as_utc(m.commence_time_utc)
                if stored_m and horizon_start < \
                        stored_m <= now + dt.timedelta(days=7):
                    m.status = "postponed_suspected"
                    report.postponed_suspected += 1
        return report

    # --- paid: odds snapshots ---------------------------------------------------

    def _record_usage(self, endpoint: str, credits: int,
                      remaining: int | None) -> None:
        self.quota.record(endpoint, credits, remaining)

    def snapshot_sport_totals(self, sport_key: str, tier: str) -> dict:
        """One sport-level /odds call for totals (1 credit)."""
        endpoint = f"/sports/{sport_key}/odds"
        self.quota.check(1)
        resp = self._http.get(
            f"{BASE}{endpoint}",
            params={"apiKey": self._key, "regions": "eu", "markets": "totals",
                    "oddsFormat": "decimal"})
        remaining = _to_int(resp.headers.get("x-requests-remaining"))
        if resp.status_code != 200:
            self._record_usage(endpoint + ":failed", 0, remaining)
            resp.raise_for_status()
        self._record_usage(endpoint, 1, remaining)
        body = resp.json()
        planned = TIER_LOWER_BOUND_MIN[tier]
        written = self._store_events_odds(body, sport_key, "totals", tier,
                                          planned)
        return {"events": len(body), "rows": written, "credits": 1,
                "remaining": remaining}

    def snapshot_event_markets(self, sport_key: str, event_id: str,
                               markets: list[str], tier: str) -> dict:
        """Event-level markets (1 credit per market per region)."""
        endpoint = f"/sports/{sport_key}/events/{event_id}/odds"
        self.quota.check(len(markets))
        resp = self._http.get(
            f"{BASE}{endpoint}",
            params={"apiKey": self._key, "regions": "eu",
                    "markets": ",".join(markets), "oddsFormat": "decimal"})
        remaining = _to_int(resp.headers.get("x-requests-remaining"))
        if resp.status_code != 200:
            self._record_usage(endpoint + ":failed", 0, remaining)
            resp.raise_for_status()
        self._record_usage(endpoint, len(markets), remaining)
        planned = TIER_LOWER_BOUND_MIN[tier]
        written = 0
        for market in markets:
            written += self._store_events_odds(resp.json(), sport_key, market,
                                               tier, planned, event_id)
        return {"event": event_id, "rows": written,
                "credits": len(markets), "remaining": remaining}

    def _store_events_odds(self, body: list | dict, sport_key: str,
                           market: str, tier: str, planned_min: int,
                           only_event_id: str | None = None) -> int:
        events = body if isinstance(body, list) else [body]
        now = dt.datetime.now(dt.UTC)
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
                actual = None
                stored = _as_utc(
                    match.commence_time_utc if match else None)
                if stored is not None:
                    actual = int((stored - now).total_seconds() / 60)
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
                                # half lines only (x.5); whole (x.0) and
                                # quarter (x.25/x.75) lines are dropped and
                                # counted (docs/decisions.md S11)
                                if half_only and float(point) * 2 % 2 != 1:
                                    dropped_lines += 1
                                    continue
                                line = Decimal(str(point))
                            s.add(OddsSnapshot(
                                toa_sport_key=sport_key, toa_event_id=eid,
                                match_id=match.id if match else None,
                                bookmaker=bk.get("title") or bk.get("key"),
                                market=market, line=line,
                                outcome=str(o.get("name")),
                                price=Decimal(str(price)), tier=tier,
                                planned_offset_min=planned_min,
                                actual_offset_min=actual,
                                source="toa",
                                captured_at_utc=now))
                            written += 1
        if dropped_lines:
            logger.info("odds.lines.dropped", dropped=dropped_lines,
                        market=market)
        return written

    # --- paid: scores (UCL results) ----------------------------------------------

    def fetch_scores(self, days_from: int = 3) -> list[dict]:
        endpoint = "/sports/soccer_uefa_champs_league/scores"
        self.quota.check(2)
        resp = self._http.get(
            f"{BASE}{endpoint}",
            params={"apiKey": self._key, "daysFrom": days_from})
        remaining = _to_int(resp.headers.get("x-requests-remaining"))
        if resp.status_code != 200:
            self._record_usage(endpoint + ":failed", 0, remaining)
            resp.raise_for_status()
        self._record_usage(endpoint, 2, remaining)
        return resp.json()

    def apply_scores(self, body: list[dict]) -> dict:
        """Finished events -> results rows + finished status."""
        written = 0
        with session_scope(self._db_url) as s:
            for ev in body:
                if not ev.get("completed"):
                    continue
                eid = str(ev.get("id"))
                match = s.scalars(select(Match).where(
                    Match.toa_event_id == eid)).first()
                if match is None:
                    continue
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
                "results_written": written}


def _as_utc(value: dt.datetime | None) -> dt.datetime | None:
    """SQLite returns naive datetimes even for timezone-aware columns; our
    convention is UTC everywhere."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


def _to_int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None
