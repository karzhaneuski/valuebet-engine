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
from core.http import register_runtime_secrets
from core.orm import Match, PrefilterDecision
from core.value.engine import (
    evaluate_fortuna_prices,
    match_key_player,
)
from core.value.fair_matrix import build_fair_matrix
from core.value.fortuna_input import parse_fortuna_text
from core.value.persistence import (
    find_matches_by_query,
    load_market_inputs_for_match,
    save_recommendations_to_db,
)
from core.value.prefilter import (
    CandidateMatch,
    PrefilterDecisionRecord,
    PrefilterReport,
    compute_high_draws_mass,
    filter_candidates,
    log_prefilter_decisions_to_db,
)
from core.value.standby_model import (
    get_key_players_for_team,
    get_model_home_share_and_model,
    has_team_lineups,
)


def _cmd_evaluate(args: argparse.Namespace, session: Session) -> int:
    """Evaluate Fortuna odds for a single match."""
    register_runtime_secrets()
    cfg = load_config()
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

    # 4. Load market inputs and dynamic standby model (if needed)
    m_inputs = load_market_inputs_for_match(session, match.id)

    model_share = args.model_home_share
    standby_model = None
    if model_share is None or m_inputs.is_empty():
        db_url = os.environ.get("DATABASE_URL") or load_config().database.url
        kickoff_time = match.commence_time_utc or dt.datetime.combine(
            match.date_utc, dt.time(15, 0), tzinfo=dt.UTC
        )
        dyn_share, dyn_model = get_model_home_share_and_model(
            db_url=db_url,
            league=match.league,
            as_of=kickoff_time,
            home_team=h_team,
            away_team=a_team,
        )
        if model_share is None:
            model_share = dyn_share
        standby_model = dyn_model

    fair_mat = build_fair_matrix(
        m_inputs,
        allow_model_fallback=True,
        model=standby_model,
        home_team=h_team,
        away_team=a_team,
    )

    # 5. Process risk flags & validate key players
    raw_abs_h = (
        [x.strip() for x in args.key_absences_home.split(",")]
        if args.key_absences_home
        else []
    )
    raw_abs_a = (
        [x.strip() for x in args.key_absences_away.split(",")]
        if args.key_absences_away
        else []
    )

    has_lineups_h = has_team_lineups(
        session, match.home_team_id, match.date_utc, cfg.risk_flags.key_player_last_n
    )
    kp_h = (
        get_key_players_for_team(
            session=session,
            team_id=match.home_team_id,
            before_date=match.date_utc,
            last_n=cfg.risk_flags.key_player_last_n,
            min_starts_share=cfg.risk_flags.key_player_min_starts_share,
        )
        if has_lineups_h
        else None
    )

    has_lineups_a = has_team_lineups(
        session, match.away_team_id, match.date_utc, cfg.risk_flags.key_player_last_n
    )
    kp_a = (
        get_key_players_for_team(
            session=session,
            team_id=match.away_team_id,
            before_date=match.date_utc,
            last_n=cfg.risk_flags.key_player_last_n,
            min_starts_share=cfg.risk_flags.key_player_min_starts_share,
        )
        if has_lineups_a
        else None
    )

    abs_h: list[str] = []
    for p in raw_abs_h:
        is_key, status_desc = match_key_player(p, kp_h, has_lineups=has_lineups_h)
        if is_key:
            abs_h.append(p)
        elif status_desc == "нет данных о составе":
            print(
                f"[ИНФО] Нет данных о составе для команды {h_team} -> игрок '{p}' "
                "не может быть подтверждён как ключевой (множитель риска не применяется)."
            )
        else:
            print(
                f"[ИНФО] Игрок '{p}' не является ключевым для {h_team} "
                f"(starts < {cfg.risk_flags.key_player_min_starts_share:.0%} в последних "
                f"{cfg.risk_flags.key_player_last_n} матчах) -> исключён из множителя риска."
            )

    abs_a: list[str] = []
    for p in raw_abs_a:
        is_key, status_desc = match_key_player(p, kp_a, has_lineups=has_lineups_a)
        if is_key:
            abs_a.append(p)
        elif status_desc == "нет данных о составе":
            print(
                f"[ИНФО] Нет данных о составе для команды {a_team} -> игрок '{p}' "
                "не может быть подтверждён как ключевой (множитель риска не применяется)."
            )
        else:
            print(
                f"[ИНФО] Игрок '{p}' не является ключевым для {a_team} "
                f"(starts < {cfg.risk_flags.key_player_min_starts_share:.0%} в последних "
                f"{cfg.risk_flags.key_player_last_n} матчах) -> исключён из множителя риска."
            )

    # 6. Evaluate prices against fair matrix
    res = evaluate_fortuna_prices(
        fair_matrix=fair_mat,
        fortuna_prices=prices,
        match_name=match_display,
        model_home_share=model_share,
        key_absences_home=abs_h,
        key_absences_away=abs_a,
        sources_conflict=args.sources_conflict,
        home_team=h_team,
        away_team=a_team,
    )

    # 7. Print Russian summary table
    print(res.format_table())

    # 8. Save recommendations to DB if requested (always executed when --save is on)
    if args.save:
        now_utc = dt.datetime.now(dt.UTC)
        is_past = False
        if match.commence_time_utc:
            is_past = (now_utc >= match.commence_time_utc) or (
                match.status in ("finished", "live", "in_play")
            )
            time_str = match.commence_time_utc.isoformat()
        else:
            is_past = (now_utc.date() > match.date_utc) or (
                match.status in ("finished", "live", "in_play")
            )
            time_str = f"{match.date_utc} (status={match.status})"

        if is_past:
            sys.stderr.write(
                f"\n[ОШИБКА] Нельзя сохранять оценки для прошедших матчей "
                f"(начало/дата: {time_str}, сейчас: {now_utc.isoformat()}).\n"
            )
            return 1

        saved = save_recommendations_to_db(session, match.id, res)
        session.commit()
        if saved > 0:
            print(
                f"\n[OK] Записано {saved} оценок с ценой выше справедливой "
                f"в таблицу 'recommendations' (матч ID {match.id})."
            )
        else:
            print(
                f"\n[ИНФО] 0 исходов с ценой выше справедливой "
                f"(ранее активные переведены в 'withdrawn') (матч ID {match.id})."
            )

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

    # Group matches by ISO week of kickoff/match date
    matches_by_week: dict[tuple[int, int], list[Match]] = {}
    for m in scheduled_matches:
        m_date = m.commence_time_utc.date() if m.commence_time_utc else m.date_utc
        iso_year, iso_week, _ = m_date.isocalendar()
        matches_by_week.setdefault((iso_year, iso_week), []).append(m)

    combined_items = []
    total_eval = 0
    total_acc = 0
    league_breakdown: dict[str, int] = {}

    for (iso_year, iso_week), week_matches in sorted(matches_by_week.items()):
        week_monday = dt.date.fromisocalendar(iso_year, iso_week, 1)
        week_sunday = week_monday + dt.timedelta(days=6)

        # Query decisions for matches in this ISO week ALREADY accepted on prior dates (< today)
        stmt_prior = (
            select(PrefilterDecision)
            .join(Match, Match.id == PrefilterDecision.match_id)
            .where(
                Match.date_utc >= week_monday,
                Match.date_utc <= week_sunday,
                PrefilterDecision.decision_date < today,
                PrefilterDecision.accepted.is_(True),
            )
            .order_by(PrefilterDecision.decision_date.desc())
        )
        prior_decisions = list(session.scalars(stmt_prior).all())
        prior_accepted_map: dict[int, PrefilterDecision] = {}
        for pd in prior_decisions:
            if pd.match_id not in prior_accepted_map:
                prior_accepted_map[pd.match_id] = pd

        accepted_this_week = len(prior_accepted_map)
        effective_weekly_cap = max(0, args.cap - accepted_this_week)
        if accepted_this_week > 0:
            print(
                f"[ИНФО] В ISO-неделю {iso_year}-W{iso_week:02d} уже отобрано {accepted_this_week} "
                f"кандидатов на прошлых запусках. Оставшийся лимит: {effective_weekly_cap} "
                f"(из {args.cap}).\n"
            )

        carried_over_records: list[PrefilterDecisionRecord] = []
        new_candidate_items: list[CandidateMatch] = []
        prior_by_league: dict[str, int] = {}

        for m in week_matches:
            h_name = m.home_team.name if m.home_team else f"Team_{m.home_team_id}"
            a_name = m.away_team.name if m.away_team else f"Team_{m.away_team_id}"
            m_name = f"{h_name} - {a_name}"
            kickoff = m.commence_time_utc or dt.datetime.combine(m.date_utc, dt.time(15, 0))

            m_inputs = load_market_inputs_for_match(session, m.id)
            fair_mat = build_fair_matrix(m_inputs)

            if m.id in prior_accepted_map:
                pd = prior_accepted_map[m.id]
                h_mass = compute_high_draws_mass(fair_mat)
                p_btts = fair_mat.btts()
                prior_by_league[m.league] = prior_by_league.get(m.league, 0) + 1
                carried_over_records.append(
                    PrefilterDecisionRecord(
                        match_id=m.id,
                        match_name=m_name,
                        league=m.league,
                        kickoff=kickoff,
                        high_draws_mass=h_mass,
                        accepted=True,
                        reason=f"Ранее отобран (перенос решения от {pd.decision_date})",
                        rank=len(carried_over_records) + 1,
                        p_fair_btts=p_btts,
                    )
                )
            else:
                new_candidate_items.append(
                    CandidateMatch(
                        match_id=m.id,
                        match_name=m_name,
                        league=m.league,
                        kickoff=kickoff,
                        fair_matrix=fair_mat,
                        has_fortuna_grid=args.has_fortuna_grid,
                    )
                )

        week_report = filter_candidates(
            new_candidate_items,
            weekly_cap=effective_weekly_cap,
            max_per_league=args.max_per_league,
        )
        combined_week_items = carried_over_records + week_report.items
        combined_items.extend(combined_week_items)
        total_eval += len(carried_over_records) + week_report.total_evaluated
        total_acc += len(carried_over_records) + week_report.total_accepted
        for lg, cnt in prior_by_league.items():
            league_breakdown[lg] = league_breakdown.get(lg, 0) + cnt
        for lg, cnt in week_report.accepted_by_league.items():
            league_breakdown[lg] = league_breakdown.get(lg, 0) + cnt

    report = PrefilterReport(
        total_evaluated=total_eval,
        total_accepted=total_acc,
        items=combined_items,
        accepted_by_league=league_breakdown,
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
        help="Сохранять ли оценки цен в базу (по умолчанию: да)",
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
        "--has-fortuna-grid",
        dest="has_fortuna_grid",
        action="store_true",
        default=None,
        help="Считать, что сетка Fortuna доступна",
    )
    cand_p.add_argument(
        "--no-fortuna-grid",
        dest="has_fortuna_grid",
        action="store_false",
        help="Считать, что сетка Fortuna отсутствует (отклонить всех кандидатов)",
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

    register_runtime_secrets()
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
