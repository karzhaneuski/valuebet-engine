"""FastAPI routes for manual Fortuna odds parsing and fair pricing evaluation."""

from __future__ import annotations

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
from core.value.engine import evaluate_fortuna_prices
from core.value.fair_matrix import build_fair_matrix
from core.value.fortuna_input import (
    calculate_section_overrounds,
    parse_fortuna_text,
)
from core.value.persistence import load_market_inputs_for_match

router = APIRouter(tags=["fortuna"])


@router.post("/fortuna-eval", response_model=FortunaEvalResponse)
@router.post("/fortuna/evaluate", response_model=FortunaEvalResponse)
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

    inputs = load_market_inputs_for_match(db, match.id)
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

    eval_result = evaluate_fortuna_prices(
        fair_matrix=fair_matrix,
        fortuna_prices=parsed_prices,
        match_name=m_name,
        home_team=h_name,
        away_team=a_name,
    )

    evaluations: list[FortunaPriceEvaluation] = []
    prices_above_fair_count = 0

    for out in eval_result.outcomes:
        edge_pct = round(out.edge * 100.0, 2)
        if out.price_above_fair:
            prices_above_fair_count += 1
            note = f"Котировка выше справедливой цены (+{edge_pct:.1f}% EV)"
        else:
            note = out.rejection_reason or f"Котировка ниже справедливой цены ({edge_pct:.1f}% EV)"

        evaluations.append(
            FortunaPriceEvaluation(
                market=out.market,
                selection=out.selection_display,
                bookmaker_odds=out.odds_fortuna,
                fair_odds=round(out.fair_odds, 2),
                fair_prob=round(out.p_fair, 4),
                edge_pct=edge_pct,
                price_above_fair=out.price_above_fair,
                section_margin=round(section_margins.get(out.market, 0.0), 4)
                if out.market in section_margins
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
