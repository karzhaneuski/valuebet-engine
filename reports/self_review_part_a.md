# Self-Review Report: Part A (SPEC v3 & Code Alignment)

**Date**: 2026-10-26  
**Branch**: `spec-v3`  
**Base Commit**: `afeb49d` (clean `main`)

---

## 1. Scope & Objectives Verification

Part A executes the foundational transition to **SPEC v3 (Product Pivot: Analytics Tool)** across documentation, specifications, value engine core, prefilter safeguards, and Polish bookmaker line shopping research.

### Checklist of Deliverables

- [x] **A1. Decisions Documentation (`docs/decisions.md`)**:
  - Added section `## Product pivot: analytics tool (SPEC v3)`.
  - Recorded 7 formal decisions:
    1. Analytical Tool Concept: pivot from automated betting recommendations to decision-support and price evaluation.
    2. All Markets on Equal Footing: exact score, multiscore, ranges, totals with push EV, team totals, BTTS, and 1X2 evaluated with fair probabilities and caveats.
    3. Fortuna Evaluation: margin calculation and label "цена выше справедливой"; removal of "рекомендация" and Kelly bet sizing.
    4. User Bet Tracking: recording user bets and evaluating pre-kickoff CLV against latest market snapshot.
    5. Dixon-Coles Standby Role: zero-alpha standby model and anomaly warning ($|s_{\text{market}} - s_{\text{model}}| > 0.15$).
    6. Responsible Gambling: explicit risks disclaimers in all interfaces.
    7. Polish Bookmakers Line Shopping: synthetic margin and fair comparison research.
  - Commit: `8335d44` (`SPEC v3: document analytics tool pivot decision`).

- [x] **A2. Specification Rewrite (`SPEC.md`)**:
  - Rewritten to v3 with `Changelog v3 (Пивот в аналитический инструмент, ревизия продукта)` at the top.
  - Preserved historical `Changelog v2 (Архив)` immediately following.
  - Updated all sections: Goal (§1), Key Principle (§2), Data & Quota budget (§3), Fair Market Matrix (§4), Fortuna Evaluation (§5), News & Lineups (§6), Tracking & CLV (§7), Backtest (§8), Architecture (§9), Language (§10), Responsible Gambling (§11).
  - Commit: `0ee993c` (`SPEC v3: rewrite specification to v3 with analytics pivot`).

- [x] **A3. Phase Prompts & Checklists (`PROMPTS.md`)**:
  - Updated phases 6–11 tasks and review checklists:
    - Phase 6 (`phase-6-news`): LLM abstraction, Tavily/Brave search, injection defense, user brief, flag "новость свежее снимка рынка", no shadow price modifications.
    - Phase 7 (`phase-7-tracking`): TOA quotas (~393 credits/30d), results fallback, `user_bets` tracking, pre-kickoff CLV, APScheduler with dry-run mode.
    - Phase 8 (`phase-8-api-mcp`): FastAPI with Bearer auth, official Python MCP server for Hermes, `docs/hermes-setup.md`.
    - Phase 9 (`phase-9-dashboard`): Streamlit dashboard (5 pages, talking exclusively to REST API).
    - Phase 10 (`phase-10-deploy`): ARM64 docker-compose, memory limits, HTTPS/auth proxy, daily backup.
    - Phase 11 (`phase-11-ucl`): UCL experimental contour.
  - Commit: `4402c25` (`SPEC v3: update PROMPTS.md phases 6-11 tasks and checklists`).

- [x] **A4. Code Alignment (`core/value/`, `core/config.py`, tests)**:
  - `EvaluatedOutcome`: added `price_above_fair: bool` (synchronized with `recommended: bool` for full backwards compatibility).
  - `EvaluationResult`: added `price_above_fair_count: int`; updated `format_table()`:
    - Removed column `¼ Келли`.
    - Replaced `Реком.` with `Выше спр.`.
    - Replaced `Причина отказа` with `Примечание / оговорки`.
    - Summary string updated to `Итого исходов с ценой выше справедливой: N`.
    - Word "рекомендация" completely eliminated from table output.
  - Supported `btts` market evaluation in `core/value/engine.py` and `core/value/fortuna_input.py`.
  - Player Name Matching:
    - Implemented `normalize_player_name` (NFKD strip accents, lowercase, collapse whitespace) and `match_key_player`.
    - Strict whole-name matching against key players set (no loose bidirectional substring).
    - If `match_lineups` has no data for the team, `has_team_lineups` returns `False`, and absences are flagged as `нет данных о составе` (NOT marked as key).
  - Past Match Protection (`core/value/__main__.py`):
    - When `--save` is used, if `commence_time_utc = NULL`, verified against today's date and match status (`finished`, `live`, `in_play`).
    - Attempting to save for started/finished matches exits with code 1 and error message.
  - Prefilter Idempotency (`core/value/__main__.py`):
    - When running `candidates`, matches already accepted on prior dates in the same ISO week are carried over as `accepted=True`.
    - They are not re-evaluated against remaining caps where they could be rejected with "Превышен недельный бюджетный лимит".
  - Commit: `5a7d57b` (`SPEC v3: align value engine, CLI output, and prefilter safeguards`).

