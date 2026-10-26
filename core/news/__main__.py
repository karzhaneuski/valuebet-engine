"""CLI entry point for news searching, extraction, and briefing (SPEC §6, decisions S5).

Commands:
    uv run python -m core.news brief --match-id <id> | --match "A - B"
        --window T-24h|T-3h [--dry-run]
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from core.config import load_config
from core.http import register_runtime_secrets
from core.llm.provider import GeminiProvider
from core.logging import configure, get_logger
from core.news.brief import generate_match_brief
from core.news.extractor import extract_player_news
from core.news.freshness import get_freshness_warnings
from core.news.search import TavilySearchClient, search_news_for_match
from core.orm import Match, OddsSnapshot, PlayerAvailability
from core.scheduler.tasks import job_prefilter_candidates

configure()
register_runtime_secrets()
logger = get_logger("news_cli")


def find_match(
    session: Session,
    match_id: int | None = None,
    match_query: str | None = None,
) -> Match | None:
    """Find scheduled or upcoming match by ID or 'Home - Away' team query."""
    if match_id is not None:
        return session.get(Match, match_id)

    if match_query:
        parts = [p.strip() for p in match_query.replace("—", "-").split("-")]
        matches = list(session.scalars(select(Match)).all())
        for m in matches:
            h_name = m.home_team.name if m.home_team else f"Team_{m.home_team_id}"
            a_name = m.away_team.name if m.away_team else f"Team_{m.away_team_id}"
            if len(parts) >= 2:
                if parts[0].lower() in h_name.lower() and parts[1].lower() in a_name.lower():
                    return m
            elif len(parts) == 1 and (
                parts[0].lower() in h_name.lower() or parts[0].lower() in a_name.lower()
            ):
                return m
    return None


def run_brief_cli(args: argparse.Namespace) -> int:
    cfg = load_config()
    engine = create_engine(cfg.database.url)
    session_factory = sessionmaker(bind=engine)

    with session_factory() as session:
        match = find_match(session, match_id=args.match_id, match_query=args.match)
        if match is None:
            print(
                f"Ошибка: матч не найден в базе данных "
                f"(запрос: id={args.match_id}, match='{args.match}')",
                file=sys.stderr,
            )
            return 1

        h_name = match.home_team.name if match.home_team else f"Team_{match.home_team_id}"
        a_name = match.away_team.name if match.away_team else f"Team_{match.away_team_id}"

        # Check weekly candidate status (quota defense)
        prefilter_res = job_prefilter_candidates(session_factory, cfg, dry_run=True)
        candidate_ids = set(prefilter_res.get("candidate_match_ids", []))
        if candidate_ids and match.id not in candidate_ids:
            print(
                f"Матч '{h_name} — {a_name}' (ID {match.id}) не входит в число кандидатов "
                f"текущей недели. Поиск новостей пропущен (экономия квоты)."
            )
            return 0

        window = args.window
        if args.dry_run:
            print(f"=== DRY-RUN: Новостной бриф {h_name} — {a_name} (ID {match.id}) ===")
            print(f"Окно поиска: {window}")
            print(f"Поисковый провайдер: Tavily, Модель: {cfg.llm.reasoning_model}")
            print("Статус кандидата: подтверждён")
            return 0

        # Real news pipeline execution
        print(f"Поиск новостей и аналитика составов: {h_name} — {a_name} ({window})...")
        search_client = TavilySearchClient(db_session=session)
        search_results = search_news_for_match(
            session=session,
            search_client=search_client,
            match_id=match.id,
            home_team=h_name,
            away_team=a_name,
            window=window,
        )

        llm = GeminiProvider(db_session_factory=session_factory)
        extraction = extract_player_news(
            llm_provider=llm,
            home_team=h_name,
            away_team=a_name,
            search_results=search_results,
        )

        # Save extracted items into PlayerAvailability
        now_utc = dt.datetime.now(dt.UTC)
        for item in extraction.items:
            t_id = match.home_team_id if item.team.lower() in h_name.lower() else match.away_team_id
            session.add(
                PlayerAvailability(
                    team_id=t_id,
                    match_id=match.id,
                    player=item.player,
                    status=item.status,
                    source="news_llm",
                    source_url=item.source_url,
                    quote_summary=item.quote_summary,
                    detected_at_utc=item.published_at or now_utc,
                )
            )
        session.commit()

        # Check market snapshot freshness
        latest_snap = session.scalar(
            select(OddsSnapshot)
            .where(OddsSnapshot.match_id == match.id)
            .order_by(OddsSnapshot.captured_at_utc.desc())
            .limit(1)
        )
        snap_time = latest_snap.captured_at_utc if latest_snap else None
        freshness_warns = get_freshness_warnings(extraction.items, snap_time)

        brief = generate_match_brief(
            llm_provider=llm,
            home_team=h_name,
            away_team=a_name,
            news_items=extraction.items,
        )

        print("\n" + "=" * 70)
        print(f"НОВОСТНОЙ БРИФ: {h_name} — {a_name}")
        print("=" * 70)
        if freshness_warns:
            for w in freshness_warns:
                print(f"[ВНИМАНИЕ] {w}")
            print("-" * 70)
        print(brief)
        print("=" * 70)
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="ValueBet Engine News CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    brief_parser = subparsers.add_parser("brief", help="Generate match squad news brief")
    group = brief_parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--match-id", type=int, help="Match ID in database")
    group.add_argument("--match", type=str, help="Team query (e.g. 'Arsenal - Chelsea')")

    brief_parser.add_argument(
        "--window",
        type=str,
        default="T-24h",
        choices=["T-24h", "T-3h"],
        help="Target pre-match news window (default: T-24h)",
    )
    brief_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate news pipeline without calling external APIs",
    )

    args = parser.parse_args()
    if args.command == "brief":
        sys.exit(run_brief_cli(args))


if __name__ == "__main__":
    main()
