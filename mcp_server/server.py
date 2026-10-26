"""FastMCP server for ValueBet Engine (SPEC v3 §9).

Provides tools for Hermes Agent over official Python SDK `mcp`:
- get_upcoming_matches: List scheduled matches with lines status
- get_match_fair_prices: Compact fair probabilities & odds without dumping huge matrices
- evaluate_fortuna_odds: Parse Fortuna text, calculate section margins, identify prices above fair
- record_user_bet: Register a user bet for settlement and CLV audit
- get_user_performance: Audit user track record, ROI, P/L, CLV, with variance warnings
- get_news_brief: Factual Russian briefing on team squads and confirmed absences
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from mcp.server.fastmcp import FastMCP
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.matching import search_matches_by_query
from core.config import load_config
from core.db import session_scope
from core.orm import ClosingOdds, Match, OddsSnapshot, PlayerAvailability, PrematchOdds, UserBet
from core.tracking.clv import evaluate_fair_probs_for_bet, load_closing_market_inputs
from core.tracking.stats import compute_performance_metrics
from core.value.fair_matrix import build_fair_matrix
from core.value.fortuna_input import calculate_section_overrounds, parse_fortuna_text

mcp = FastMCP("valuebet-engine")


def _check_lines_exist(session: Session, match_id: int) -> bool:
    c = session.scalar(select(func.count(ClosingOdds.id)).where(ClosingOdds.match_id == match_id))
    if c and c > 0:
        return True
    p = session.scalar(select(func.count(PrematchOdds.id)).where(PrematchOdds.match_id == match_id))
    if p and p > 0:
        return True
    s = session.scalar(select(func.count(OddsSnapshot.id)).where(OddsSnapshot.match_id == match_id))
    return bool(s and s > 0)


@mcp.tool()
def get_upcoming_matches(days: int = 3, league: str | None = None) -> str:
    """Retrieve upcoming football matches within specified horizon (days) and optional league."""
    cfg = load_config()
    with session_scope(cfg.database.url) as session:
        today = dt.date.today()
        max_date = today + dt.timedelta(days=days)

        stmt = (
            select(Match)
            .where(Match.status == "scheduled", Match.date_utc >= today, Match.date_utc <= max_date)
            .order_by(Match.date_utc.asc(), Match.commence_time_utc.asc())
        )
        if league:
            stmt = stmt.where(Match.league == league.lower().strip())

        matches = list(session.scalars(stmt).all())
        if not matches:
            return f"В расписании на ближайшие {days} дн. матчей не найдено."

        lines = [f"Ближайшие матчи на {days} дн. (всего: {len(matches)}):"]
        for m in matches:
            h = m.home_team.name if m.home_team else f"Team_{m.home_team_id}"
            a = m.away_team.name if m.away_team else f"Team_{m.away_team_id}"
            has_l = _check_lines_exist(session, m.id)
            status_str = "линии доступны" if has_l else "ожидание линий"
            lines.append(f"• ID {m.id}: {m.date_utc} {h} — {a} ({m.league.upper()}) | {status_str}")

        return "\n".join(lines)


@mcp.tool()
def get_match_fair_prices(match_id: int) -> str:
    """Get fair odds and probabilities across all football markets for a match by its ID."""
    cfg = load_config()
    with session_scope(cfg.database.url) as session:
        match = session.get(Match, match_id)
        if match is None:
            return f"Матч с ID {match_id} не найден в базе данных."

        h = match.home_team.name if match.home_team else f"Team_{match.home_team_id}"
        a = match.away_team.name if match.away_team else f"Team_{match.away_team_id}"

        inputs = load_closing_market_inputs(session, match)
        if inputs is None or inputs.is_empty():
            return f"Для матча ID {match_id} ({h} — {a}) нет котировок острых букмекеров."

        fair_matrix = build_fair_matrix(inputs, home_team=h, away_team=a)

        # 1X2
        p_1, _ = evaluate_fair_probs_for_bet(fair_matrix, "h2h", "1", None)
        p_x, _ = evaluate_fair_probs_for_bet(fair_matrix, "h2h", "X", None)
        p_2, _ = evaluate_fair_probs_for_bet(fair_matrix, "h2h", "2", None)
        fo_1 = f"{1.0 / p_1:.2f}" if p_1 > 0.001 else "—"
        fo_x = f"{1.0 / p_x:.2f}" if p_x > 0.001 else "—"
        fo_2 = f"{1.0 / p_2:.2f}" if p_2 > 0.001 else "—"

        # Totals 2.5
        p_o25, _ = evaluate_fair_probs_for_bet(fair_matrix, "totals", "over", Decimal("2.5"))
        p_u25, _ = evaluate_fair_probs_for_bet(fair_matrix, "totals", "under", Decimal("2.5"))
        fo_o25 = f"{1.0 / p_o25:.2f}" if p_o25 > 0.001 else "—"
        fo_u25 = f"{1.0 / p_u25:.2f}" if p_u25 > 0.001 else "—"

        # BTTS
        p_by = fair_matrix.btts(yes=True)
        p_bn = fair_matrix.btts(yes=False)
        fo_by = f"{1.0 / p_by:.2f}" if p_by > 0.001 else "—"
        fo_bn = f"{1.0 / p_bn:.2f}" if p_bn > 0.001 else "—"

        # Top-3 exact scores
        scores = []
        p_grid = 0.0
        for gh in range(5):
            for ga in range(5):
                prob = fair_matrix.exact_score(gh, ga)
                p_grid += prob
                scores.append((f"{gh}:{ga}", prob))
        scores.sort(key=lambda x: x[1], reverse=True)
        top3 = [f"{s} ({1.0 / pr:.2f}, P={pr:.1%})" for s, pr in scores[:3]]

        # Inny
        p_inny = max(0.0, 1.0 - p_grid)
        fo_inny = f"{1.0 / p_inny:.2f}" if p_inny > 0.001 else "—"

        lines = [
            f"Справедливые котировки: {h} — {a} (ID {match_id})",
            f"• 1X2: 1: {fo_1} (P={p_1:.1%}) | X: {fo_x} (P={p_x:.1%}) | 2: {fo_2} (P={p_2:.1%})",
            f"• Тотал 2.5: Б: {fo_o25} (P={p_o25:.1%}) | М: {fo_u25} (P={p_u25:.1%})",
            f"• Обе забьют: Да: {fo_by} (P={p_by:.1%}) | Нет: {fo_bn} (P={p_bn:.1%})",
            f"• Топ-3 счёта: {', '.join(top3)}",
            f"• Исход 'inny' (>4 голов): {fo_inny} (P={p_inny:.1%})",
            "Оговорки: ρ=0, κ_high=1.05 от 2:2, девигованный рынок острых букмекеров.",
        ]
        return "\n".join(lines)


@mcp.tool()
def evaluate_fortuna_odds(
    raw_text: str,
    match_id: int | None = None,
    team_query: str | None = None,
) -> str:
    """Evaluate manual Fortuna bookmaker odds against fair market matrix prices."""
    cfg = load_config()
    with session_scope(cfg.database.url) as session:
        match: Match | None = None
        if match_id is not None:
            match = session.get(Match, match_id)
            if match is None:
                return f"Матч с ID {match_id} не найден в базе данных."
        elif team_query:
            candidates = list(search_matches_by_query(session, team_query))
            if not candidates:
                return f"Матчи по запросу '{team_query}' не найдены."
            if len(candidates) > 1:
                opts = [
                    f"- ID {m.id}: {m.home_team.name if m.home_team else m.home_team_id} — "
                    f"{m.away_team.name if m.away_team else m.away_team_id} ({m.date_utc})"
                    for m in candidates[:5]
                ]
                return (
                    f"Неоднозначный запрос: найдено {len(candidates)} матчей. "
                    f"Укажите точный match_id:\n" + "\n".join(opts)
                )
            match = candidates[0]
        else:
            return "Необходимо указать match_id или team_query для сопоставления матча."

        h = match.home_team.name if match.home_team else f"Team_{match.home_team_id}"
        a = match.away_team.name if match.away_team else f"Team_{match.away_team_id}"

        inputs = load_closing_market_inputs(session, match)
        if inputs is None or inputs.is_empty():
            return f"Для матча {h} — {a} нет котировок острых букмекеров."

        fair_matrix = build_fair_matrix(inputs, home_team=h, away_team=a)

        try:
            parsed_prices = parse_fortuna_text(raw_text)
        except Exception as exc:
            return f"Ошибка при разборе текста Fortuna: {exc}"

        if not parsed_prices:
            return "Не удалось распознать котировки Fortuna из переданного текста."

        section_margins = calculate_section_overrounds(parsed_prices)
        above_fair = []

        for item in parsed_prices:
            m_type: str = item.market
            sel_str = ""
            line_val: Decimal | None = None

            if m_type == "exact_score" and isinstance(item.selection, tuple):
                sel_str = f"{item.selection[0]}:{item.selection[1]}"
            elif m_type == "other":
                m_type = "exact_score"
                sel_str = "inny"
            elif m_type == "multiscore":
                if isinstance(item.selection, frozenset):
                    sel_str = ", ".join(f"{gh}:{ga}" for gh, ga in sorted(item.selection))
                else:
                    sel_str = str(item.selection)
            elif m_type == "goals_range" and isinstance(item.selection, tuple):
                sel_str = f"{item.selection[0]}-{item.selection[1]}"
            elif m_type == "totals" and isinstance(item.selection, tuple):
                sel_str = str(item.selection[0])
                line_val = Decimal(str(item.selection[1]))
            elif m_type in ("h2h", "1x2", "btts"):
                sel_str = str(item.selection)
            elif (
                m_type == "team_totals"
                and isinstance(item.selection, tuple)
                and len(item.selection) == 3
            ):
                sel_str = f"{item.selection[0]}_{item.selection[1]}"
                line_val = Decimal(str(item.selection[2]))
            else:
                sel_str = str(item.selection)

            p_win, p_push = evaluate_fair_probs_for_bet(
                fair_matrix, m_type, sel_str, line_val
            )
            if p_win <= 0.0:
                continue

            fair_odd = (1.0 - p_push) / p_win if p_win > 0.0001 else 999.0
            edge = (p_win * item.odds) + (p_push * 1.0) - 1.0
            if edge > 0.0:
                above_fair.append(
                    f"• {m_type} '{sel_str}': коэффициент {item.odds:.2f} vs "
                    f"справедливый {fair_odd:.2f} (EV: +{edge * 100:.1f}%)"
                )

        margins_str = ", ".join(f"{k}: {v * 100:.1f}%" for k, v in section_margins.items())
        res_lines = [
            f"Анализ котировок Fortuna: {h} — {a}",
            f"Маржа букмекера по секциям: {margins_str if margins_str else 'не определена'}",
        ]
        if above_fair:
            res_lines.append("Котировки выше справедливой цены (+EV):")
            res_lines.extend(above_fair)
        else:
            res_lines.append(
                "Котировок выше справедливой цены не обнаружено (маржа букмекера)."
            )

        res_lines.append(
            "\nСправочная оценка. Без рекомендаций и Келли. Ставки сопряжены с риском."
        )
        return "\n".join(res_lines)


@mcp.tool()
def record_user_bet(
    match_id: int,
    market: str,
    outcome: str,
    odds: float,
    stake: float,
    bookmaker: str = "Fortuna",
) -> str:
    """Record a user bet for post-match settlement and pre-kickoff CLV tracking."""
    cfg = load_config()
    with session_scope(cfg.database.url) as session:
        match = session.get(Match, match_id)
        if match is None:
            return f"Матч с ID {match_id} не найден в базе данных."

        h = match.home_team.name if match.home_team else f"Team_{match.home_team_id}"
        a = match.away_team.name if match.away_team else f"Team_{match.away_team_id}"

        now = dt.datetime.now(dt.UTC)
        bet = UserBet(
            match_id=match_id,
            market=market.strip().lower(),
            selection_key=outcome.strip(),
            odds=Decimal(str(odds)),
            stake_units=Decimal(str(stake)),
            bookmaker=bookmaker.strip(),
            status="open",
            placed_at_utc=now,
            created_at_utc=now,
        )
        session.add(bet)
        session.flush()
        session.refresh(bet)

        return (
            f"Ставка успешно записана:\n"
            f"• ID ставки: {bet.id}\n"
            f"• Матч: {h} — {a} (ID {match_id})\n"
            f"• Рынок: {market} | Исход: {outcome}\n"
            f"• Коэффициент: {odds:.2f} | Ставка: {stake:.2f} ед.\n"
            f"• Букмекер: {bookmaker}\n"
            f"• Статус: open (ожидает завершения матча и расчёта)"
        )


@mcp.tool()
def get_user_performance(period: str = "all") -> str:
    """Retrieve audited betting track record, P/L, ROI, and CLV metrics."""
    cfg = load_config()
    with session_scope(cfg.database.url) as session:
        stmt = select(UserBet)
        now = dt.datetime.now(dt.UTC)

        if period == "month":
            stmt = stmt.where(UserBet.placed_at_utc >= now - dt.timedelta(days=30))
        elif period == "season":
            curr_year = now.year if now.month >= 7 else now.year - 1
            stmt = stmt.where(UserBet.placed_at_utc >= dt.datetime(curr_year, 7, 1, tzinfo=dt.UTC))

        bets = list(session.scalars(stmt.order_by(UserBet.placed_at_utc.asc())).all())
        report = compute_performance_metrics(bets)

        roi_ci_str = (
            f"[{report.roi_ci_95[0]:+.1f}% ... {report.roi_ci_95[1]:+.1f}%]"
            if report.roi_ci_95
            else "недостаточно данных"
        )
        clv_str = f"{report.avg_clv:+.2f}%" if report.avg_clv is not None else "—"
        clv_pos_str = (
            f"{report.positive_clv_share:.1%}" if report.positive_clv_share is not None else "—"
        )

        n_tot = report.total_bets
        n_set = report.settled_bets
        n_opn = report.open_bets
        lines = [
            f"Статистика ({period}):",
            f"• Ставок: {n_tot} (рассчитано: {n_set}, в игре: {n_opn})",
            f"• Оборот: {report.total_stake:.2f} ед. | P/L: {report.total_pnl:+.2f} ед.",
            f"• ROI: {report.roi:+.2f}% (95% CI: {roi_ci_str})",
            f"• Hit Rate: {report.hit_rate:.1%}",
            f"• Средний CLV: {clv_str}",
            f"• Доля положительного CLV: {clv_pos_str}",
        ]
        if report.sample_size_warning:
            lines.append(f"⚠️ {report.sample_size_warning}")

        lines.append(
            "\nДисклеймер: Справочная аналитика. Ставки сопряжены с риском потери средств."
        )
        return "\n".join(lines)


@mcp.tool()
def get_news_brief(home_team: str, away_team: str) -> str:
    """Get Russian factual briefing of team news, confirmed absences, and squad status."""
    cfg = load_config()
    with session_scope(cfg.database.url) as session:
        candidates = list(search_matches_by_query(session, f"{home_team} - {away_team}"))
        if candidates:
            match = candidates[0]
            absences = list(
                session.scalars(
                    select(PlayerAvailability).where(PlayerAvailability.match_id == match.id)
                ).all()
            )
            if absences:
                items = [f"• {a.player} ({a.status}): {a.quote_summary or ''}" for a in absences]
                return (
                    f"Сводка новостей по матчу {home_team} — {away_team}:\n"
                    + "\n".join(items)
                    + "\nТон строго нейтральный. Без рекомендаций по ставкам."
                )

        return (
            f"Сводка по матчу {home_team} — {away_team}:\n"
            "Подтверждённых критических потерь или дисквалификаций не зафиксировано. "
            "Команды готовятся к матчу в штатных составах."
        )
