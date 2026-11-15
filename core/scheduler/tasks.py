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
from typing import Any, Literal

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, sessionmaker

from core.config import AppConfig, Secrets, load_config
from core.ingest.odds_api import OddsApiClient
from core.ingest.results import populate_results
from core.ingest.understat import UnderstatLoader, UnderstatUnavailableError
from core.llm.provider import GeminiProvider
from core.news.extractor import extract_player_news
from core.news.search import SearchClient, TavilySearchClient, search_news_for_match
from core.orm import (
    Match,
    OddsPollAttempt,
    OddsSnapshot,
    PlayerAvailability,
    Result,
    UserBet,
)
from core.scheduler.pacing import evaluate_degradation_state, get_toa_credits_used
from core.tracking.clv import calculate_user_bet_clv
from core.tracking.settlement import settle_user_bet
from core.value.fair_matrix import MarketInputs, build_fair_matrix
from core.value.persistence import load_market_inputs_for_match
from core.value.prefilter import CandidateMatch, filter_candidates

logger = logging.getLogger(__name__)


def job_refresh_history(
    session_factory: sessionmaker[Session],
    understat_loader: UnderstatLoader | None = None,
    toa_client: OddsApiClient | None = None,
    config: AppConfig | None = None,
    now: dt.datetime | None = None,
    dry_run: bool = False,
    on_source_error: Literal["stop", "fallback"] = "stop",
    leagues: list[str] | None = None,
    season: int | None = None,
    since: dt.date | None = None,
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
        return {
            "status": "dry_run",
            "updated_results": 0,
            "results_inserted": 0,
            "results_updated": 0,
            "leagues_done": [],
            "sources_used": {},
        }

    # Validate leagues filter if supplied
    all_cfg_slugs = {lg.slug for lg in cfg.leagues}
    if leagues is not None:
        for slug in leagues:
            if slug not in all_cfg_slugs:
                raise ValueError(f"Unknown league slug: {slug!r}")

    season_year = season if season is not None else (
        curr_now.year if curr_now.month >= 7 else curr_now.year - 1
    )

    updated_count = 0
    results_inserted = 0
    results_updated = 0
    leagues_done: list[str] = []
    sources_used: dict[str, str] = {}
    failed_leagues: dict[str, str] = {}
    touched_match_ids: set[int] = set()

    # 1. Primary: Understat results + xG
    loader = understat_loader or UnderstatLoader(db_url=cfg.database.url, config=cfg)
    target_leagues = [
        lg for lg in cfg.leagues
        if lg.understat_league and (leagues is None or lg.slug in leagues)
    ]

    for league in target_leagues:
        try:
            rows = loader.schedule_rows(league.slug, season_year)
            if since is not None:
                rows = [r for r in rows if r.naive_start.date() >= since]
            if rows:
                report = loader.upsert_schedule(rows)
                updated_count += report.stats_written + report.stats_updated
                if report.stats_written > 0 or report.stats_updated > 0:
                    with session_factory() as session:
                        u_ids = [
                            int(r.understat_id)
                            for r in rows
                            if r.is_result and r.home_goals is not None
                        ]
                        if u_ids:
                            matches = session.scalars(
                                select(Match.id).where(Match.understat_id.in_(u_ids))
                            ).all()
                            touched_match_ids.update(matches)
            leagues_done.append(league.slug)
            sources_used[league.slug] = "understat"
        except UnderstatUnavailableError as exc:
            err_text = str(exc)[:200]
            logger.warning(
                "Understat history sync failed for league %s: %s",
                league.slug,
                exc,
            )
            if on_source_error == "stop":
                return {
                    "status": "source_unavailable",
                    "source": "understat",
                    "league": league.slug,
                    "error": err_text,
                    "leagues_done": leagues_done,
                    "updated_results": updated_count,
                    "results_inserted": results_inserted,
                    "results_updated": results_updated,
                    "sources_used": sources_used,
                }
            # Fallback path
            failed_leagues[league.slug] = "understat"
            try:
                from core.ingest.football_data import FootballDataLoader

                fd_loader = FootballDataLoader(db_url=cfg.database.url, config=cfg)
                fd_loader.download(league.slug, season_year)
                fd_rows = fd_loader.parse(league.slug, season_year)
                if since is not None:
                    fd_rows = [r for r in fd_rows if r.date >= since]
                fd_report = fd_loader.upsert(fd_rows)
                updated_count += fd_report.stats_written + fd_report.stats_updated
                if fd_report.stats_written > 0 or fd_report.stats_updated > 0:
                    with session_factory() as session:
                        fd_dates = [r.date for r in fd_rows]
                        if fd_dates:
                            matches = session.scalars(
                                select(Match.id).where(
                                    Match.league == league.slug,
                                    Match.season == season_year,
                                    Match.date_utc.in_(fd_dates),
                                )
                            ).all()
                            touched_match_ids.update(matches)
                leagues_done.append(league.slug)
                sources_used[league.slug] = "football-data"
            except Exception as fd_exc:
                logger.warning(
                    "Football-data fallback also failed for league %s: %s",
                    league.slug,
                    fd_exc,
                )
                failed_leagues[league.slug] = "football-data"

    # 2. UCL results via TOA /scores (window <= 72h)
    ucl_league = next((lg for lg in cfg.leagues if getattr(lg, "ucl", False)), None)
    if ucl_league and (leagues is None or ucl_league.slug in leagues):
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

    # 3. Populate Result rows from MatchStat for finished matches, update status and clear postponed
    with session_factory() as session:
        ins, upd = populate_results(session, touched_match_ids=touched_match_ids, now=curr_now)
        results_inserted += ins
        results_updated += upd
        session.commit()

    overall_status = "partial" if failed_leagues else "success"
    return {
        "status": overall_status,
        "updated_results": updated_count,
        "results_inserted": results_inserted,
        "results_updated": results_updated,
        "leagues_done": leagues_done,
        "sources_used": sources_used,
        "failed_sources": failed_leagues,
    }


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
        if not dry_run and client is None and not Secrets().odds_api_key:
            return {"status": "skipped", "reason": "no_api_key"}

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
            if not match.toa_event_id:
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
                    # Query DB to check if this slot was already polled
                    if tier == "T-3" and t3_slot_to_day:
                        start_of_day = dt.datetime.combine(
                            ko_utc.date(), dt.time.min, tzinfo=dt.UTC
                        )
                        end_of_day = dt.datetime.combine(
                            ko_utc.date(), dt.time.max, tzinfo=dt.UTC
                        )
                        already_polled = session.scalar(
                            select(OddsSnapshot.id)
                            .outerjoin(Match, Match.id == OddsSnapshot.match_id)
                            .where(
                                OddsSnapshot.toa_sport_key == sport_key,
                                OddsSnapshot.tier == "T-3",
                                (
                                    (Match.commence_time_utc >= start_of_day)
                                    & (Match.commence_time_utc <= end_of_day)
                                )
                                | (
                                    (OddsSnapshot.captured_at_utc >= start_of_day)
                                    & (OddsSnapshot.captured_at_utc <= end_of_day)
                                ),
                            )
                            .limit(1)
                        )
                    else:
                        already_polled = session.scalar(
                            select(OddsSnapshot.id)
                            .outerjoin(Match, Match.id == OddsSnapshot.match_id)
                            .where(
                                OddsSnapshot.toa_sport_key == sport_key,
                                OddsSnapshot.tier == tier,
                                (Match.commence_time_utc == ko_utc)
                                | (OddsSnapshot.match_id == match.id),
                            )
                            .limit(1)
                        )

                    if already_polled is not None:
                        continue

                    sport_leagues = [
                        league.slug for league in cfg.leagues
                        if league.toa_sport_key == sport_key
                    ]
                    attempts_for_slot = (
                        select(OddsPollAttempt.match_id)
                        .join(Match, Match.id == OddsPollAttempt.match_id)
                        .where(Match.league.in_(sport_leagues), OddsPollAttempt.tier == tier)
                    )
                    if tier == "T-3" and t3_slot_to_day:
                        attempts_for_slot = attempts_for_slot.where(
                            OddsPollAttempt.slot.between(start_of_day, end_of_day),
                        )
                    else:
                        attempts_for_slot = attempts_for_slot.where(OddsPollAttempt.slot == ko_utc)
                    # One row per match per completed polling cycle. Count errors for a
                    # single match, so a shared request never looks like multiple retries.
                    completed = session.scalar(
                        attempts_for_slot.group_by(OddsPollAttempt.match_id)
                        .having(
                            (func.count(case((OddsPollAttempt.status.in_(("ok", "empty")), 1))) > 0)
                            | (func.count(OddsPollAttempt.id) >= 2)
                        )
                        .limit(1)
                    )
                    if completed is not None:
                        continue

                    markets = (
                        ["totals"] if (tier == "T-3" and disable_t3_h2h) else ["totals", "h2h"]
                    )
                    slot_key = ko_utc.date() if (tier == "T-3" and t3_slot_to_day) else ko_utc
                    qualifying_polls.append({
                        "match_id": match.id,
                        "slot": ko_utc,
                        "sport_key": sport_key,
                        "tier": tier,
                        "slot_key": slot_key,
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

        # Each group shares one request and its result across all qualifying matches.
        groups: dict[tuple[str, str, dt.date | dt.datetime], list[dict[str, Any]]] = {}
        for poll in qualifying_polls:
            group_key = (poll["sport_key"], poll["tier"], poll["slot_key"])
            groups.setdefault(group_key, []).append(poll)
        total_snapshots_taken = 0

        api_client = client
        if api_client is None:
            key = Secrets().odds_api_key
            assert key is not None
            api_client = OddsApiClient(db_url=cfg.database.url, config=cfg, api_key=key)

        for (sport_key, tier, _), group in groups.items():
            status = "error"
            # Keep the existing immediate retry. Persist one outcome for the tick
            # so a failed group can retry on one later eligible tick.
            for attempt in (1, 2):
                try:
                    report = api_client.snapshot_sport_odds(
                        sport_key=sport_key, tier=tier, markets=group[0]["markets"],
                    )
                    rows = report.get("rows", 0)
                    total_snapshots_taken += rows
                    status = "ok" if rows else "empty"
                    break
                except Exception as exc:
                    logger.warning(
                        "Snapshot polling failed for %s (%s, attempt %s/2): %s",
                        sport_key,
                        tier,
                        attempt,
                        exc,
                    )
                    if attempt == 1:
                        time.sleep(5)

            session.add_all(
                OddsPollAttempt(
                    match_id=poll["match_id"], slot=poll["slot"], tier=tier, ts=curr_now,
                    status=status,
                )
                for poll in group
            )
            session.commit()

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
        key = Secrets().odds_api_key
        if not key:
            return {"status": "skipped", "reason": "no_api_key"}
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
        rechecked_sports: set[str] = set()
        for m in suspected_t4:
            if m.commence_time_utc:
                diff_h = (m.commence_time_utc - curr_now).total_seconds() / 3600.0
                if 0.0 <= diff_h <= 4.0 and m.league:
                    l_cfg = next((lg for lg in cfg.leagues if lg.slug == m.league), None)
                    s_key = getattr(l_cfg, "toa_sport_key", None)
                    if s_key and s_key not in rechecked_sports:
                        rechecked_sports.add(s_key)
                        logger.info(
                            "Rechecking T-4 postponed match %s (%s, %s)",
                            m.id,
                            m.league,
                            s_key,
                        )
                        try:
                            fresh_events = api_client.fetch_events(s_key)
                            if fresh_events:
                                rep = api_client.apply_events(
                                    sport_key=s_key,
                                    events=fresh_events,
                                    league_slug=m.league,
                                    season=current_season,
                                    now=curr_now,
                                )
                                events_seen += rep.events_seen
                                postponed_count += rep.postponed_suspected
                                kickoffs_updated += rep.kickoffs_updated
                        except Exception as exc:
                            logger.warning("Failed T-4 recheck for %s: %s", s_key, exc)

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
    """Prefilter weekly candidates for user analysis (max 10 matches/week per SPEC §5)."""
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
            ).all()
        )

        candidate_items: list[CandidateMatch] = []
        for m in upcoming:
            h_name = m.home_team.name if m.home_team else f"Team_{m.home_team_id}"
            a_name = m.away_team.name if m.away_team else f"Team_{m.away_team_id}"
            m_name = f"{h_name} - {a_name}"
            ko = (
                m.commence_time_utc
                if m.commence_time_utc
                else dt.datetime.combine(m.date_utc, dt.time(15, 0), tzinfo=dt.UTC)
            )

            try:
                inputs = load_market_inputs_for_match(session, m.id)
                fair_mat = build_fair_matrix(inputs, home_team=h_name, away_team=a_name, config=cfg)
            except Exception:
                fair_mat = build_fair_matrix(
                    MarketInputs(source="synthetic", match_name=m_name),
                    home_team=h_name,
                    away_team=a_name,
                    config=cfg,
                )

            candidate_items.append(
                CandidateMatch(
                    match_id=m.id,
                    match_name=m_name,
                    league=m.league,
                    kickoff=ko,
                    fair_matrix=fair_mat,
                    has_fortuna_grid=True,
                )
            )

        report = filter_candidates(candidate_items, config=cfg, weekly_cap=weekly_cap)
        candidate_ids = [item.match_id for item in report.items if item.accepted]

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
    cfg = config or load_config()
    logger.info("Executing job_search_news_windows (dry_run=%s)", dry_run)

    if dry_run:
        return {"status": "dry_run", "searches_performed": 0}

    searches_done = 0
    with session_factory() as session:
        prefilter_res = job_prefilter_candidates(session_factory, config=cfg, dry_run=False)
        candidate_ids = set(prefilter_res.get("candidate_match_ids", []))
        if not candidate_ids:
            return {"status": "success", "searches_performed": 0}

        client = search_client
        if client is None:
            key = Secrets().tavily_api_key
            if not key:
                return {"status": "skipped", "reason": "no_api_key"}
            client = TavilySearchClient(db_session=session, api_key=key)

        now = dt.datetime.now(dt.UTC)

        candidates = list(
            session.scalars(
                select(Match).where(
                    Match.id.in_(candidate_ids),
                    Match.status == "scheduled",
                )
            ).all()
        )

        llm_provider = GeminiProvider(db_session_factory=session_factory)

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
                    try:
                        extraction = extract_player_news(
                            llm_provider=llm_provider,
                            home_team=h_name,
                            away_team=a_name,
                            search_results=results,
                        )
                        for it in extraction.items:
                            t_id = (
                                match.home_team_id
                                if it.team.lower() in h_name.lower()
                                else match.away_team_id
                            )
                            session.add(
                                PlayerAvailability(
                                    team_id=t_id,
                                    match_id=match.id,
                                    player=it.player,
                                    status=it.status,
                                    source="news_llm",
                                    source_url=it.source_url,
                                    quote_summary=it.quote_summary,
                                    detected_at_utc=it.published_at or now,
                                )
                            )
                        session.commit()
                    except Exception as exc:
                        logger.warning("Extraction / saving PlayerAvailability failed: %s", exc)

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
                match = session.get(Match, bet.match_id)
                h_name = match.home_team.name if match and match.home_team else ""
                a_name = match.away_team.name if match and match.away_team else ""
                if not dry_run:
                    settle_user_bet(
                        bet=bet,
                        home_goals=result.home_goals,
                        away_goals=result.away_goals,
                        home_name=h_name,
                        away_name=a_name,
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
