# Master Progress Log: SPEC v3 & Phases 6–9 Live Realization

**Branch**: `v3-integration`  
**Date**: 2026-10-26  
**Status**: Completed. All quality gates, anti-stub checks, and E2E suites passing 100%.

---

## Stage 1: Core Engine, Scheduler, News, Tracking & Verification (1.1 – 1.8)

- [x] **1.1. Database Schema & Migration**:
  - `core/orm.py`: Added `"h2h"` to `ODD_MARKETS`; added `"team_totals"`, `"h2h"` to `RECO_MARKETS`; added `"returning"` to `PLAYER_STATUSES`; added `"fair_market_matrix"`, `"pinnacle_correct_score"`, `"football_data_closing"` to `CLV_SOURCES`.
  - Migration `alembic/versions/596dfc9c3bf1_add_h2h_market_and_clv_sources.py` generated and tested from scratch.
  - Verification: `alembic upgrade head` and `alembic check` clean.

- [x] **1.2. Value Engine Pivot ("Все рынки на равных")**:
  - `core/value/engine.py`: Evaluates `exact_score`, `multiscore`, `goals_range`, `totals`, `team_totals`, `btts`, `h2h`.
  - Sets `price_above_fair = True` objectively based on edge threshold without generating "recommendations".
  - Backward compatibility: Preserved `calculate_kelly_quarter_capped(p_fair, item.odds)` for existing callers.
  - Verification: All 16 unit tests in `tests/test_engine.py` pass.

- [x] **1.3. Scheduler Real Tasks & TOA Ingestion**:
  - `core/scheduler/tasks.py`:
    - `job_poll_odds_snapshots`: Active tier windows T-24 (23-25h before kickoff) and T-3 (2.5-3.5h), 15m delay tolerance, 1 retry, pacing degradation (425/440/450).
    - `job_poll_events`: Free `/events` polling, detects kickoff shift > 15 min, flags `postponed_suspected`, triggers T-4 recheck.
    - `job_refresh_history`: Results + xG from Understat for top 5, TOA `/scores` for UCL, sets finished status.
    - `job_settle_matches_and_clv`: Settle open bets and compute pre-kickoff CLV against closing fair market matrix.
    - `run_all_jobs_sync`: Synchronous execution runner for all scheduled jobs.
  - `core/scheduler/main.py`: Secured `/run-sync` with localhost/127.0.0.1 validation, Bearer token verification, concurrency lock (`threading.Lock`), background scheduler.
  - Verification: `tests/test_scheduler.py` (6 passed).

- [x] **1.4. News Pipeline & Injection Defense**:
  - `core/news/extractor.py`: Scraped text isolated inside `<untrusted_news_data>` delimiters; `_sanitize_untrusted_input` strips delimiter tags to prevent prompt injection escapes; 1 retry before graceful empty list fallback.
  - `core/news/brief.py`: Returns exact template `"По матчу {home_team} — {away_team} подтверждённых данных о составах нет."` on empty news; post-verification `verify_and_sanitize_brief` detects betting words and probability percentages, substituting neutral template.
  - `core/news/__main__.py`: CLI created supporting `brief --match-id <id> | --match "A - B" --window T-24h|T-3h [--dry-run]`, enforces weekly candidate check.
  - Verification: `tests/test_news.py` (14 passed).

- [x] **1.5. Gemini LLM Provider**:
  - `core/llm/provider.py`: `GeminiProvider` sends API key via `x-goog-api-key` header (never query param); logs failed calls to `llm_calls` table with `ERROR: <msg>`.
  - Verification: `tests/test_llm.py` (7 passed).

- [x] **1.6. Tracking & Settlement**:
  - `core/tracking/settlement.py`: Canonical outcome keys, side resolution via `match_team_side`, unknown market returns `"void"`.
  - `core/tracking/clv.py`: Pre-kickoff fair matrix snapshot lookup; calculates CLV against fair matrix; rejects post-kickoff evaluation for scheduled matches.
  - `core/tracking/__main__.py`: CLI created with `bet add`, `bet list`, `bet settle`, `bet stats` in Russian. Rejects bet placement after kickoff.
  - Verification: `tests/test_tracking.py` (11 passed).

- [x] **1.7. Stage 1 E2E Test**:
  - `tests/test_e2e_stage1.py`: Full lifecycle from pre-match (T-48 schedule, T-24 poll, fair matrix, real Fortuna fixture evaluation, user bet placement) to post-match (T-3 closing snapshot, kickoff, finish, settlement, pre-kickoff CLV calculation, performance metrics).
  - Verification: `tests/test_e2e_stage1.py` passed (1 passed in 0.86s).

