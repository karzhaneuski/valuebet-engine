# ValueBet Engine — SPEC v3 Final Project Summary

**Date:** 2026-10-26  
**Repository:** `karzhaneuski/valuebet-engine`  
**Branch:** `v3-integration`  
**Specification:** `SPEC.md` (v3.0.0 — Analytical Architecture Pivot)  
**Phases Completed:** SPEC v3, Phase 6 (News & LLM Context), Phase 7 (User Bets & Automation), Phase 8 (REST API & FastMCP Server), Phase 9 (Streamlit Dashboard & Multi-Channel E2E Suite).

---

## 1. Executive Summary & Product Pivot

### 1.1 Why the Architecture Pivoted to SPEC v3
Backtesting results (`reports/backtest/SUMMARY_4b.md`) and empirical soft vs. sharp market measurements (`reports/soft_vs_sharp_totals.md`, `reports/fortuna_margin_study.md`) proved unequivocally that statistical Poisson/Dixon-Coles models cannot systematically beat modern sharp bookmakers, and that high soft-bookmaker margins (~33% on exact scores, ~21% on multiscore, 6–7% on totals) render passive single-bookmaker automated betting unprofitable.

Consequently, **SPEC v3** fundamentally transitioned ValueBet Engine from an *automated recommendation generator* into a **high-precision analytical decision-support and quote-auditing system**:
1. **Fair Exact Score Matrix via Sharp Market Lines:** The fair exact score probability distribution is derived entirely from devigged sharp market closing quotes (O/U 2.5 and 1X2 devig via Shin/multiplicative, bivariate Poisson optimization with $\rho=0$, and $\kappa_{\text{high}}=0.20$ smoothing on diagonal $\ge 2:2$).
2. **"Все рынки на равных":** All football markets (`exact_score`, `multiscore`, `goals_range`, `totals`, `team_totals`, `btts`, `h2h`) are computed from the fair matrix on equal footing.
3. **Objective Fortuna Price Evaluation:** Compares user-supplied Polish Fortuna odds against fair market prices. Outcomes where price exceeds fair price by configured margin are objectively labeled *"цена выше справедливой"* (price above fair). Automatic betting recommendations and Kelly sizing are strictly eliminated.
4. **Transparent User Bet Tracking & CLV Audit:** The user records actual bets manually (`user_bets` table). The engine settles bets by official final scores and calculates Closing Line Value (CLV) against the pre-kickoff fair market matrix snapshot. Portfolio ROI, hit rate, and CLV are audited with 95% bootstrap confidence intervals and mandatory sample size warnings ($N < 100$).
5. **Robust Security & Degradation Pacing:** Complete prompt injection defenses for scraped news, token-based API authentication, and a three-tier TOA quota degradation ladder (425 / 440 / 450 credits).

---

## 2. Architecture & File Mapping

```mermaid
flowchart TD
    subgraph Data Layer & Ingestion
        FD[Football-Data CSVs] --> DB[(SQLite Database)]
        US[Understat Results & xG] --> DB
        TOA[The Odds API] -->|Pacing Ladder 425/440/450| DB
        NEWS[Tavily / Brave Search] -->|Injection Delimiters| EXT[News Extractor]
    end

    subgraph Analytical Engine
        DB --> FM[Fair Pricing Matrix Engine]
        DB --> TRK[Settlement & CLV Engine]
        DB --> SCH[Scheduler Tasks & Workers]
        EXT --> LLM[Gemini LLM Provider]
        LLM --> BRF[Factual Briefing Engine]
    end

    subgraph Interfaces & Delivery
        FM & TRK & BRF & SCH --> API[FastAPI REST API]
        API --> DASH[Streamlit Dashboard<br>Zero Direct DB Access]
        FM & TRK & BRF --> MCP[FastMCP Python Server]
        MCP --> HERMES[Hermes Agent]
    end
```

### 2.1 File Map: Real vs Legacy Architecture
All features are implemented in real code without placeholders or fictional imports:

