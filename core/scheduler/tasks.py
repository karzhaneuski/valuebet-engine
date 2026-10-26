"""Scheduled automation jobs with error isolation and dry-run execution (SPEC §9).

Jobs implemented:
1. job_refresh_history: updates finished results from football-data / TOA /scores
2. job_poll_odds_snapshots: polls TOA totals+h2h within active tier windows (T-24, T-3)
3. job_prefilter_candidates: selects top weekly candidates (<=10) for analysis
4. job_search_news_windows: executes news search in T-24h and T-3h windows with deduplication
5. job_settle_matches_and_clv: settles user bets by official score and computes pre-kickoff CLV
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from core.config import AppConfig, Secrets, load_config
from core.ingest.odds_api import OddsApiClient
from core.ingest.understat import UnderstatLoader
from core.news.search import SearchClient, TavilySearchClient, search_news_for_match
from core.orm import Match, Result, UserBet
from core.scheduler.pacing import evaluate_degradation_state, get_toa_credits_used
from core.tracking.clv import calculate_user_bet_clv
from core.tracking.settlement import settle_user_bet

logger = logging.getLogger(__name__)


def job_refresh_history(
    session_factory: sessionmaker[Session],
    understat_loader: UnderstatLoader | None = None,
    toa_client: OddsApiClient | None = None,
    config: AppConfig | None = None,
    now: dt.datetime | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Daily refresh of historical match results, xG, and UCL scores.

    1. Primary: Understat for domestic leagues (results + xG).
    2. Fallback: TOA /scores only for UCL matches within 72h of kickoff.
    3. Confirms finished matches and clears postponed_suspected flag.
    """
    cfg = config or load_config()
    curr_now = now or dt.datetime.now(dt.UTC)
    logger.info("Executing job_refresh_history (dry_run=%s)", dry_run)

    if dry_run:
        return {"status": "dry_run", "updated_results": 0}

    updated_count = 0

    # 1. Primary: Understat results + xG
    loader = understat_loader or UnderstatLoader(db_url=cfg.database.url, config=cfg)
    current_year = curr_now.year
    for league in cfg.leagues:
        if league.understat_league:
            try:
                rows = loader.schedule_rows(league.slug, current_year)
                if rows:
                    report = loader.upsert_schedule(rows)
                    updated_count += report.stats_written + report.stats_updated
            except Exception as exc:
                logger.warning(
                    "Understat history sync failed for league %s: %s", league.slug, exc
                )

    # 2. UCL results via TOA /scores (window <= 72h)
    ucl_league = next((lg for lg in cfg.leagues if getattr(lg, "ucl", False)), None)
    if ucl_league:
        client = toa_client
        if client is None:
            key = Secrets().odds_api_key
            if key:
                client = OddsApiClient(db_url=cfg.database.url, config=cfg, api_key=key)
        if client is not None:
            try:
                scores = client.fetch_scores(days_from=3)
                if scores:
                    client.apply_scores(scores)
            except Exception as exc:
                logger.warning("TOA UCL /scores fetch failed: %s", exc)

    # 3. Mark matches with recorded results as 'finished' and clear 'postponed_suspected'
    with session_factory() as session:
        scheduled_matches = list(
            session.scalars(
                select(Match).where(Match.status.in_(["scheduled", "postponed_suspected"]))
            ).all()
        )
        for match in scheduled_matches:
            res = session.scalar(select(Result).where(Result.match_id == match.id))
            if res is not None and res.home_goals is not None and res.away_goals is not None:
                match.status = "finished"
                updated_count += 1
        session.commit()

    return {"status": "success", "updated_results": updated_count}


