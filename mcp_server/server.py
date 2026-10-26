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
from core.http import register_runtime_secrets
from core.news.extractor import NewsItem
from core.news.freshness import get_freshness_warnings
from core.orm import ClosingOdds, Match, OddsSnapshot, PlayerAvailability, PrematchOdds, UserBet
from core.tracking.stats import compute_performance_metrics
from core.value.engine import evaluate_fortuna_prices
from core.value.fair_matrix import build_fair_matrix
from core.value.fortuna_input import calculate_section_overrounds, parse_fortuna_text
from core.value.persistence import load_market_inputs_for_match

register_runtime_secrets()
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

        inputs = load_market_inputs_for_match(session, match.id)
        if inputs is None or inputs.is_empty():
            return f"Для матча ID {match_id} ({h} — {a}) нет котировок острых букмекеров."

        fair_matrix = build_fair_matrix(inputs, home_team=h, away_team=a)

        # 1X2
        p_1 = fair_matrix.p_home_devig
        p_x = fair_matrix.p_draw_devig
        p_2 = fair_matrix.p_away_devig
        fo_1 = f"{1.0 / p_1:.2f}" if p_1 > 0.001 else "—"
        fo_x = f"{1.0 / p_x:.2f}" if p_x > 0.001 else "—"
        fo_2 = f"{1.0 / p_2:.2f}" if p_2 > 0.001 else "—"

        # Totals 2.5
        p_o25 = fair_matrix.total_over(2.5)
        p_u25 = fair_matrix.total_under(2.5)
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

        inputs = load_market_inputs_for_match(session, match.id)
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
        eval_result = evaluate_fortuna_prices(
            fair_matrix=fair_matrix,
            fortuna_prices=parsed_prices,
            match_name=f"{h} — {a}",
            home_team=h,
            away_team=a,
        )

        above_fair = []
        for out in eval_result.outcomes:
            if out.price_above_fair:
                edge_pct = out.edge * 100.0
                above_fair.append(
                    f"• {out.market} '{out.selection_display}': "
                    f"коэффициент {out.odds_fortuna:.2f} vs справедливый {out.fair_odds:.2f} "
                    f"(EV: +{edge_pct:.1f}%)"
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
            f"\nСправочная оценка (оговорка: kappa_high={cfg.fair_matrix.kappa_high:.2f}). "
            "Без рекомендаций и Келли. Ставки сопряжены с риском."
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
    line: float | None = None,
) -> str:
    """Record a user bet for post-match settlement and pre-kickoff CLV tracking."""
    cfg = load_config()
    with session_scope(cfg.database.url) as session:
        match = session.get(Match, match_id)
        if match is None:
            return f"Матч с ID {match_id} не найден в базе данных."

        m_clean = market.strip().lower()
        sel_clean = outcome.strip()
        allowed_markets = {
            "exact_score",
            "multiscore",
            "goals_range",
            "totals",
            "btts",
            "h2h",
            "team_totals",
        }
        if m_clean not in allowed_markets:
            return (
                f"Неподдерживаемый рынок '{market}'. Допустимые: "
                f"{', '.join(sorted(allowed_markets))}."
            )

        now = dt.datetime.now(dt.UTC)
        if match.commence_time_utc:
            ko = (
                match.commence_time_utc
                if match.commence_time_utc.tzinfo
                else match.commence_time_utc.replace(tzinfo=dt.UTC)
            )
            if now >= ko:
                return (
                    f"Ошибка: матч уже начался ({ko.strftime('%Y-%m-%d %H:%M UTC')}). "
                    f"Регистрация ставок задним числом отклонена."
                )

        h = match.home_team.name if match.home_team else f"Team_{match.home_team_id}"
        a = match.away_team.name if match.away_team else f"Team_{match.away_team_id}"

        p_fair_dec: Decimal | None = None
        try:
            from core.tracking.clv import evaluate_fair_probs_for_bet

            inputs = load_market_inputs_for_match(session, match.id, as_of=now)
            if not inputs.is_empty():
                fair_matrix = build_fair_matrix(inputs, home_team=h, away_team=a, config=cfg)
                if fair_matrix.status == "ok":
                    p_win, _ = evaluate_fair_probs_for_bet(
                        matrix=fair_matrix,
                        market=m_clean,
                        selection_key=sel_clean,
                        line=Decimal(str(line)) if line is not None else None,
                        cs_grid_max_goals=cfg.fortuna.cs_grid_max_goals,
                    )
                    if p_win > 0:
                        p_fair_dec = Decimal(str(round(p_win, 4)))
        except Exception:
            p_fair_dec = None

        bet = UserBet(
            match_id=match_id,
            market=m_clean,
            selection_key=sel_clean,
            line=Decimal(str(line)) if line is not None else None,
            odds=Decimal(str(odds)),
            stake_units=Decimal(str(stake)),
            bookmaker=bookmaker.strip(),
            p_fair_at_placement=p_fair_dec,
            status="open",
            placed_at_utc=now,
            created_at_utc=now,
        )
        session.add(bet)
        session.flush()
        session.refresh(bet)

        fair_str = f" | p_fair: {p_fair_dec:.4f}" if p_fair_dec is not None else ""
        return (
            f"Ставка успешно записана:\n"
            f"• ID ставки: {bet.id}\n"
            f"• Матч: {h} — {a} (ID {match_id})\n"
            f"• Рынок: {m_clean} | Исход: {sel_clean}\n"
            f"• Коэффициент: {odds:.2f}{fair_str} | Ставка: {stake:.2f} ед.\n"
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
            f"[{report.roi_ci_95[0] * 100.0:+.1f}% ... {report.roi_ci_95[1] * 100.0:+.1f}%]"
            if report.roi_ci_95
            else "недостаточно данных"
        )
        clv_str = f"{report.avg_clv * 100.0:+.2f}%" if report.avg_clv is not None else "—"
        clv_pos_str = (
            f"{report.positive_clv_share * 100.0:.1f}%"
            if report.positive_clv_share is not None
            else "—"
        )

        n_tot = report.total_bets
        n_set = report.settled_bets
        n_opn = report.open_bets
        lines = [
            f"Статистика ({period}):",
            f"• Ставок: {n_tot} (рассчитано: {n_set}, в игре: {n_opn})",
            f"• Оборот: {report.total_stake:.2f} ед. | P/L: {report.total_pnl:+.2f} ед.",
            f"• ROI: {report.roi * 100.0:+.2f}% (95% CI: {roi_ci_str})",
            f"• Hit Rate: {report.hit_rate * 100.0:.1f}%",
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
        if len(candidates) > 1:
            cand_lines = [
                f"• ID {m.id}: {m.home_team.name if m.home_team else ''} — "
                f"{m.away_team.name if m.away_team else ''} ({m.date_utc})"
                for m in candidates[:5]
            ]
            return (
                f"Неоднозначный запрос: найдено несколько матчей для '{home_team} — {away_team}'. "
                f"Уточните запрос или укажите точный ID матча:\n"
                + "\n".join(cand_lines)
            )

        kappa_val = cfg.fair_matrix.kappa_high
        caveat_text = (
            f"Оговорка: матрица рассчитывается с параметром сглаживания κ_high = {kappa_val:.2f} "
            "на диагонали от 2:2 для учёта редких результативных ничьих."
        )

        if len(candidates) == 1:
            match = candidates[0]
            absences = list(
                session.scalars(
                    select(PlayerAvailability).where(PlayerAvailability.match_id == match.id)
                ).all()
            )
            h_name = match.home_team.name if match.home_team else home_team
            a_name = match.away_team.name if match.away_team else away_team

            latest_snap = session.scalar(
                select(OddsSnapshot)
                .where(OddsSnapshot.match_id == match.id)
                .order_by(OddsSnapshot.captured_at_utc.desc())
                .limit(1)
            )
            snapshot_time = latest_snap.captured_at_utc if latest_snap else None
            news_items = [
                NewsItem(
                    player=a.player,
                    team=h_name if a.team_id == match.home_team_id else a_name,
                    status=a.status,
                    source_url=a.source_url or "",
                    quote_summary=a.quote_summary or "",
                    published_at=a.detected_at_utc,
                )
                for a in absences
            ]
            warnings = get_freshness_warnings(news_items, snapshot_time)
            warn_str = ("\n" + "\n".join(warnings)) if warnings else ""

            disclaimer_suffix = (
                f"{warn_str}\n{caveat_text}\n"
                "Тон строго нейтральный. Без рекомендаций по ставкам."
            )
            if absences:
                items = [f"• {a.player} ({a.status}): {a.quote_summary or ''}" for a in absences]
                return (
                    f"Сводка новостей по матчу {h_name} — {a_name}:\n"
                    + "\n".join(items)
                    + "\n"
                    + disclaimer_suffix
                )
            if warnings:
                return (
                    f"Сводка по матчу {h_name} — {a_name}:\n"
                    "Подтверждённых критических потерь или дисквалификаций не зафиксировано.\n"
                    + disclaimer_suffix
                )

        return (
            f"Сводка по матчу {home_team} — {away_team}:\n"
            "Подтверждённых критических потерь или дисквалификаций не зафиксировано. "
            "Команды готовятся к матчу в штатных составах.\n"
            f"{caveat_text}\nТон строго нейтральный. Без рекомендаций по ставкам."
        )
