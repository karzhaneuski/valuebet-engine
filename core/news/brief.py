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
import re

from core.llm.provider import LLMError, LLMProvider
from core.news.extractor import NewsItem

logger = logging.getLogger(__name__)

FORBIDDEN_BRIEF_PATTERNS = [
    re.compile(r"ставк[а-я]*", re.IGNORECASE),
    re.compile(r"рекоменд[а-я]*", re.IGNORECASE),
    re.compile(r"\b\d+%\b"),
    re.compile(r"\b0\.\d+\b"),
]


def verify_and_sanitize_brief(
    brief: str,
    home_team: str,
    away_team: str,
    news_items: list[NewsItem],
) -> str:
    """Post-verification: check for forbidden betting words and probabilities."""
    for pat in FORBIDDEN_BRIEF_PATTERNS:
        match = pat.search(brief)
        if match:
            logger.warning(
                "Forbidden betting content detected in LLM brief ('%s'). "
                "Replacing with safe neutral factual template.",
                match.group(0),
            )
            neutral_lines = [
                f"Фактическая сводка по составам: {home_team} — {away_team}:",
            ]
            for item in news_items:
                neutral_lines.append(f"• {item.player} ({item.team}): статус «{item.status}».")
            return "\n".join(neutral_lines)
    return brief

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
        return f"По матчу {home_team} — {away_team} подтверждённых данных о составах нет."

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
        return verify_and_sanitize_brief(
            brief=brief.strip(),
            home_team=home_team,
            away_team=away_team,
            news_items=news_items,
        )
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
