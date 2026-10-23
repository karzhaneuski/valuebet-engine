"""Ingest CLI: python -m core.ingest backfill|refresh|events|odds-snapshot|scores."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

from sqlalchemy import select

from core.config import load_config, load_secrets
from core.db import session_scope
from core.ingest.football_data import FootballDataLoader
from core.ingest.odds_api import OddsApiClient
from core.ingest.understat import UnderstatLoader
from core.logging import configure, get_logger
from core.orm import Match

logger = get_logger("ingest.cli")

TOP5 = ["premier_league", "la_liga", "serie_a", "bundesliga", "ligue_1"]
SEASONS = [2021, 2022, 2023, 2024, 2025, 2026]


def _db_url() -> str:
    return load_config().database.url


def cmd_backfill(args: argparse.Namespace) -> None:
    config = load_config()
    db = _db_url()
    report: dict = {"football_data": [], "understat": []}
    us = UnderstatLoader(db, config)
    # 1. Understat schedule first (matches are the join base)
    for slug in TOP5:
        for season in SEASONS:
            if args.seasons and season not in args.seasons:
                continue
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


def backfill_lineups(db: str, config, since: str) -> dict:
    us = UnderstatLoader(db, config)
    since_d = dt.date.fromisoformat(since)
    total = 0
    with session_scope(db) as s:
        matches = s.scalars(select(Match).where(
                Match.date_utc >= since_d,
                Match.status == "finished",
                Match.understat_id.isnot(None)).order_by(Match.date_utc)
        ).all()
        pending = [m for m in matches]
    print(f"lineups: {len(pending)} finished matches since {since}")
    for m in pending:
        assert m.understat_id is not None  # filtered in the query
        rows = us.lineup_rows(m.understat_id)
        written = us.upsert_lineups(m, rows)
        total += written
        time.sleep(1.0)  # Understat is informal: throttle hard
    return {"since": since, "matches": len(pending), "rows_written": total}


def cmd_refresh(args: argparse.Namespace) -> None:
    """Re-download current-season files and re-upsert."""
    args.seasons = [2026]
    args.lineups_since = None
    cmd_backfill(args)


def cmd_events(args: argparse.Namespace) -> None:
    config = load_config()
    client = OddsApiClient(_db_url(), config,
                           load_secrets().odds_api_key)
    report = {}
    for league in config.leagues:
        if not league.toa_sport_key:
            continue
        if args.leagues and league.slug not in args.leagues:
            continue
        events = client.fetch_events(league.toa_sport_key)
        r = client.apply_events(league.toa_sport_key, events, league.slug,
                                season=2026)
        report[league.slug] = {"events": r.events_seen,
                               "created": r.matches_created,
                               "kickoffs_updated": r.kickoffs_updated,
                               "postponed_suspected":
                                   r.postponed_suspected,
                               "unmatched": r.unmatched_teams}
        print(f"events {league.slug}: {r.events_seen} events, "
              f"+{r.matches_created} new, {r.postponed_suspected} suspected")
    out = Path("reports/ingest/events_report.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str))


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
        event_id = args.event_id
        if not event_id:
            with session_scope(_db_url()) as s:
                match = s.scalars(select(Match).where(
                    Match.league == args.leagues,
                    Match.toa_event_id.isnot(None)).order_by(
                    Match.commence_time_utc.desc())).first()
                event_id = match.toa_event_id if match else None
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


def main(argv: list[str] | None = None) -> None:
    configure(dev_mode=True)
    parser = argparse.ArgumentParser(prog="core.ingest")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("backfill", help="fd + understat history load")
    p.add_argument("--seasons", type=int, nargs="*", default=None)
    p.add_argument("--lineups-since", type=str, default=None,
                   help="YYYY-MM-DD: load lineups for finished matches since")
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

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
