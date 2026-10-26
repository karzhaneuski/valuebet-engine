# Отчёт саморевью: Фаза 8 — REST API и MCP для Hermes Agent

**Ветка:** `phase-8-api-mcp`  
**Дата:** 2026-10-26  
**Статус:** Готово к пушу и верификации на CI  

---

## 1. Выполненные задачи

### 1.1 FastAPI REST API (`api/`)
1. **Эндпоинты по разделу 9 SPEC.md v3:**
   - `GET /matches?days=&league=&status=&search=&limit=&offset=`: список матчей с метаданными, статусом линий и поиском.
   - `GET /matches/{id}/fair-prices`: расчёт справедливой рыночной матрицы и коэффициентов по всем рынкам (1X2, тоталы с push для целых линий, точный счёт, inny >4 голов, обе забьют, мультисчёт, диапазоны голов, командные тоталы) с обязательными методологическими оговорками ($\rho = 0$, $\kappa_{\text{high}} = 1.05$).
   - `POST /fortuna-eval`: приём текста котировок Fortuna, расчёт маржи по секциям, сопоставление со справедливыми ценами, пометка «цена выше справедливой» (+EV) без слова «рекомендация» и без ставок по Келли.
   - `POST /user-bets`: регистрация ставки пользователя (match_id, market, outcome, odds, stake, bookmaker).
   - `GET /user-bets`: история ставок со статусами расчета (open, won, lost, push, void), P/L и pre-kickoff CLV.
   - `GET /performance`: честная статистика (P/L, ROI с 95% bootstrap CI, средний CLV, доля положительного CLV, предупреждение о высокой дисперсии при $N < 100$).
   - `GET /health`: статус базы данных, время последнего снимка, остаток квот TOA и поисковых клиентов.
2. **Безопасность и авторизация (`api/auth.py`):**
   - Авторизация по Bearer-токену `API_SECRET_TOKEN` на всех защищённых эндпоинтах.
   - Эндпоинт `/health` публичный и не требует авторизации.
   - Запросы без заголовка или с неверным токеном отклоняются с HTTP 401.
3. **Разрешение неоднозначностей команд (`api/matching.py`):**
   - Нормализация названий команд и удаление шумовых токенов.
   - При неоднозначном запросе возвращается список кандидатов, выбор не угадывается молча.

### 1.2 MCP-сервер (`mcp_server/`)
1. Построен на официальном Python SDK `mcp` (`mcp>=1.30.0` с `FastMCP`).
2. Зарегистрированы 6 инструментов для Hermes Agent:
   - `get_upcoming_matches(days, league)`
   - `get_match_fair_prices(match_id)`
   - `evaluate_fortuna_odds(raw_text, match_id, team_query)`
   - `record_user_bet(match_id, market, outcome, odds, stake, bookmaker)`
   - `get_user_performance(period)`
   - `get_news_brief(home_team, away_team)`
3. Описания инструментов на английском языке, ответы — компактные и на русском языке, оптимизированные под контекст LLM (без сырых дампов матриц 11×11).
4. Поддерживает запуск через stdio transport: `uv run python -m mcp_server.main`.

### 1.3 Документация (`docs/hermes-setup.md`)
- Детальное руководство по подключению Hermes Agent через `hermes.json` / `mcp.json`.
- Примеры вызовов через REST API (curl) с Bearer-авторизацией.
- Рекомендуемый фрагмент системного промпта для соблюдения аналитического позиционирования SPEC v3.

---

## 2. Чек-лист ревью фазы 8

- [x] Авторизация на всех эндпоинтах кроме `/health`: подтверждено тестами `test_health_is_public`, `test_auth_missing_token`, `test_auth_wrong_token`, `test_auth_valid_token`.
- [x] Ответы MCP компактны и не вываливают сырые матрицы: выводятся ключевые исходы, топ-3 счёта и inny.
- [x] Терминология строго согласована со SPEC v3 («цена выше справедливой», без «рекомендаций» и Келли).
- [x] Неоднозначные названия команд не угадываются молча: возвращается список подходящих матчей.
- [x] Документация `docs/hermes-setup.md` актуальна и содержит полные примеры.

---

## 3. Результаты тестов и проверок

### Ruff
```
uv run ruff check .
All checks passed!
```

### Mypy
```
uv run mypy
Success: no issues found in 96 source files
```

### Mypy --strict (математическое ядро)
```
uv run mypy --strict core/model core/backtest core/value
Success: no issues found in 17 source files
```

### Pytest
```
uv run pytest
============================== 345 passed, 3 skipped in 32.59s ==============================
```
Все 14 тестов API (`tests/test_api.py`) и 8 тестов MCP (`tests/test_mcp.py`) зелёные.
