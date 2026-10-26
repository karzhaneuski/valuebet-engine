"""CLI entry point for user bet tracking, settlement, and portfolio performance analytics (SPEC §7).

Commands:
    uv run python -m core.tracking bet add --match-id <id>|--match "A - B"
        --market <m> --outcome <o> --odds <x> --stake <s> [--line <l>] [--placed-at <ts>]
    uv run python -m core.tracking bet list
    uv run python -m core.tracking bet settle
    uv run python -m core.tracking bet stats
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from core.config import load_config
from core.http import register_runtime_secrets
from core.logging import configure, get_logger
from core.orm import Match, UserBet
from core.scheduler.tasks import job_settle_matches_and_clv
from core.tracking.clv import evaluate_fair_probs_for_bet
from core.tracking.stats import compute_performance_metrics
from core.value.fair_matrix import build_fair_matrix
from core.value.persistence import load_market_inputs_for_match

configure()
register_runtime_secrets()
logger = get_logger("tracking_cli")


def find_match(
    session: Session,
    match_id: int | None = None,
    match_query: str | None = None,
) -> Match | None:
    """Find match by ID or 'Home - Away' substring query."""
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


def cmd_bet_add(args: argparse.Namespace, session_factory: sessionmaker[Session]) -> int:
    with session_factory() as session:
        match = find_match(session, match_id=args.match_id, match_query=args.match)
        if match is None:
            print(
                f"Ошибка: матч не найден в базе данных "
                f"(запрос: id={args.match_id}, match='{args.match}')",
                file=sys.stderr,
            )
            return 1

        now_utc = dt.datetime.now(dt.UTC)
        if args.placed_at:
            try:
                placed_time = dt.datetime.fromisoformat(args.placed_at)
                if placed_time.tzinfo is None:
                    placed_time = placed_time.replace(tzinfo=dt.UTC)
            except Exception as exc:
                print(f"Ошибка парсинга даты placed_at: {exc}", file=sys.stderr)
                return 1
        else:
            placed_time = now_utc

        # Rule: cannot register bet after kickoff
        if match.commence_time_utc:
            ko_utc = (
                match.commence_time_utc
                if match.commence_time_utc.tzinfo
                else match.commence_time_utc.replace(tzinfo=dt.UTC)
            )
            if placed_time >= ko_utc:
                print(
                    f"Ошибка: запрещено регистрировать ставку после начала матча "
                    f"(размещение: {placed_time}, начало матча: {ko_utc})",
                    file=sys.stderr,
                )
                return 1

        # Calculate fair probability at placement time
        p_fair: Decimal | None = None
        try:
            inputs = load_market_inputs_for_match(session, match.id, as_of=placed_time)
            if not inputs.is_empty():
                h_name = match.home_team.name if match.home_team else f"Team_{match.home_team_id}"
                a_name = match.away_team.name if match.away_team else f"Team_{match.away_team_id}"
                fm = build_fair_matrix(inputs, home_team=h_name, away_team=a_name)
                if fm.status == "ok":
                    line_dec = Decimal(str(args.line)) if args.line is not None else None
                    p_w, _ = evaluate_fair_probs_for_bet(
                        matrix=fm,
                        market=args.market,
                        selection_key=args.outcome,
                        line=line_dec,
                    )
                    if p_w > 0:
                        p_fair = Decimal(str(round(p_w, 4)))
        except Exception as exc:
            logger.warning("Could not calculate p_fair_at_placement: %s", exc)

        bet = UserBet(
            match_id=match.id,
            market=args.market,
            selection_key=args.outcome,
            line=Decimal(str(args.line)) if args.line is not None else None,
            odds=Decimal(str(args.odds)),
            stake_units=Decimal(str(args.stake)),
            bookmaker=getattr(args, "bookmaker", "fortuna") or "fortuna",
            p_fair_at_placement=p_fair,
            placed_at_utc=placed_time,
            created_at_utc=now_utc,
            status="open",
        )
        session.add(bet)
        session.commit()

        h_name = match.home_team.name if match.home_team else f"Team_{match.home_team_id}"
        a_name = match.away_team.name if match.away_team else f"Team_{match.away_team_id}"
        pf_display = f"{p_fair:.2%}" if p_fair is not None else "—"
        print(f"Ставка #{bet.id} успешно зарегистрирована:")
        print(f"• Матч: {h_name} — {a_name} (ID {match.id})")
        print(f"• Рынок: {bet.market}, Исход: {bet.selection_key}, Линия: {bet.line or '—'}")
        print(f"• Коэффициент: {bet.odds}, Ставка: {bet.stake_units} ед., p_fair: {pf_display}")
        return 0


def cmd_bet_list(session_factory: sessionmaker[Session]) -> int:
    with session_factory() as session:
        bets = list(session.scalars(select(UserBet).order_by(UserBet.id.asc())).all())
        if not bets:
            print("У пользователя пока нет зарегистрированных ставок.")
            return 0

        sep = "-" * 98
        print(sep)
        print(
            f"{'ID':<4} {'Матч':<28} {'Рынок':<12} {'Исход':<12} {'Кэф':>6} "
            f"{'Сумма':>6} {'Статус':<8} {'CLV fair':>9} {'Источник CLV'}"
        )
        print(sep)
        for b in bets:
            m = session.get(Match, b.match_id)
            if m:
                h_name = m.home_team.name if m.home_team else f"Team_{m.home_team_id}"
                a_name = m.away_team.name if m.away_team else f"Team_{m.away_team_id}"
                m_str = f"{h_name} — {a_name}"[:27]
            else:
                m_str = f"Match_{b.match_id}"

            clv_str = f"{b.clv_fair:+.2%}" if b.clv_fair is not None else "—"
            src_str = b.clv_source or "—"
            print(
                f"{b.id:<4} {m_str:<28} {b.market:<12} {b.selection_key:<12} "
                f"{b.odds:>6.2f} {b.stake_units:>6.1f} {b.status:<8} {clv_str:>9} {src_str}"
            )
        print(sep)
        print(f"Всего ставок: {len(bets)}")
        return 0


def cmd_bet_settle(session_factory: sessionmaker[Session]) -> int:
    cfg = load_config()
    print("Запуск расчёта статусов ставок и предматчевого CLV...")
    res = job_settle_matches_and_clv(session_factory, config=cfg, dry_run=False)
    s_count = res.get("settled_bets", 0)
    c_count = res.get("clv_calculated", 0)
    print(f"Результат расчёта: рассчитано ставок: {s_count}, CLV вычислено: {c_count}")
    return 0


def cmd_bet_stats(session_factory: sessionmaker[Session]) -> int:
    with session_factory() as session:
        bets = list(session.scalars(select(UserBet)).all())
        if not bets:
            print("У пользователя пока нет зарегистрированных ставок.")
            return 0

        metrics = compute_performance_metrics(bets)
        sep = "=" * 60
        print(sep)
        print("ПОРТФЕЛЬНАЯ СТАТИСТИКА СТАВОК ПОЛЬЗОВАТЕЛЯ")
        print(sep)
        print(f"Всего ставок в портфеле:       {metrics.total_bets}")
        print(f"Рассчитанных ставок:           {metrics.settled_bets}")
        won_str = f"{metrics.won_bets} / {metrics.lost_bets} / {metrics.push_bets}"
        print(f"Выиграно / Проиграно / Возврат: {won_str}")
        print(f"Доля выигрышей (Hit Rate):     {metrics.hit_rate:.1%}")
        print(f"Сумма поставленных единиц:     {metrics.total_stake:.2f}")
        print(f"Чистый PnL (единицы):          {metrics.total_pnl:+.2f}")
        print(f"Доходность (ROI):              {metrics.roi:+.2%}")
        if metrics.roi_ci_95:
            ci_str = f"[{metrics.roi_ci_95[0]:+.1%}, {metrics.roi_ci_95[1]:+.1%}]"
        else:
            ci_str = "—"
        print(f"95% Bootstrap CI по ROI:       {ci_str}")
        clv_m = metrics.avg_clv
        clv_str = f"{clv_m:+.2%}" if clv_m is not None else "—"
        print(f"Средний предматчевый CLV:      {clv_str}")
        brier = metrics.brier_score
        brier_str = f"{brier:.4f}" if brier is not None else "—"
        print(f"Оценка Brier Score:            {brier_str}")
        print(sep)
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="ValueBet Engine User Bet Tracking CLI")
    subparsers = parser.add_subparsers(dest="entity", required=True)

    bet_parser = subparsers.add_parser("bet", help="Manage user bets")
    bet_sub = bet_parser.add_subparsers(dest="action", required=True)

    # bet add
    add_p = bet_sub.add_parser("add", help="Register a new user bet")
    m_grp = add_p.add_mutually_exclusive_group(required=True)
    m_grp.add_argument("--match-id", type=int, help="Match ID")
    m_grp.add_argument("--match", type=str, help="Match query ('Home - Away')")

    add_p.add_argument(
        "--market", type=str, required=True, help="Market (exact_score, totals, etc.)"
    )
    add_p.add_argument(
        "--outcome", type=str, required=True, help="Selection key (e.g. '1:0', 'over 2.5')"
    )
    add_p.add_argument("--odds", type=float, required=True, help="Odds placed")
    add_p.add_argument("--stake", type=float, required=True, help="Stake in units")
    add_p.add_argument("--line", type=float, default=None, help="Market line (e.g. 2.5)")
    add_p.add_argument("--placed-at", type=str, default=None, help="Placement ISO timestamp")

    # bet list
    bet_sub.add_parser("list", help="List user registered bets")

    # bet settle
    bet_sub.add_parser("settle", help="Settle finished bets and calculate pre-kickoff CLV")

    # bet stats
    bet_sub.add_parser("stats", help="Show portfolio performance statistics")

    args = parser.parse_args()
    cfg = load_config()
    engine = create_engine(cfg.database.url)
    session_factory = sessionmaker(bind=engine)

    if args.entity == "bet":
        if args.action == "add":
            sys.exit(cmd_bet_add(args, session_factory))
        elif args.action == "list":
            sys.exit(cmd_bet_list(session_factory))
        elif args.action == "settle":
            sys.exit(cmd_bet_settle(session_factory))
        elif args.action == "stats":
            sys.exit(cmd_bet_stats(session_factory))


if __name__ == "__main__":
    main()
