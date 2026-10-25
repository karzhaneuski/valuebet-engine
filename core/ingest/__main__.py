"""Ingest CLI: python -m core.ingest backfill|refresh|events|odds-snapshot|scores."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Protocol

from sqlalchemy import select

from core.config import load_config, load_secrets
from core.db import session_scope
from core.http import register_secret_values
from core.ingest.football_data import FootballDataLoader
from core.ingest.odds_api import OddsApiClient
from core.ingest.understat import UnderstatLoader
from core.logging import configure, get_logger
from core.orm import Match, MatchLineup

# C3: silence soccerdata config & cache chatter before any soccerdata import
os.environ.setdefault("SOCCERDATA_LOGLEVEL", "WARNING")
for _noisy in ("soccerdata", "TLSLibrary", "TLSRequests", "urllib3"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
try:
    import tls_requests.models.libraries as _lib
    _lib.logger.setLevel(logging.WARNING)
except (ImportError, AttributeError):
    pass

logger = get_logger("ingest.cli")

TOP5 = ["premier_league", "la_liga", "serie_a", "bundesliga", "ligue_1"]
SEASONS = [2021, 2022, 2023, 2024, 2025, 2026]


def _db_url() -> str:
    return load_config().database.url


def cmd_backfill(args: argparse.Namespace) -> None:
    if getattr(args, "lineups_only", False) and not args.lineups_since:
        sys.exit("--lineups-only requires --lineups-since YYYY-MM-DD")
    config = load_config()
    db = _db_url()
    report: dict = {"football_data": [], "understat": []}
    if not getattr(args, "lineups_only", False):
        us = UnderstatLoader(db, config)
        # 1. Understat schedule first (matches are the join base)
        for slug in TOP5:
            for season in SEASONS:
                if args.seasons and season not in args.seasons:
                    continue
                try:
                    if season < 2022:
                        us.backfill_archive(slug, season)  # one-time download
                    us_rows = us.schedule_rows(slug, season)
                    us_r = us.upsert_schedule(us_rows)
                    report["understat"].append(
                        {"league": slug, "season": season,
                         "created": us_r.matches_created,
                         "existing": us_r.matches_existing,
                         "stats": us_r.stats_written,
                         "unmatched": sorted(set(us_r.unmatched))})
                    print(f"understat {slug} {season}: +{us_r.matches_created} "
                          f"(~{us_r.matches_existing} existing), "
                          f"stats {us_r.stats_written}")
                except Exception as exc:
                    err_msg = f"{type(exc).__name__}: {exc}"
                    logger.error("understat.schedule.failed", league=slug,
                                 season=season, error=err_msg)
                    if "understat_errors" not in report:
                        report["understat_errors"] = []
                    report["understat_errors"].append({
                        "league": slug, "season": season, "error": err_msg,
                    })
                    print(f"understat {slug} {season} FAILED: {err_msg}")
                    continue
        # 2. football-data results + closing odds
        fd = FootballDataLoader(db, config)
        for slug in TOP5:
            for season in SEASONS:
                if args.seasons and season not in args.seasons:
                    continue
                fd.download(slug, season)
                fd_rows = fd.parse(slug, season)
                fd_r = fd.upsert(fd_rows)
                report["football_data"].append(
                    {"league": slug, "season": season, "rows": fd_r.rows,
                     "matched": fd_r.matched,
                     "missing_match": fd_r.missing_match,
                     "unmatched_teams": sorted(set(fd_r.unmatched_teams)),
                     "date_mismatches": fd_r.date_mismatches,
                     "quarantined": fd_r.quarantined,
                     "inverted_pairs": fd_r.inverted_pairs,
                     "closing_written": fd_r.closing_written})
                print(f"fd {slug} {season}: {fd_r.matched}/{fd_r.rows} matched, "
                      f"closing {fd_r.closing_written}, "
                      f"unmatched {sorted(set(fd_r.unmatched_teams))}")
    # 3. optional lineups backfill from a date
    if args.lineups_since:
        lineups = backfill_lineups(db, config, args.lineups_since)
        report["lineups"] = lineups
    out = Path("reports/ingest/backfill_report.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str))
    print(f"report saved: {out}")
    if report.get("understat_errors"):
        sys.exit(f"backfill completed with {len(report['understat_errors'])} understat error(s)")


class LineupsLoader(Protocol):
    """Anything that can fetch lineup rows for an Understat game."""

    def lineup_rows(self, understat_id: int, league_slug: str,
                    season: int) -> list[dict]: ...
    def upsert_lineups(self, match, rows: list[dict]) -> int: ...


def backfill_lineups(db: str, config, since: str,
                     loader: LineupsLoader | None = None) -> dict:
    """S6: lineups backfill.

    - only matches WITHOUT lineups are taken (NOT EXISTS, checked before
      any download), so a re-run resumes where the last one stopped;
    - each match is retried on 429/5xx with a pause; after the attempts
      are exhausted the match goes to the report as failed and the run
      continues;
    - the report is refreshed after every match.
    """
    us = loader if loader is not None else UnderstatLoader(db, config)
    since_d = dt.date.fromisoformat(since)
    with session_scope(db) as s:
        has_lineups = select(MatchLineup.match_id).where(
            MatchLineup.match_id == Match.id).exists()
        pending = s.scalars(select(Match).where(
            Match.date_utc >= since_d,
            Match.status == "finished",
            Match.understat_id.isnot(None),
            ~has_lineups).order_by(Match.date_utc)).all()
        todo = [(m.id, m.understat_id, m.league, m.season) for m in pending
                if m.understat_id is not None]
    print(f"lineups: {len(todo)} matches without lineups since {since}")
    written_total = 0
    failed: list[dict] = []
    for i, (match_id, understat_id, league_slug, season) in enumerate(todo, 1):
        attempts = config.ingest.retry_attempts
        written: int | None = None
        rows: list[dict] | None = None
        for attempt in range(1, attempts + 1):
            try:
                rows = us.lineup_rows(understat_id, league_slug, season)
                break
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"[:300]
                if attempt == attempts:
                    logger.warning("lineups.match.failed",
                                   match_id=match_id,
                                   understat_id=understat_id,
                                   error=last_error)
                    failed.append({"match_id": match_id,
                                   "understat_id": understat_id,
                                   "error": last_error})
                    written = None
                    break
                time.sleep(config.ingest.retry_backoff_s * attempt)
        if written is None and failed and failed[-1]["match_id"] == match_id:
            continue  # exhausted retries: report and move on
        db_match = s_get_match(db, match_id)
        if db_match is None or rows is None:
            continue
        try:
            written = us.upsert_lineups(db_match, rows)
        except Exception as exc:
            error_text = f"{type(exc).__name__}: {exc}"[:300]
            logger.warning("lineups.match.failed", match_id=match_id,
                           error=error_text)
            failed.append({"match_id": match_id, "understat_id":
                           understat_id, "error": error_text})
            continue
        written_total += written
        if i % 10 == 0 or i == len(todo):
            print(f"lineups progress: {i}/{len(todo)}, rows {written_total}")
        time.sleep(1.0)  # Understat is informal: throttle hard
    return {"since": since, "matches": len(todo),
            "rows_written": written_total, "failed_matches": failed}


def s_get_match(db: str, match_id: int):
    with session_scope(db) as s:
        return s.get(Match, match_id)


def cmd_refresh(args: argparse.Namespace) -> None:
    """Re-download current-season files and re-upsert."""
    args.seasons = [2026]
    args.lineups_since = None
    cmd_backfill(args)


def current_season(now: dt.datetime | None = None) -> int:
    """League season for a date: 2026/27 -> 2026 (starts in July)."""
    now = now or dt.datetime.now(dt.UTC)
    return now.year if now.month >= 7 else now.year - 1


def cmd_events(args: argparse.Namespace) -> None:
    config = load_config()
    client = OddsApiClient(_db_url(), config,
                           load_secrets().odds_api_key)
    season = current_season()
    report = {}
    for league in config.leagues:
        if not league.toa_sport_key:
            continue
        if args.leagues and league.slug not in args.leagues:
            continue
        events = client.fetch_events(league.toa_sport_key)
        r = client.apply_events(league.toa_sport_key, events, league.slug,
                                season=season)
        report[league.slug] = {"events": r.events_seen,
                               "created": r.matches_created,
                               "kickoffs_updated": r.kickoffs_updated,
                               "postponed_suspected":
                                   r.postponed_suspected,
                               "teams_created": r.teams_created,
                               "unmatched": r.unmatched_teams}
        print(f"events {league.slug}: {r.events_seen} events, "
              f"+{r.matches_created} new, {r.postponed_suspected} suspected")
    out = Path("reports/ingest/events_report.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str))


def pick_latest_event_id(db_url: str, league: str) -> str | None:
    """TOA event id of the league's most recently scheduled match
    (S9: odds-snapshot works without an explicit --event-id)."""
    with session_scope(db_url) as s:
        match = s.scalars(select(Match).where(
            Match.league == league,
            Match.toa_event_id.isnot(None)).order_by(
            Match.commence_time_utc.desc())).first()
        return match.toa_event_id if match else None


def cmd_odds_snapshot(args: argparse.Namespace) -> None:
    config = load_config()
    client = OddsApiClient(_db_url(), config,
                           load_secrets().odds_api_key)
    league = next(lg for lg in config.leagues if lg.slug == args.league)
    if args.markets == ["totals"]:
        assert league.toa_sport_key is not None
        result = client.snapshot_sport_totals(league.toa_sport_key,
                                              args.tier)
    else:
        event_id = args.event_id or pick_latest_event_id(_db_url(),
                                                         args.league)
        if not event_id:
            sys.exit("event-id required for event-level markets")
        assert league.toa_sport_key is not None
        result = client.snapshot_event_markets(
            league.toa_sport_key, event_id, args.markets, args.tier)
    print(json.dumps(result))


def cmd_scores(args: argparse.Namespace) -> None:
    config = load_config()
    client = OddsApiClient(_db_url(), config,
                           load_secrets().odds_api_key)
    body = client.fetch_scores(days_from=args.days_from)
    result = client.apply_scores(body)
    print(json.dumps(result))


def build_parser() -> argparse.ArgumentParser:
    """CLI parser (exposed for tests)."""
    parser = argparse.ArgumentParser(prog="core.ingest")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("backfill", help="fd + understat history load")
    p.add_argument("--seasons", type=int, nargs="*", default=None)
    p.add_argument("--lineups-since", type=str, default=None,
                   help="YYYY-MM-DD: load lineups for finished matches since")
    p.add_argument("--lineups-only", action="store_true", default=False,
                   help="only backfill lineups (requires --lineups-since)")
    p.set_defaults(func=cmd_backfill)

    p = sub.add_parser("refresh", help="re-pull current season files")
    p.set_defaults(func=cmd_refresh)

    p = sub.add_parser("events", help="TOA /events: kickoffs + postponements")
    p.add_argument("--leagues", nargs="*", default=None)
    p.set_defaults(func=cmd_events)

    p = sub.add_parser("odds-snapshot", help="one-shot odds snapshot")
    p.add_argument("--league", default="premier_league")
    p.add_argument("--markets", nargs="*",
                   default=["totals"],
                   help="totals | correct_score btts alternate_totals")
    p.add_argument("--tier", default="T-24",
                   choices=["T-48", "T-24", "T-3", "T-1"])
    p.add_argument("--event-id", default=None,
                   help="event-level markets: TOA event id")
    p.set_defaults(func=cmd_odds_snapshot)

    p = sub.add_parser("scores", help="TOA /scores daysFrom=3 (UCL results)")
    p.add_argument("--days-from", type=int, default=3)
    p.set_defaults(func=cmd_scores)

    p = sub.add_parser("checks",
                       help="run all database invariants, exit != 0 on FAIL")
    p.set_defaults(func=cmd_checks)

    return parser


def main(argv: list[str] | None = None) -> None:
    configure(dev_mode=True)
    # S11: runtime secret values must be redacted from any log line
    secrets = load_secrets()
    register_secret_values([v for v in (
        secrets.odds_api_key, secrets.api_football_key,
        secrets.tavily_api_key, secrets.brave_search_api_key,
        secrets.gemini_api_key, secrets.api_token) if v])
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


def cmd_checks(args: argparse.Namespace) -> None:
    from core.ingest.checks_runner import run_all_checks

    failed = run_all_checks(_db_url(), load_config())
    if failed:
        sys.exit(f"checks FAILED: {failed} invariant(s) violated")


if __name__ == "__main__":
    main()