| Domain | Real Implemented File | Description |
|:---|:---|:---|
| **ORM & DB** | `core/orm.py` | Full schema: `odds_snapshots`, `user_bets`, `results`, `quota_counters`, `llm_calls`, `player_availability` |
| **Migrations** | `alembic/versions/596dfc9c3bf1_add_h2h_market_and_clv_sources.py` | Schema migration for `h2h` market and pre-kickoff `CLV_SOURCES` |
| **Value Engine** | `core/value/engine.py` | Evaluates all markets on equal footing; sets `price_above_fair`; preserves `calculate_kelly_quarter_capped` |
| **Fair Matrix** | `core/value/fair_matrix.py` | Bivariate Poisson exact score matrix with $\rho=0$, $\kappa_{\text{high}}=0.20$ |
| **Fortuna Parser** | `core/value/fortuna_input.py` | Section margins and Fortuna text parser for Polish interface |
| **Scheduler Tasks** | `core/scheduler/tasks.py` | `job_poll_odds_snapshots`, `job_poll_events`, `job_refresh_history`, `job_settle_matches_and_clv` |
| **Scheduler Service** | `core/scheduler/main.py` | FastAPI worker, Bearer token auth, localhost verification, concurrency lock on `/run-sync` |
| **Pacing Ladder** | `core/scheduler/pacing.py` | Projected burn evaluation; degradation thresholds at 425, 440, and 450 credits |
| **News Search** | `core/news/search.py` | Tavily client with Brave Search fallback; monthly quota tracking |
| **News Extractor** | `core/news/extractor.py` | Prompt injection defense with `<untrusted_news_data>` delimiter tag sanitization |
| **News Briefing** | `core/news/brief.py` | Neutral briefing generation; empty fallback template; anti-betting post-verification |
| **News CLI** | `core/news/__main__.py` | CLI: `python -m core.news brief --match-id <id>` |
| **LLM Provider** | `core/llm/provider.py` | `GeminiProvider` using `x-goog-api-key` header; logs failed calls to `llm_calls` |
| **Bet Settlement** | `core/tracking/settlement.py` | Multi-market settlement logic by official score; integer lines push handling |
| **CLV Calculation** | `core/tracking/clv.py` | Pre-kickoff fair matrix snapshot lookup; calculates CLV against fair matrix |
| **Portfolio Stats** | `core/tracking/stats.py` | ROI, hit rate, average CLV, Brier score, 95% bootstrap confidence intervals |
| **Tracking CLI** | `core/tracking/__main__.py` | CLI: `python -m core.tracking bet add\|list\|settle\|stats` |
| **REST API** | `api/main.py` | FastAPI application with CORS configuration and toggleable `/docs` |
| **Health API** | `api/routes/health.py` | Public health check with DB status, pacing state, zero DB URL leaks |
| **Matches API** | `api/routes/matches.py` | Match querying and full fair pricing distributions across all markets |
| **Fortuna API** | `api/routes/fortuna.py` | Section overrounds and quote comparison against fair prices |
| **Tracking API** | `api/routes/tracking.py` | User bet recording (rejects post-kickoff bets) and performance auditing |
| **FastMCP Server** | `mcp_server/server.py` | 6 tools for Hermes Agent over official Python SDK `mcp` |
| **Dashboard** | `dashboard/app.py` | Streamlit dashboard with 5 analytical views and zero direct DB access |
| **Dashboard Views**| `dashboard/views/*` | `matches.py`, `fortuna.py`, `bets.py`, `stats.py`, `status.py` |
| **Anti-Stub Script**| `scripts/check_no_stubs.py` | Automated verification of 0 forbidden stubs and 11 live contour functions |
| **E2E Suite** | `tests/test_e2e_stage1.py`, `tests/test_e2e_stage2.py` | Comprehensive multi-channel lifecycle and consistency test suites |

---

## 3. Real Quality Gates & Test Suite Execution

All quality checks were run in macOS (Python 3.12, `uv`) against an isolated copy of the database (`/tmp/v3.db`).

