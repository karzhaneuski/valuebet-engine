# Progress Log: SPEC v3 & Phases 6–9 Implementation

**Current Status**: Part A Completed (branch `spec-v3`). Ready for push & CI verification before Part B.  
**Branch Stack**:
- `main` (`afeb49d`)
  └── `spec-v3` (commits: `8335d44` .. `a5a8cc1`)
      └── `phase-6-news` (pending Part B)
          └── `phase-7-tracking` (pending Part C)
              └── `phase-8-api-mcp` (pending Part D)
                  └── `phase-9-dashboard` (pending Part E)

---

## Part A: SPEC v3 & Code Alignment (`spec-v3`)

- [x] **A1. Decisions Documentation**:
  - `docs/decisions.md`: Added section `## Product pivot: analytics tool (SPEC v3)` covering all 7 design pivot decisions.
  - Commit: `8335d44` (`SPEC v3: document analytics tool pivot decision`).
- [x] **A2. Specification Rewrite**:
  - `SPEC.md`: Rewritten to v3 with `Changelog v3` at top, preserving `Changelog v2 (Архив)` below it.
  - Commit: `0ee993c` (`SPEC v3: rewrite specification to v3 with analytics pivot`).
- [x] **A3. Prompts & Checklists Update**:
  - `PROMPTS.md`: Updated tasks and review checklists for Phases 6–11.
  - Commit: `4402c25` (`SPEC v3: update PROMPTS.md phases 6-11 tasks and checklists`).
- [x] **A4. Code Alignment**:
  - Value engine & CLI: `price_above_fair` concept, removed Kelly / "рекомендация" from table output, "Выше спр." and notes/caveats display.
  - Lineups: strict whole-name normalized matching (`normalize_player_name`, `match_key_player`), missing lineups flagged as "нет данных о составе".
  - Past match protection: `commence_time_utc = NULL` validated against today's date and match status (`finished`, `live`, `in_play`).
  - Prefilter idempotency: prior accepted matches carried over across next-day reruns.
  - Unit tests updated (`tests/test_engine.py`, `tests/test_persistence_cli.py`).
  - Commit: `5a7d57b` (`SPEC v3: align value engine, CLI output, and prefilter safeguards`).
- [x] **A5. Polish Bookmakers Line Shopping Check**:
  - `scripts/pl_line_shopping_check.py`: parsing, Pinnacle totals devigging, MaxC margin calculation, comparison against fair prices.
  - Fixture: `tests/fixtures/pl_books_synthetic.txt`.
  - Test: `tests/test_pl_line_shopping.py` (4 passed).
  - Report: `reports/pl_line_shopping.md`.
  - Commit: `f50bed1` (`SPEC v3: implement Polish line shopping check script and test`).
- [x] **A6. Margin Study & Phase 5 Migration Refinements**:
  - `scripts/fortuna_margin_study.py` & `reports/fortuna_margin_study.md`: labeled 2026-10-25 matrix as Fortuna quotes (not sharp market, table $p_{\text{fair}} \times \text{odds}$ not representative).
  - `reports/phase5_report.md`: noted initial constraint `uq_recommendations_idempotency` was dropped in migration `e5f1b2c3d4e5` in favor of partial unique index `uq_recommendations_active`.
  - Commit: `a5a8cc1` (`SPEC v3: refine margin study caveats and phase 5 migration notes`).
- [x] **A7. Self-Review & Quality Gates**:
  - Report: `reports/self_review_part_a.md`.
  - Tests: `285 passed, 3 skipped, 1 warning in 30.91s`.
  - Ruff: clean.
  - Mypy: clean (70 source files).
  - Strict mypy: clean (17 source files).

---

## Part B: Phase 6 — News & LLM (`phase-6-news`)

- [x] B1. Create branch `phase-6-news` from `spec-v3`.
- [x] B2. `core/llm`: Provider interface (`LLMProvider`) with `generate_text` and `generate_structured`.
- [x] B3. `core/llm`: `GeminiProvider` implementation with logging into `llm_calls` table and retries.
- [x] B4. `core/news`: Search client (Tavily with Brave fallback) respecting search quotas.
- [x] B5. Extraction schema with prompt-injection defense (`published_at`, `player`, `status`, etc.).
- [x] B6. User brief (`core/news/brief.py`) in Russian without price alterations.
- [x] B7. Flag "новость свежее снимка рынка" comparing `published_at` against market snapshot timestamp.
- [x] B8. Unit tests with mocked providers (`tests/test_news.py`, `tests/test_llm.py`).
- [x] B9. Self-review report `reports/self_review_phase6.md`.
- [x] B10. Push & verify green CI.
  - Commit: `45100b7` (`Phase 6: implement news extraction, LLM provider, search clients, and freshness check`).
  - GitHub Actions run: `40252703190` (`success` - lint-type-test 1m19s, docker-arm64 7m4s).

---

## Part C: Phase 7 — Tracking & Scheduler (`phase-7-tracking`)

- [x] C1. Create branch `phase-7-tracking` from `phase-6-news`.
- [x] C2. TOA tier snapshotting by budget (~393 credits/30d), QuotaGuard cost tracking, degradation ladder.
- [x] C3. Match results fallback (football-data for top-5, TOA `/scores` for UCL).
- [x] C4. `user_bets` tracking and settlement by official final score across all markets.
- [x] C5. Pre-kickoff CLV calculation against closing fair market matrix (`clv_source="fair_market_matrix"` / `"pinnacle_correct_score"`).
- [x] C6. Metrics & statistics (P/L, ROI with variance warning, CLV, hit rate, 95% bootstrap CI).
- [x] C7. APScheduler tasks with `--dry-run` mode.
- [x] C8. Unit tests (`tests/test_tracking.py`, `tests/test_scheduler.py`).
- [x] C9. Self-review report `reports/self_review_phase7.md`.
- [x] C10. Push & verify green CI (Run `40254479322` success: lint-type-test in 1m16s, docker-arm64 in 6m50s).

---

## Part D: Phase 8 — API & MCP (`phase-8-api-mcp`)

- [ ] D1. Create branch `phase-8-api-mcp` from `phase-7-tracking`.
- [ ] D2. FastAPI (`api/`): Endpoints for matches, fair prices, fortuna eval, user bets, performance, health.
- [ ] D3. Bearer token auth via `API_SECRET_TOKEN` (all endpoints except `/health`).
- [ ] D4. MCP Server (`mcp_server/`) on official Python SDK `mcp` with tools for Hermes Agent.
- [ ] D5. Documentation `docs/hermes-setup.md`.
- [ ] D6. Unit tests (`tests/test_api.py`, `tests/test_mcp.py`).
- [ ] D7. Self-review report `reports/self_review_phase8.md`.
- [ ] D8. Push & verify green CI.

---

## Part E: Phase 9 — Dashboard & Final Summary (`phase-9-dashboard`)

- [ ] E1. Create branch `phase-9-dashboard` from `phase-8-api-mcp`.
- [ ] E2. Streamlit dashboard (`dashboard/`): 5 pages communicating strictly via REST API.
- [ ] E3. AppTest / API mock tests (`tests/test_dashboard.py`).
- [ ] E4. Self-review report `reports/self_review_phase9.md`.
- [ ] E5. Push & verify green CI.
- [ ] E6. Final report `reports/SUMMARY_v3.md` and chat summary.