- [x] **1.8. Anti-Stub Verification**:
  - `scripts/check_no_stubs.py`: Scans `core`, `api`, `mcp_server`, `dashboard` for forbidden stubs (`TODO`, `FIXME`, placeholder dicts, `In production, invokes...`). Verifies definition and external call for all 11 live contour functions.
  - Verification: Passes 100% cleanly.

---

## Stage 2: Interfaces, Security, Dashboard & Full Consistency (2.1 – 2.8)

- [x] **2.1. Secrets & CI**:
  - `core/http.py`: `register_runtime_secrets()` registers all runtime secrets from `.env`.
  - Entrypoints updated to call `register_runtime_secrets()`: `api.main`, `mcp_server.server`, `core.scheduler.main`, `core.news.__main__`, `core.tracking.__main__`, `core.value.__main__`.
  - `.github/workflows/ci.yml`:
    - Added `v3-*` branch trigger.
    - Secrets scan made case-insensitive with allowlist for code patterns (`sec.api_secret_token`, `your-secret-api-token`) and scanning for `tvly-`, `AIza`, `BSA`.
    - Added step `Check no stubs` running `uv run python scripts/check_no_stubs.py` before pytest.
    - Added step `Mypy (strict on core)`.
  - `tests/test_http_secrets.py`: Unit tests verifying redaction of Gemini keys, Brave tokens, Tavily keys, and custom secret tokens.

- [x] **2.2. Line Shopping Report**:
  - `reports/pl_line_shopping.md`: Completed report with summary metrics, per-match comparison, synthetic MaxC margin compression, and disclaimer on synthetic fixture.

- [x] **2.3. Health Endpoint Security & Pacing**:
  - `api/routes/health.py`:
    - Returns `status: "degraded"` when DB or essential tables are inaccessible.
    - Returns `toa_credits_remaining: null` on error (never 0).
    - Omits `database_url` (returns `null`).
    - Sources pacing state from backend (`evaluate_degradation_state` in `core.scheduler.pacing`).
  - Verification: `tests/test_api.py` includes health failure and public endpoint checks.

- [x] **2.4. MCP Server Validation & Enhancements**:
  - `mcp_server/server.py`:
    - `record_user_bet`: Validates market and canonical outcome key, rejects past-kickoff bets (`now >= commence_time`), auto-populates `p_fair_at_placement`.
    - `get_news_brief`: Handles team query ambiguity by returning candidates, includes caveat $\kappa_{\text{high}} = 0.20$.
  - Verification: `tests/test_mcp.py` (10 passed).

- [x] **2.5. FastMCP Tools**:
  - All 6 tools verified: `get_upcoming_matches`, `get_match_fair_prices`, `evaluate_fortuna_odds`, `record_user_bet`, `get_user_performance`, `get_news_brief`.

- [x] **2.6. API Security & Config**:
  - `api/main.py`: CORS explicit origins `["http://localhost:8501", "http://127.0.0.1:8501"]` or `CORS_ORIGINS`; `allow_credentials=False` if wildcard; `/docs` behind `ENABLE_API_DOCS` flag.

- [x] **2.7. Dashboard Alignment & Unit Contract**:
  - `dashboard/app.py`: Full SPEC §11 responsible gaming disclaimer.
  - `dashboard/views/stats.py`: Formats ROI as `f"{roi:+.1f}%"` directly from percentage returned by API; verified via TestClient contract test.
  - `dashboard/views/status.py`: Degradation banner sourced directly from `/health` (`pacing_stage`, `pacing_degraded`, without hardcoded 380 threshold).
  - `dashboard/views/fortuna.py`: "🟢 ВЫШЕ справедливой" badge based on `price_above_fair`.

- [x] **2.8. Stage 2 E2E Integration Test**:
  - `tests/test_e2e_stage2.py`: Verifies multi-channel consistency across REST API (FastAPI TestClient), MCP tools in-process, and Streamlit Dashboard (AppTest). All channels report identical settled bets, turnover, P/L, ROI, and sample size warnings.
  - Verification: `tests/test_e2e_stage2.py` passed (1 passed in 2.05s).

---

## Stage 3: CI/CD Quality Gates & Anti-Stub Verification

- [x] **Zero Forbidden Stubs**: `scripts/check_no_stubs.py` verified 0 forbidden stub patterns in 4 directories and external invocation of all 11 contour functions.
- [x] **Mypy Strict**: `uv run mypy --strict core/model core/backtest core/value core/news core/llm core/tracking core/scheduler` (clean, 34 files).
- [x] **Pytest Suite**: 363 passed, 3 skipped in 25.07s locally.
- [x] **GitHub Actions CI**: Run ID `40317518951` on `v3-integration` (commit `692bd8f`):
  - `lint-type-test`: Passed in 1m57s (ID `121553724144`).
  - `docker-arm64`: Passed in 6m25s (ID `121553723848`).