### 3.1 Pytest Execution (Verbatim Output)
```text
============================= test session starts ==============================
platform darwin -- Python 3.12.13, pytest-9.1.1, pluggy-1.6.0
rootdir: /Users/karzhaneuski/projects/valuebet-engine
configfile: pyproject.toml
plugins: anyio-4.13.1, respx-0.22.3
collected 364 items

tests/test_alembic_match_lineups.py .                                    [  0%]
tests/test_anomaly_warnings.py ..                                        [  0%]
tests/test_api.py ...............                                        [  4%]
tests/test_backtest_multiscore.py ....                                   [  6%]
tests/test_backtest_phase4b.py .......                                   [  8%]
tests/test_backtest_rho.py ....                                          [  9%]
tests/test_check_no_stubs.py ..                                          [  9%]
tests/test_cli_secrets.py .                                              [  9%]
tests/test_clv_metric.py .                                               [ 10%]
tests/test_config.py .......                                             [ 12%]
tests/test_dashboard.py ..........                                       [ 14%]
tests/test_dataset_quarantine.py .                                       [ 15%]
tests/test_dixon_coles.py ................                               [ 19%]
tests/test_e2e_stage1.py .                                               [ 19%]
tests/test_e2e_stage2.py .                                               [ 20%]
tests/test_engine.py ................                                    [ 24%]
tests/test_football_data_ingest.py ..................................    [ 33%]
tests/test_fortuna_parser.py ............                                [ 37%]
tests/test_http_secrets.py .......                                       [ 39%]
tests/test_inny_bias.py ..                                               [ 39%]
tests/test_llm.py .......                                                [ 41%]
tests/test_market_matrix.py ...................................          [ 51%]
tests/test_mcp.py ..........                                             [ 53%]
tests/test_multiplicative_devig.py ....                                  [ 54%]
tests/test_multiscore_engine.py ......                                   [ 56%]
tests/test_news.py ..............                                        [ 60%]
tests/test_odds_api_client.py .............                              [ 64%]
tests/test_pacing.py ..........                                          [ 66%]
tests/test_persistence_cli.py .............                              [ 70%]
tests/test_pinnacle_benchmark.py ...s                                    [ 71%]
tests/test_pl_line_shopping.py ....                                      [ 72%]
tests/test_postponement.py .................                             [ 77%]
tests/test_prefilter.py ..........................                       [ 84%]
tests/test_scheduler.py ......                                           [ 85%]
tests/test_scheduler_tasks.py ...........                                [ 88%]
tests/test_soft_vs_sharp_totals.py ........                              [ 91%]
tests/test_standby_model.py s                                            [ 91%]
tests/test_tracking.py ...........                                       [ 94%]
tests/test_understat_ingest.py ...................ss                     [100%]

======================== 361 passed, 3 skipped in 25.01s ========================
```

### 3.2 Anti-Stub Checker (`scripts/check_no_stubs.py`)
```text
=== Running Anti-Stub & Live Contour Checks ===

PASSED: Checked 4 directories for forbidden stub patterns.
PASSED: Verified definition and external call for all 11 live contour functions:
  ✓ job_poll_odds_snapshots        (defined in core/scheduler/tasks.py)
  ✓ job_poll_events                (defined in core/scheduler/tasks.py)
  ✓ job_refresh_history            (defined in core/scheduler/tasks.py)
  ✓ job_settle_matches_and_clv     (defined in core/scheduler/tasks.py)
  ✓ job_prefilter_candidates       (defined in core/scheduler/tasks.py)
  ✓ job_search_news_windows        (defined in core/scheduler/tasks.py)
  ✓ search_news_for_match          (defined in core/news/search.py)
  ✓ extract_player_news            (defined in core/news/extractor.py)
  ✓ generate_match_brief           (defined in core/news/brief.py)
  ✓ settle_user_bet                (defined in core/tracking/settlement.py)
  ✓ calculate_user_bet_clv         (defined in core/tracking/clv.py)

All anti-stub verification gates passed successfully.
```