- [x] **A5. Polish Bookmakers Line Shopping Check**:
  - Implemented `scripts/pl_line_shopping_check.py` with parsing, Pinnacle devigged fair odds comparison, margin compression calculation, and Markdown reporting.
  - Created synthetic test fixture in `tests/fixtures/pl_books_synthetic.txt`.
  - Added unit and integration tests in `tests/test_pl_line_shopping.py` (4 tests passing).
  - Generated research report `reports/pl_line_shopping.md` (average operator margin 4.45% compresses to MaxC margin 1.10%, achieving 3.35% margin compression).
  - Commit: `f50bed1` (`SPEC v3: implement Polish line shopping check script and test`).

- [x] **A6. Margin Study & Phase 5 Report Refinements**:
  - In `scripts/fortuna_margin_study.py` and `reports/fortuna_margin_study.md`: explicitly labeled the 2026-10-25 matrix as "котировки Fortuna, не острый рынок, таблица p_fair×odds нерепрезентативна".
  - In `reports/phase5_report.md` (Step 6): documented that initial constraint `uq_recommendations_idempotency` was dropped in migration `e5f1b2c3d4e5` in favor of partial unique index `uq_recommendations_active`.
  - Commit: `a5a8cc1` (`SPEC v3: refine margin study caveats and phase 5 migration notes`).

---

## 2. Quality Gate Verification

### Ruff Check
```
$ uv run ruff check .
All checks passed!
```

### Mypy (Default)
```
$ uv run mypy
Success: no issues found in 70 source files
```

### Mypy (Strict on Core Modules)
```
$ uv run mypy --strict core/model core/backtest core/value
Success: no issues found in 17 source files
```

### Pytest (Full Suite)
```
$ uv run pytest
============================= test session starts ==============================
platform darwin -- Python 3.12.9, pytest-8.3.4, pluggy-1.5.0
rootdir: /Users/karzhaneuski/projects/valuebet-engine
configfile: pyproject.toml
collected 288 items

tests/test_backfill_safeguards.py ..                                     [  0%]
tests/test_backtest.py .................                                 [  6%]
tests/test_backtest_cli.py ........                                      [  9%]
tests/test_backtest_data.py ......                                       [ 11%]
tests/test_backtest_honesty.py ......                                    [ 13%]
tests/test_backtest_mixture.py ......                                    [ 15%]
tests/test_backtest_roi.py ......                                        [ 17%]
tests/test_cache.py .                                                    [ 17%]
tests/test_cli_smoke.py .                                                [ 18%]
tests/test_config.py ...                                                 [ 19%]
tests/test_db.py ...                                                     [ 20%]
tests/test_db_copy.py ..                                                 [ 20%]
tests/test_devig.py ..........                                           [ 24%]
tests/test_dixon_coles.py ...........                                    [ 28%]
tests/test_engine.py ......................                              [ 35%]
tests/test_fair_matrix.py .....................                          [ 43%]
tests/test_football_data.py ....                                         [ 44%]
tests/test_fortuna_input.py ...................                          [ 51%]
tests/test_http.py ...                                                   [ 52%]
tests/test_ingest.py .                                                   [ 52%]
tests/test_inny_bias.py ..                                               [ 53%]
tests/test_log_loss.py .....                                             [ 54%]
tests/test_markets.py .............                                      [ 59%]
tests/test_model_smoke.py ........                                       [ 62%]
tests/test_persistence_cli.py .......................                    [ 70%]
tests/test_pinnacle_benchmark.py ..........                              [ 73%]
tests/test_pinnacle_correct_score_storage.py ...                         [ 74%]
tests/test_pl_line_shopping.py ....                                      [ 76%]
tests/test_prefilter.py .......                                          [ 78%]
tests/test_prob_grid.py .......                                          [ 80%]
tests/test_quota.py ..........                                           [ 84%]
tests/test_sample_data.py ....                                           [ 85%]
tests/test_soft_vs_sharp_totals.py ........                              [ 88%]
tests/test_standby_model.py ..............s                              [ 93%]
tests/test_structures.py ....                                            [ 94%]
tests/test_time_decay.py .....                                           [ 96%]
tests/test_toa_fetch.py ....                                             [ 97%]
tests/test_toa_prematch.py .......                                       [100%]
=============================== warnings summary ===============================
.venv/lib/python3.12/site-packages/fastapi/testclient.py:1
  /Users/karzhaneuski/projects/valuebet-engine/.venv/lib/python3.12/site-packages/fastapi/testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
    from starlette.testclient import TestClient as TestClient  # noqa

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
285 passed, 3 skipped, 1 warning in 30.91s
```

---

## 3. Commit Stack Summary on `spec-v3`

```
* a5a8cc1 SPEC v3: refine margin study caveats and phase 5 migration notes
* f50bed1 SPEC v3: implement Polish line shopping check script and test
* 5a7d57b SPEC v3: align value engine, CLI output, and prefilter safeguards
* 4402c25 SPEC v3: update PROMPTS.md phases 6-11 tasks and checklists
* 0ee993c SPEC v3: rewrite specification to v3 with analytics pivot
* 8335d44 SPEC v3: document analytics tool pivot decision
* afeb49d (origin/main, main) Phase 5: canonical z=-2.28 in decisions and report
```
All commits are clean, focused, and self-contained with passing checks.