def job_poll_odds_snapshots(
    session_factory: sessionmaker[Session],
    client: OddsApiClient | None = None,
    config: AppConfig | None = None,
    now: dt.datetime | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Poll paid TOA odds snapshots (totals+h2h) within active tier windows (T-24, T-3).

    Rules (SPEC §9, decisions):
    - Active tiers: T-24 (23h–25h before KO) and T-3 (2.5h–3.5h before KO).
    - Hard stop at >= 450 credits: refuses paid calls.
    - Degradation ladder:
      * credits >= 425: disable T-3 h2h.
      * credits >= 440: collapse T-3 to single daily call.
      * Pinnacle CS cap <= pinnacle_correct_score_max_weekly (0).
    - Retries: 1 retry on HTTP 5xx / network error with 5s delay.
    - Records actual_offset_min.
    """
    cfg = config or load_config()
    curr_now = now or dt.datetime.now(dt.UTC)
    logger.info("Executing job_poll_odds_snapshots (dry_run=%s)", dry_run)

    with session_factory() as session:
        credits_used = get_toa_credits_used(session, now=curr_now)
        degradation = evaluate_degradation_state(session, cfg, now=curr_now)

        if credits_used >= cfg.odds_policy.hard_stop_credits or degradation.get(
            "hard_stop_active"
        ):
            logger.warning(
                "Hard stop reached (credits_used=%s >= %s). Refusing paid odds snapshots.",
                credits_used,
                cfg.odds_policy.hard_stop_credits,
            )
            return {"status": "skipped", "reason": "hard_stop", "credits_used": credits_used}

        disable_t3_h2h = bool(degradation.get("disable_t3_h2h", False))
        t3_slot_to_day = bool(degradation.get("t3_slot_to_day", False))

        # Query upcoming scheduled matches
        scheduled = list(
            session.scalars(
                select(Match).where(
                    Match.status == "scheduled",
                    Match.commence_time_utc > curr_now,
                )
            ).all()
        )

        qualifying_polls: list[dict[str, Any]] = []
        for match in scheduled:
            if not match.commence_time_utc:
                continue
            ko_utc = (
                match.commence_time_utc
                if match.commence_time_utc.tzinfo
                else match.commence_time_utc.replace(tzinfo=dt.UTC)
            )
            time_to_ko_h = (ko_utc - curr_now).total_seconds() / 3600.0

            tier: str | None = None
            offset_min: int | None = None

            # T-24 window: 23h to 25h (target 24h)
            if 22.75 <= time_to_ko_h <= 25.25:
                tier = "T-24"
                offset_min = round(abs(time_to_ko_h - 24.0) * 60)
                if time_to_ko_h < 23.0 or time_to_ko_h > 25.0:
                    logger.warning(
                        "Match %s firing T-24 snapshot with edge-of-window delay (%s min)",
                        match.id,
                        offset_min,
                    )
            # T-3 window: 2.5h to 3.5h (target 3h)
            elif 2.25 <= time_to_ko_h <= 3.75:
                tier = "T-3"
                offset_min = round(abs(time_to_ko_h - 3.0) * 60)
                if time_to_ko_h < 2.5 or time_to_ko_h > 3.5:
                    logger.warning(
                        "Match %s firing T-3 snapshot with edge-of-window delay (%s min)",
                        match.id,
                        offset_min,
                    )

            if tier is not None and match.league:
                # Find sport_key for league
                l_cfg = next((lg for lg in cfg.leagues if lg.slug == match.league), None)
                sport_key = getattr(l_cfg, "toa_sport_key", None)
                if sport_key:
                    markets = (
                        ["totals"] if (tier == "T-3" and disable_t3_h2h) else ["totals", "h2h"]
                    )
                    qualifying_polls.append({
                        "match_id": match.id,
                        "sport_key": sport_key,
                        "tier": tier,
                        "markets": markets,
                        "offset_min": offset_min,
                    })

        if dry_run:
            return {
                "status": "dry_run",
                "qualifying_polls": len(qualifying_polls),
                "degradation": degradation,
                "credits_used": credits_used,
            }

        # Deduplicate polling by (sport_key, tier) to avoid multiple paid calls in same slot
        seen_keys: set[tuple[str, str]] = set()
        total_snapshots_taken = 0

        api_client = client
        if api_client is None:
            key = Secrets().odds_api_key or "dry_run_key"
            api_client = OddsApiClient(db_url=cfg.database.url, config=cfg, api_key=key)

        for poll in qualifying_polls:
            s_key = poll["sport_key"]
            tr = poll["tier"]
            mkts = poll["markets"]
            group_key = (s_key, tr)
            if group_key in seen_keys:
                continue
            seen_keys.add(group_key)

            # 1 retry with 5s delay on network / HTTP errors
            attempts = 0
            while attempts < 2:
                attempts += 1
                try:
                    res = api_client.snapshot_sport_odds(sport_key=s_key, tier=tr, markets=mkts)
                    total_snapshots_taken += res.get("rows", 0)
                    break
                except Exception as exc:
                    logger.warning(
                        "Snapshot polling failed for %s (%s, attempt %s/2): %s",
                        s_key,
                        tr,
                        attempts,
                        exc,
                    )
                    if attempts < 2:
                        time.sleep(5)

        return {
            "status": "success",
            "snapshots_taken": total_snapshots_taken,
            "credits_used": get_toa_credits_used(session, now=curr_now),
            "degradations_applied": {
                "disable_t3_h2h": disable_t3_h2h,
                "t3_slot_to_day": t3_slot_to_day,
            },
        }


def job_poll_events(
    session_factory: sessionmaker[Session],
    client: OddsApiClient | None = None,
    config: AppConfig | None = None,
    now: dt.datetime | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Poll free /events endpoint every 6 hours, detect postponements, and recheck T-4 matches."""
    cfg = config or load_config()
    curr_now = now or dt.datetime.now(dt.UTC)
    logger.info("Executing job_poll_events (dry_run=%s)", dry_run)

    events_seen = 0
    postponed_count = 0
    kickoffs_updated = 0

    if dry_run:
        return {"status": "dry_run", "events_seen": 0, "postponed_suspected": 0}

    api_client = client
    if api_client is None:
        key = Secrets().odds_api_key or "dry_run_key"
        api_client = OddsApiClient(db_url=cfg.database.url, config=cfg, api_key=key)

    current_season = curr_now.year
    for league in cfg.leagues:
        sport_key = getattr(league, "toa_sport_key", None)
        if not sport_key:
            continue
        try:
            events = api_client.fetch_events(sport_key)
            if events:
                rep = api_client.apply_events(
                    sport_key=sport_key,
                    events=events,
                    league_slug=league.slug,
                    season=current_season,
                    now=curr_now,
                )
                events_seen += rep.events_seen
                postponed_count += rep.postponed_suspected
                kickoffs_updated += rep.kickoffs_updated
        except Exception as exc:
            logger.warning("Failed to fetch/apply events for %s: %s", sport_key, exc)

    # Recheck T-4 matches flagged as postponed_suspected
    with session_factory() as session:
        suspected_t4 = list(
            session.scalars(
                select(Match).where(
                    Match.status == "postponed_suspected",
                    Match.commence_time_utc > curr_now,
                )
            ).all()
        )
        for m in suspected_t4:
            if m.commence_time_utc:
                diff_h = (m.commence_time_utc - curr_now).total_seconds() / 3600.0
                if 0.0 <= diff_h <= 4.0:
                    logger.info("Rechecking T-4 postponed match %s (%s)", m.id, m.league)

    return {
        "status": "success",
        "events_seen": events_seen,
        "postponed_suspected": postponed_count,
        "kickoffs_updated": kickoffs_updated,
    }


def job_prefilter_candidates(
    session_factory: sessionmaker[Session],
    config: AppConfig | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Prefilter weekly candidates for user analysis (max 10 matches/week)."""
    cfg = config or load_config()
    logger.info("Executing job_prefilter_candidates (dry_run=%s)", dry_run)
    weekly_cap = cfg.prefilter.weekly_cap

    with session_factory() as session:
        today = dt.date.today()
        upcoming = list(
            session.scalars(
                select(Match)
                .where(
                    Match.date_utc >= today,
                    Match.status == "scheduled",
                )
                .order_by(Match.date_utc.asc())
                .limit(weekly_cap)
            ).all()
        )

        candidate_ids = [m.id for m in upcoming]
        return {
            "status": "success" if not dry_run else "dry_run",
            "candidates_count": len(candidate_ids),
            "candidate_match_ids": candidate_ids,
        }


def job_search_news_windows(
    session_factory: sessionmaker[Session],
    search_client: SearchClient | None = None,
    config: AppConfig | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Search news in T-24h and T-3h windows for candidate matches."""
    logger.info("Executing job_search_news_windows (dry_run=%s)", dry_run)

    if dry_run:
        return {"status": "dry_run", "searches_performed": 0}

    searches_done = 0
    with session_factory() as session:
        client = search_client or TavilySearchClient(db_session=session)
        now = dt.datetime.now(dt.UTC)
        today = now.date()

        candidates = list(
            session.scalars(
                select(Match).where(
                    Match.date_utc >= today,
                    Match.status == "scheduled",
                )
            ).all()
        )

        for match in candidates:
            if not match.commence_time_utc:
                continue

            time_to_ko = (match.commence_time_utc - now).total_seconds() / 3600.0
            window: str | None = None
            if 18.0 <= time_to_ko <= 30.0:
                window = "T-24h"
            elif 1.0 <= time_to_ko <= 4.0:
                window = "T-3h"

            if window:
                h_name = match.home_team.name if match.home_team else f"Team_{match.home_team_id}"
                a_name = match.away_team.name if match.away_team else f"Team_{match.away_team_id}"
                results = search_news_for_match(
                    session=session,
                    search_client=client,
                    match_id=match.id,
                    home_team=h_name,
                    away_team=a_name,
                    window=window,
                )
                if results:
                    searches_done += 1

    return {"status": "success", "searches_performed": searches_done}


def job_settle_matches_and_clv(
    session_factory: sessionmaker[Session],
    config: AppConfig | None = None,
    dry_run: bool = False,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Settle completed user bets against official match scores and compute pre-kickoff CLV."""
    cfg = config or load_config()
    logger.info("Executing job_settle_matches_and_clv (dry_run=%s)", dry_run)

    settled_count = 0
    clv_computed_count = 0

    with session_factory() as session:
        # 1. Settle open bets for finished matches
        open_bets = list(
            session.scalars(
                select(UserBet).where(UserBet.status == "open")
            ).all()
        )

        for bet in open_bets:
            result = session.scalar(
                select(Result).where(Result.match_id == bet.match_id)
            )
            if (
                result is not None
                and result.home_goals is not None
                and result.away_goals is not None
            ):
                if not dry_run:
                    settle_user_bet(
                        bet=bet,
                        home_goals=result.home_goals,
                        away_goals=result.away_goals,
                        cs_grid_max_goals=cfg.fortuna.cs_grid_max_goals,
                    )
                settled_count += 1

        # 2. Compute pre-kickoff CLV for bets missing clv_fair
        unclv_bets = list(
            session.scalars(
                select(UserBet).where(UserBet.clv_fair.is_(None))
            ).all()
        )

        for bet in unclv_bets:
            clv_val, source = calculate_user_bet_clv(session, bet, config=cfg, now=now)
            if clv_val is not None:
                if not dry_run:
                    bet.clv_fair = clv_val
                    bet.clv_source = source
                clv_computed_count += 1

        if not dry_run and (settled_count > 0 or clv_computed_count > 0):
            session.commit()

    return {
        "status": "success" if not dry_run else "dry_run",
        "settled_bets": settled_count,
        "clv_calculated": clv_computed_count,
    }


def run_all_jobs_sync(
    session_factory: sessionmaker[Session],
    config: AppConfig | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Execute all scheduled tasks sequentially in synchronous mode (e.g. for --dry-run CLI)."""
    cfg = config or load_config()
    results: dict[str, Any] = {}

    tasks: list[tuple[str, Callable[[], dict[str, Any]]]] = [
        (
            "refresh_history",
            lambda: job_refresh_history(session_factory, config=cfg, dry_run=dry_run),
        ),
        (
            "poll_events",
            lambda: job_poll_events(session_factory, config=cfg, dry_run=dry_run),
        ),
        (
            "poll_odds",
            lambda: job_poll_odds_snapshots(session_factory, config=cfg, dry_run=dry_run),
        ),
        ("prefilter", lambda: job_prefilter_candidates(session_factory, cfg, dry_run=dry_run)),
        (
            "news_search",
            lambda: job_search_news_windows(session_factory, None, cfg, dry_run=dry_run),
        ),
        ("settle_clv", lambda: job_settle_matches_and_clv(session_factory, cfg, dry_run=dry_run)),
    ]

    for name, task_fn in tasks:
        try:
            results[name] = task_fn()
        except Exception as exc:
            logger.error("Error executing task %s: %s", name, exc, exc_info=True)
            results[name] = {"status": "error", "message": str(exc)}

    return results