### 3.3 Static Quality Gates
- **Ruff linter**: `uv run ruff check .` -> `All checks passed!`
- **Mypy type checker**: `uv run mypy` -> `Success: no issues found in 101 source files`
- **Mypy strict check**: `uv run mypy --strict core/model core/backtest core/value core/news core/llm core/tracking core/scheduler` -> `Success: no issues found in 34 source files`
- **Alembic migration & schema parity**:
  - `uv run alembic upgrade head` -> All 10 migrations upgraded cleanly.
  - `uv run alembic check` -> `No new upgrade operations detected.`

---

## 4. Honest Scope & "Не реализовано"

To maintain absolute transparency and integrity:

1. **Реальные сетевые запросы к платным API во время тестов**:
   - Во всех модульных и E2E-тестах используются моки (`respx`, фикстуры, `TestClient`). Это сделано умышленно согласно строгим проектным ограничениям (запрет на расходование квот The Odds API, Tavily, Brave, Gemini во время разработки и в CI).
2. **Автоматическое выставление ставок у букмекеров**:
   - Полностью отсутствует в соответствии с дизайн-принципами SPEC v3 и польским законодательством (Ustawa o grach hazardowych). Все ставки регистрируются пользователем вручную.
3. **Теневой режим (`shadow_llm`)**:
   - Отложен в `docs/decisions.md` до накопления репрезентативной выборки ($\ge 100$ матчей) в живом логе `llm_calls`. Схема базы данных полностью подготовлена.
4. **Автоматический парсинг сайта Fortuna**:
   - Сайт Fortuna защищён Cloudflare Turnstile / anti-bot. Автоматический веб-скрейпинг не реализован; используется ручная вставка текста страницы события в поле дашборда или через CLI/API (SPEC §5).

---

## 5. Production Deployment & Migration Guide

### Шаг 1. Миграция базы данных
Применить Alembic миграции на боевой базе данных:
```bash
uv run alembic upgrade head
```

### Шаг 2. Переменные окружения (.env)
Сконфигурировать необходимые токены и ключи доступа:
```bash
DATABASE_URL=sqlite:///./valuebet.db
API_SECRET_TOKEN=<your-secure-secret-token>
GEMINI_API_KEY=<your-google-gemini-key>
ODDS_API_KEY=<your-the-odds-api-key>
TAVILY_API_KEY=<your-tavily-search-key>
BRAVE_SEARCH_API_KEY=<optional-brave-key>
CORS_ORIGINS=http://localhost:8501,http://127.0.0.1:8501
ENABLE_API_DOCS=false
```

### Шаг 3. Запуск сервиса планировщика (Scheduler)
Фоновый демон сбора данных, префильтрации кандидатов и расчёта ставок:
```bash
uv run python -m core.scheduler.main
```
Или однократный запуск всех заданий в тестовом режиме:
```bash
uv run python -m core.scheduler.main --dry-run
```

### Шаг 4. Запуск REST API
API сервер для взаимодействия с клиентами и Hermes Agent:
```bash
uv run uvicorn api.main:app --host 0.0.0.0 --port 8000
```

### Шаг 5. Запуск Streamlit Dashboard
Аналитический интерфейс для пользователя:
```bash
uv run streamlit run dashboard/app.py --server.port 8501
```

### Шаг 6. Интеграция с Hermes Agent (MCP)
В конфигурацию Hermes Agent (`hermes.json`) добавить MCP сервер:
```json
{
  "mcpServers": {
    "valuebet": {
      "command": "uv",
      "args": ["run", "python", "-m", "mcp_server.server"],
      "cwd": "/path/to/valuebet-engine"
    }
  }
}
```
Инструменты FastMCP (`get_upcoming_matches`, `get_match_fair_prices`, `evaluate_fortuna_odds`, `record_user_bet`, `get_user_performance`, `get_news_brief`) будут автоматически доступны агенту.
