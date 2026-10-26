# ValueBet Engine — SPEC v3 Implementation Summary

## Реализация компонентов SPEC v3

| Пункт | Коммит | Имя теста |
|:---|:---|:---|
| BLOCKING 1: Секреты — точное восстановление `ASSIGNMENT_PATTERN` и `_ENV_NAMES` в `core/http.py`, allowlist baseline в `.github/secrets_allowlist.txt` и CI | `c9f0959` | `tests/test_http_secrets.py` |
| BLOCKING 2: Сетка опроса TOA — шаг 15 минут, дедупликация слотов матчей, T-3 деградация на дневной слот при расходе >= 440, пропуск при отсутствии ключа или hard stop, T-4 recheck для перенесённых матчей | `c9f0959` | `tests/test_scheduler_dedup.py` |
| BLOCKING 3: Внутренние лиги — `job_refresh_history` синхронизирует результаты в таблицу `Result`, вычисляет сезон динамически, fallback на football-data | `c9f0959` | `tests/test_tracking.py` |
| BLOCKING 4: Расчёт ставок — передача названий команд в `settle_user_bet` и корректный расчёт `team_totals` в won/lost/push вместо void | `c9f0959` | `tests/test_tracking.py` |
| BLOCKING 5: E2E — реальный сквозной прогон двухэтапного пайплайна через задачи планировщика без ручных вставок в БД | `c9f0959` | `tests/test_e2e_stage1.py`, `tests/test_e2e_stage2.py` |
| BLOCKING 6: Пайплайн новостей — префильтрация кандидатов перед поиском, сохранение в `PlayerAvailability`, предупреждающие баннеры устаревания снимка (> 6 ч) в API и MCP | `c9f0959` | `tests/test_news.py`, `tests/test_api.py`, `tests/test_mcp.py` |
| BLOCKING 7: Streamlit AppTest — тестирование представлений аналитического дашборда через официальный AppTest без прямого доступа к базе данных | `43e6fd2` | `tests/test_dashboard.py` |
| SHOULD FIX 8: Достижимость точек входа — проверка внешнего вызова за пределами определяющего файла для 16 функций контура в `scripts/check_no_stubs.py` | `c9f0959` | `tests/test_e2e_stage1.py` |
| SHOULD FIX 9: Защита от prompt-injection — вырезание тегов `<untrusted_news_data>` и закрывающих тегов из пользовательского ввода через регулярные выражения | `c9f0959` | `tests/test_news.py` |
| SHOULD FIX 10: Санитайзер саммари — защита от подмешивания ставок, нейтральный шаблон при пустых новостях и фильтрация терминов ставок | `c9f0959` | `tests/test_news.py` |
| SHOULD FIX 11: Квоты поиска — честный биллинг: 0 кредитов за фиксацию окна поиска и ровно 1 биллингуемый запрос за факт выполнения поиска | `c9f0959` | `tests/test_news.py` |
| SHOULD FIX 12: Пороги ценности — явные реалистичные поля в `ThresholdsConfig` (`totals_edge: 0.02`, `team_totals_edge: 0.05`, `btts_edge: 0.03`, `h2h_edge: 0.03`) | `c9f0959` | `tests/test_config.py`, `tests/test_engine.py` |
| SHOULD FIX 13: CLV в процентах — добавление `clv_pct` в схему `UserBetItem` REST API и единое отображение в интерфейсах | `c9f0959` | `tests/test_api.py` |
| SHOULD FIX 14: Динамический каппа_high в оговорках — подстановка значения из конфигурации в дисклеймеры API и MCP | `c9f0959` | `tests/test_api.py`, `tests/test_mcp.py` |
| SHOULD FIX 15: Документация API по умолчанию выключена (`ENABLE_API_DOCS = False`) | `c9f0959` | `tests/test_api.py` |
| SHOULD FIX 16: Kelly из `core/value` убран полностью (`kelly = 0.0`) | `c9f0959` | `tests/test_engine.py` |
| SHOULD FIX 17: Деградация TOA — calendar-day T-3 (`t3_slot_to_day` схлопывает вызовы в 1 суточный при расходе >= 440) | `c9f0959` | `tests/test_scheduler_dedup.py` |
| NIT: SPEC.md строка 132 — формула EV приведена к `EV = P(win) * odds + P(push) * 1 - 1` | `c9f0959` | `tests/test_engine.py` |
| NIT: `core/value/__main__.py` — замена слова «рекомендации» на «оценки цен» | `c9f0959` | `tests/test_persistence_cli.py` |

---

## Не реализовано

1. **Реальные сетевые запросы к платным API во время тестов и CI**:
   Во всех модульных и E2E-тестах используются моки (`respx`, фикстуры, `TestClient`) и тестовые базы SQLite в памяти. Реальные вызовы к внешним платным провайдерам (The Odds API, Gemini, Tavily, Brave) в тестах исключены для предотвращения расхода квот и исключения внешних сетевых зависимостей.
2. **Автоматическое выставление ставок у букмекеров**:
   Полностью отсутствует в соответствии с дизайн-принципами SPEC v3 и законодательством (Ustawa o grach hazardowych). Все ставки регистрируются пользователем вручную.
3. **Теневой режим модели (`shadow_llm`)**:
   Отложен в `docs/decisions.md` до накопления эмпирической выборки (>= 100 матчей) в живом логе `llm_calls`. Схема базы данных полностью подготовлена.
4. **Автоматический парсинг сайта Fortuna**:
   Сайт Fortuna защищён Cloudflare Turnstile / anti-bot. Автоматический веб-скрейпинг не реализован; используется ручная вставка текста страницы события пользователем (SPEC §5).
