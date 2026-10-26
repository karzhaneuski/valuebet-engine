"""FastAPI routes for manual Fortuna odds parsing and fair pricing evaluation."""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from api.auth import verify_bearer_token
from api.db import get_db
from api.matching import search_matches_by_query
from api.schemas import (
    FortunaEvalRequest,
    FortunaEvalResponse,
    FortunaPriceEvaluation,
)
from core.orm import Match
from core.tracking.clv import evaluate_fair_probs_for_bet, load_closing_market_inputs
from core.value.fair_matrix import build_fair_matrix
from core.value.fortuna_input import (
    calculate_section_overrounds,
    parse_fortuna_text,
)

router = APIRouter(tags=["fortuna"])


@router.post("/fortuna-eval", response_model=FortunaEvalResponse)
def evaluate_fortuna_odds(
    request: FortunaEvalRequest,
    db: Session = Depends(get_db),
    auth_token: str = Depends(verify_bearer_token),
) -> FortunaEvalResponse:
    """Parse pasted Fortuna betting lines, compute section margins, and compare with fair prices.

    Labels outcomes strictly as 'цена выше справедливой' (price above fair).
    Contains NO Kelly staking recommendations or bet advice.
    """
    match: Match | None = None
    if request.match_id is not None:
        match = db.get(Match, request.match_id)
        if match is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Матч с ID {request.match_id} не найден в базе данных",
            )
    elif request.query:
        candidates = list(search_matches_by_query(db, request.query))
        if not candidates:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Матчи по запросу '{request.query}' не найдены",
            )
        if len(candidates) > 1:
            opts = [
                f"ID {m.id}: {m.home_team.name if m.home_team else m.home_team_id} - "
                f"{m.away_team.name if m.away_team else m.away_team_id} ({m.date_utc})"
                for m in candidates[:5]
            ]
            msg = (
                f"Неоднозначный запрос: найдено {len(candidates)} матчей. "
                f"Укажите точный match_id: {'; '.join(opts)}"
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=msg,
            )
        match = candidates[0]
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Необходимо указать match_id или query с названиями команд",
        )

    h_name = match.home_team.name if match.home_team else f"Team_{match.home_team_id}"
    a_name = match.away_team.name if match.away_team else f"Team_{match.away_team_id}"
    m_name = f"{h_name} - {a_name}"

    inputs = load_closing_market_inputs(db, match)
    if inputs is None or inputs.is_empty():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Котировки для матча '{m_name}' не найдены для построения справедливой матрицы",
        )

    fair_matrix = build_fair_matrix(inputs, home_team=h_name, away_team=a_name)

    # Parse Fortuna text
    try:
        parsed_prices = parse_fortuna_text(request.raw_text)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Ошибка парсинга текста Fortuna: {exc}",
        ) from exc

    if not parsed_prices:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Не удалось распознать ни одной котировки Fortuna из переданного текста",
        )

    section_margins = calculate_section_overrounds(parsed_prices)
    evaluations: list[FortunaPriceEvaluation] = []
    prices_above_fair_count = 0

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
                sel_str = ", ".join(f"{h}:{a}" for h, a in sorted(item.selection))
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

        fair_odd = round((1.0 - p_push) / p_win, 2) if p_win > 0.0001 else 999.0
        # EV per unit staked: EV = P(win)*odds + P(push)*1 - 1
        edge = (p_win * item.odds) + (p_push * 1.0) - 1.0
        edge_pct = round(edge * 100.0, 2)
        price_above = edge > 0.0

        if price_above:
            prices_above_fair_count += 1
            note = f"Котировка выше справедливой цены (+{edge_pct:.1f}% EV)"
        else:
            note = f"Котировка ниже справедливой цены ({edge_pct:.1f}% EV)"

        evaluations.append(
            FortunaPriceEvaluation(
                market=m_type,
                selection=sel_str,
                bookmaker_odds=item.odds,
                fair_odds=fair_odd,
                fair_prob=round(p_win, 4),
                edge_pct=edge_pct,
                price_above_fair=price_above,
                section_margin=round(section_margins.get(item.market, 0.0), 4)
                if item.market in section_margins
                else None,
                status_note=note,
            )
        )

    caveats: list[str] = [
        "Маржа букмекера на точном счёте ~31% (в среднем ни одна ячейка не даёт перевеса).",
        "Отметка 'цена выше справедливой' указывает на математическое расхождение с рынком.",
        "Полное отсутствие рекомендаций к выставлению ставок и критерия Келли.",
    ]

    disclaimer = (
        "Сервис предоставляет исключительно справочно-аналитическую информацию. "
        "Ставки на спорт сопряжены с финансовым риском потери средств. "
        "Инструмент не даёт гарантий доходности."
    )

    return FortunaEvalResponse(
        match_id=match.id,
        match_name=m_name,
        section_margins={k: round(v, 4) for k, v in section_margins.items()},
        prices_above_fair_count=prices_above_fair_count,
        evaluations=evaluations,
        caveats=caveats,
        disclaimer=disclaimer,
    )
