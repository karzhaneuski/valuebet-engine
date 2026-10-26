# Phase 9: Streamlit Analytical Dashboard & AppTest Suite — Self Review

**Branch:** `phase-9-dashboard`  
**Base:** `phase-8-api-mcp`  
**Date:** 2026-10-26  
**Scope:** Streamlit analytical UI (5 pages), strict REST API isolation, AppTest coverage.

---

## 1. Architectural Guardrail Verification (Zero SQLite / ORM in Dashboard)

- **Requirement:** Dashboard must NEVER access SQLite, SQLAlchemy, or internal database session factories directly. All interaction must flow strictly through the REST API (`dashboard.api_client.ApiClient`).
- **Enforcement:**
  - Automated test in `tests/test_dashboard.py::test_dashboard_zero_direct_sqlite_access` scans all files in `dashboard/` for imports or usages of `sqlite3`, `sqlalchemy`, `core.orm`, `core.db`, `create_engine`, `sessionmaker`, `get_engine`, `get_db`.
  - Result: **0 violations detected across all dashboard files**.
  - All data retrieval and mutations occur exclusively via HTTP calls to FastAPI endpoints (`/matches`, `/matches/{id}/fair-prices`, `/fortuna-eval`, `/user-bets`, `/performance`, `/health`).

---

## 2. Implementation Overview (5 Dedicated Pages)

1. **Матчи и справедливые цены (`dashboard/views/matches.py`):**
   - Filters: league, time horizon (1-30 days), team name search.
   - Fair price tables: 1X2, BTTS, Totals (half and integer lines with push probability), Exact Score grid.
   - Highlights "inny" (>4 goals) and transparently prints methodological caveats (source bookmakers, standby model flag).

2. **Оценка цен Fortuna (`dashboard/views/fortuna.py`):**
   - Parses pasted text lines from Fortuna.
   - Calculates section margins (~31% on exact score, ~5-6% on 1X2 and totals).
   - Prominently warns about exact score margin (>25%).
   - Displays outcomes with price higher than fair ("цена выше справедливой") with green badges (+EV).
   - **Zero recommendations, zero Kelly sizing.**

3. **Мои ставки (`dashboard/views/bets.py`):**
   - Interactive bet registration form (match, market, outcome, odds, stake, bookmaker).
   - Filterable history table with settled P/L, CLV %, status, and closing line source.

4. **Статистика и CLV (`dashboard/views/stats.py`):**
   - Honest portfolio accounting: net P/L (units), ROI (%), average CLV (%), hit rate (%).
   - 95% bootstrap confidence intervals for ROI and CLV.
   - **Mandatory sample size warning:** Automatically displayed whenever $N < 100$ settled bets.
   - Plotly cumulative P/L and closing line audit charts.

5. **Состояние данных (`dashboard/views/status.py`):**
   - Health check metrics, UTC timestamp of latest odds snapshot.
   - TOA 500 monthly credit budget visual progress bar.
   - Degradation ladder alert banner (normal / 380+ credit degraded / 450+ hard stop).
   - Search quota counters (Tavily, Brave).

6. **Navigation & Responsible Gaming (`dashboard/app.py`):**
   - Clean sidebar radio navigation.
   - Persistent responsible gaming disclaimer: *"Сервис предоставляет исключительно справочную аналитику. Ставки сопряжены с риском потери средств. Полное отсутствие рекомендаций."*

---

## 3. Test & Verification Results

- `uv run ruff check .`: **All checks passed!**
- `uv run mypy`: **Success: no issues found in 97 source files.**
- `uv run mypy --strict core/model core/backtest core/value`: **Success: no issues found in 17 source files.**
- `uv run pytest`: **354 passed, 3 skipped in 34.43s.**
  - 9 dedicated tests in `tests/test_dashboard.py` covering:
    - Architectural zero-DB import verification.
    - ApiClient header and request mocking.
    - Page 1 Matches rendering.
    - Page 2 Fortuna evaluation and margins.
    - Page 3 User Bets logging and history table.
    - Page 4 Stats, CLV, bootstrap intervals, and small sample size warning ($N < 100$).
    - Page 5 Status, TOA credits progress, and degradation ladder.
    - Graceful error handling for API connection failures (`st.error`).
