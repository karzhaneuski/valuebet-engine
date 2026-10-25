"""CLI for Value discovery, Fortuna pricing evaluation, and candidate prefiltering.

Usage:
    uv run python -m core.value evaluate --match "<Home> - <Away>" --input fortuna.txt
    uv run python -m core.value evaluate --match "<Home> - <Away>" < fortuna.txt
    uv run python -m core.value candidates --days 3
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.config import load_config
from core.db import get_engine
from core.orm import Match
from core.value.engine import evaluate_fortuna_prices
from core.value.fair_matrix import build_fair_matrix
from core.value.fortuna_input import parse_fortuna_text
from core.value.persistence import (
    find_matches_by_query,
    load_market_inputs_for_match,
    save_recommendations_to_db,
)
from core.value.prefilter import (
    CandidateMatch,
    filter_candidates,
    log_prefilter_decisions_to_db,
)


def _cmd_evaluate(args: argparse.Namespace, session: Session) -> int:
    """Evaluate Fortuna odds for a single match."""
    # 1. Match lookup
    match: Match | None = None
    if args.match_id:
        match = session.get(Match, args.match_id)
        if match is None:
            sys.stderr.write(f"Ошибка: Матч с ID {args.match_id} не найден в базе данных.\n")
            return 1
    elif args.match:
        m_date = dt.date.fromisoformat(args.date) if args.date else None
        matches = find_matches_by_query(session, args.match, match_date=m_date)
        if not matches:
            sys.stderr.write(
                f"Ошибка: Матч не найден по запросу '{args.match}'. "
                f"Проверьте названия команд или укажите --date YYYY-MM-DD.\n"
            )
            return 1
        if len(matches) > 1:
            sys.stderr.write("Найдено несколько подходящих матчей:\n")
            for m in matches[:10]:
                h_name = m.home_team.name if m.home_team else f"Team_{m.home_team_id}"
                a_name = m.away_team.name if m.away_team else f"Team_{m.away_team_id}"
                sys.stderr.write(
                    f"  • [ID: {m.id}] {m.date_utc} {h_name} vs {a_name} ({m.league}, {m.status})\n"
                )
            sys.stderr.write(
                "\nУточните дату (--date YYYY-MM-DD) или укажите ID матча (--match-id <ID>).\n"
            )
            return 1
        match = matches[0]
    else:
        sys.stderr.write("Ошибка: Укажите --match '<Хозяева> - <Гости>' или --match-id <ID>.\n")
        return 1

    h_team = match.home_team.name if match.home_team else f"Team_{match.home_team_id}"
    a_team = match.away_team.name if match.away_team else f"Team_{match.away_team_id}"
    match_display = f"{h_team} - {a_team}"

    # 2. Read Fortuna input text
    input_text = ""
    if args.input and args.input != "-":
        p = Path(args.input)
        if not p.exists():
            sys.stderr.write(f"Ошибка: Файл с котировками Fortuna не найден: {args.input}\n")
            return 1
        input_text = p.read_text(encoding="utf-8")
    else:
        # Read from stdin
        if sys.stdin.isatty():
            print(f"Введите котировки Fortuna для матча '{match_display}' (завершите ввод Ctrl+D):")
        input_text = sys.stdin.read()

    if not input_text.strip():
        sys.stderr.write("Ошибка: Пустой ввод котировок Fortuna.\n")
        return 1

    # 3. Parse Fortuna text
    try:
        prices = parse_fortuna_text(input_text)
    except Exception as e:
        sys.stderr.write(f"Ошибка парсинга котировок Fortuna:\n{e}\n")
        return 1

    # 4. Load market inputs and build FairMatrix
    m_inputs = load_market_inputs_for_match(session, match.id)
    fair_mat = build_fair_matrix(m_inputs)

    # 5. Process risk flags
    abs_h = [x.strip() for x in args.key_absences_home.split(",")] if args.key_absences_home else []
    abs_a = [x.strip() for x in args.key_absences_away.split(",")] if args.key_absences_away else []

    # 6. Evaluate prices against fair matrix
    res = evaluate_fortuna_prices(
        fair_matrix=fair_mat,
        fortuna_prices=prices,
        match_name=match_display,
        model_home_share=args.model_home_share,
        key_absences_home=abs_h,
        key_absences_away=abs_a,
        sources_conflict=args.sources_conflict,
    )

    # 7. Print Russian summary table
    print(res.format_table())

    # 8. Save recommendations to DB if requested
    if args.save and res.recommendations_count > 0:
        saved = save_recommendations_to_db(session, match.id, res)
        session.commit()
        print(
            f"\n[OK] Записано {saved} рекомендаций в таблицу 'recommendations' "
            f"(матч ID {match.id})."
        )
    elif args.save:
        print("\n[ИНФО] Рекомендаций выше порогов нет. Запись в базу не требуется.")

    return 0


def _cmd_candidates(args: argparse.Namespace, session: Session) -> int:
    """Run candidate prefilter for upcoming matches."""
    today = dt.date.fromisoformat(args.from_date) if args.from_date else dt.date.today()
    end_date = today + dt.timedelta(days=args.days)

    stmt = (
        select(Match)
        .where(Match.status == "scheduled")
        .where(Match.date_utc >= today)
        .where(Match.date_utc <= end_date)
        .order_by(Match.date_utc, Match.id)
    )
    scheduled_matches = list(session.scalars(stmt).all())

    if not scheduled_matches:
        # Fallback to next scheduled matches if current window is empty
        stmt_fallback = (
            select(Match)
            .where(Match.status == "scheduled")
            .order_by(Match.date_utc, Match.id)
            .limit(20)
        )
        scheduled_matches = list(session.scalars(stmt_fallback).all())
        if scheduled_matches:
            print(
                f"[ИНФО] На период {today} .. {end_date} матчей нет. "
                f"Рассматриваем ближайшие {len(scheduled_matches)} матчей "
                f"(начиная с {scheduled_matches[0].date_utc}):\n"
            )

    if not scheduled_matches:
        print("[ИНФО] В базе данных нет запланированных матчей.")
        return 0

    candidate_items: list[CandidateMatch] = []
    for m in scheduled_matches:
        h_name = m.home_team.name if m.home_team else f"Team_{m.home_team_id}"
        a_name = m.away_team.name if m.away_team else f"Team_{m.away_team_id}"
        m_name = f"{h_name} - {a_name}"
        kickoff = m.commence_time_utc or dt.datetime.combine(m.date_utc, dt.time(15, 0))

        m_inputs = load_market_inputs_for_match(session, m.id)
        fair_mat = build_fair_matrix(m_inputs)

        candidate_items.append(
            CandidateMatch(
                match_id=m.id,
                match_name=m_name,
                league=m.league,
                kickoff=kickoff,
                fair_matrix=fair_mat,
            )
        )

    report = filter_candidates(
        candidate_items,
        weekly_cap=args.cap,
        max_per_league=args.max_per_league,
    )

    print(report.format_table())

    if args.save:
        saved = log_prefilter_decisions_to_db(session, report, decision_date=today)
        session.commit()
        print(f"\n[OK] Зафиксировано {saved} решений префильтра в 'prefilter_decisions'.")

    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="python -m core.value",
        description="ValueBet Engine: Справедливая рыночная матрица и отбор value-ставок.",
    )
    subparsers = parser.add_subparsers(dest="command")

    # Command: evaluate
    eval_p = subparsers.add_parser(
        "evaluate",
        help="Оценка котировок Fortuna для матча против справедливой матрицы",
    )
    eval_p.add_argument(
        "--match",
        type=str,
        help="Название матча (например, 'Arsenal - Everton')",
    )
    eval_p.add_argument(
        "--match-id",
        type=int,
        help="ID матча в базе данных",
    )
    eval_p.add_argument(
        "--date",
        type=str,
        help="Дата матча в формате YYYY-MM-DD",
    )
    eval_p.add_argument(
        "--input",
        "-i",
        type=str,
        help="Путь к файлу с ручными котировками Fortuna (или '-' для stdin)",
    )
    eval_p.add_argument(
        "--key-absences-home",
        type=str,
        help="Ключевые отсутствующие хозяев (через запятую)",
    )
    eval_p.add_argument(
        "--key-absences-away",
        type=str,
        help="Ключевые отсутствующие гостей (через запятую)",
    )
    eval_p.add_argument(
        "--sources-conflict",
        action="store_true",
        help="Флаг противоречия источников по составам",
    )
    eval_p.add_argument(
        "--model-home-share",
        type=float,
        help="Доля голов хозяев по модели Dixon-Coles (для детекции аномалий)",
    )
    eval_p.add_argument(
        "--save",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Сохранять ли рекомендации в базу (по умолчанию: да)",
    )

    # Command: candidates
    cand_p = subparsers.add_parser(
        "candidates",
        help="Префильтр кандидатов на ближайшие матчи для ручного ввода котировок Fortuna",
    )
    cand_p.add_argument(
        "--days",
        type=int,
        default=3,
        help="Горизонт отбора матчей в днях (по умолчанию 3)",
    )
    cand_p.add_argument(
        "--from-date",
        type=str,
        help="Начальная дата в формате YYYY-MM-DD (по умолчанию: сегодня)",
    )
    cand_p.add_argument(
        "--cap",
        type=int,
        default=10,
        help="Недельный лимит кандидатов (по умолчанию 10)",
    )
    cand_p.add_argument(
        "--max-per-league",
        type=int,
        default=3,
        help="Максимум матчей из одной лиги для ротации (по умолчанию 3)",
    )
    cand_p.add_argument(
        "--save",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Сохранять ли решения в базу prefilter_decisions (по умолчанию: да)",
    )

    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    cfg = load_config()
    db_url = os.environ.get("DATABASE_URL") or cfg.database.url
    engine = get_engine(db_url)

    with Session(engine) as session:
        if args.command == "evaluate":
            return _cmd_evaluate(args, session)
        if args.command == "candidates":
            return _cmd_candidates(args, session)

    return 0


if __name__ == "__main__":
    sys.exit(main())
