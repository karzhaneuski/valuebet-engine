"""Match news briefing generation in Russian using reasoning model (SPEC §6, decisions S5).

Generates a concise 2-3 paragraph analytical summary of confirmed news, injuries,
and squad availability for the user.
STRICTLY PROHIBITS:
- Recommending bets or suggesting 'value'
- Inventing win/draw/loss probabilities
- Manipulating or suggesting fair market prices
"""

from __future__ import annotations

import logging

from core.llm.provider import LLMError, LLMProvider
from core.news.extractor import NewsItem

logger = logging.getLogger(__name__)

BRIEF_PROMPT_TEMPLATE = """Вы — спортивный футбольный аналитик. Ваша задача — подготовить краткую
информационную сводку (2–3 абзаца на русском языке) по матчу {home_team} — {away_team}
на основе проверенных новостей о составах команд.

ФАКТИЧЕСКИЕ ДАННЫЕ О СОСТАВАХ:
{items_text}

КАТЕГОРИЧЕСКИЕ ТРЕБОВАНИЯ И ЗАПРЕТЫ:
1. ЗАПРЕЩЕНО давать любые рекомендации по ставкам ("стоит поставить", "хороший выбор").
2. ЗАПРЕЩЕНО выдумывать, называть или оценивать вероятности победы или забитых голов.
3. ЗАПРЕЩЕНО обсуждать коэффициенты букмекеров или предлагать справедливые цены.
4. Тон — строго нейтральный, фактологический, журналистский.
5. Осветите ключевые потери, статус сомнительных игроков и общее влияние на глубину состава.
"""


def generate_match_brief(
    llm_provider: LLMProvider,
    home_team: str,
    away_team: str,
    news_items: list[NewsItem],
    model: str | None = None,
) -> str:
    """Generate a 2-3 paragraph Russian factual brief based on extracted news items.

    Returns safe objective text without betting advice or invented probabilities.
    """
    if not news_items:
        return (
            f"По матчу {home_team} — {away_team} подтверждённых данных о критических потерях "
            "или дисквалификациях ключевых игроков на данный момент не обнаружено. "
            "Команды готовятся к матчу в штатных составах."
        )

    formatted_items = []
    for item in news_items:
        formatted_items.append(
            f"- Команда: {item.team}, Игрок: {item.player}, Статус: {item.status}. "
            f"Детали: {item.quote_summary}"
        )
    items_text = "\n".join(formatted_items)

    prompt = BRIEF_PROMPT_TEMPLATE.format(
        home_team=home_team,
        away_team=away_team,
        items_text=items_text,
    )

    try:
        brief = llm_provider.generate_text(
            prompt=prompt,
            model=model,
            purpose="user_brief",
        )
        return brief.strip()
    except LLMError as exc:
        logger.warning(
            "Failed to generate news brief for %s vs %s: %s",
            home_team,
            away_team,
            exc,
        )
        return (
            f"Краткая сводка по потерям {home_team} — {away_team}:\n"
            + "\n".join(f"• {item.player} ({item.team}): {item.status}" for item in news_items)
        )
