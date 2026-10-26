# Self-Review Report: Phase 6 (News & LLM Integration)

**Date**: 2026-10-26  
**Branch**: `phase-6-news`  
**Parent**: `spec-v3` (`de5d92c`)  

---

## 1. Scope & Compliance with SPEC v3 (§6)

Phase 6 implements the news extraction and LLM analytical briefing subsystem under the updated analytical tool paradigm:
1. **LLM Abstraction & Providers (`core/llm/provider.py`)**:
   - `LLMProvider` abstract base class defining `generate_text` and `generate_structured`.
   - `GeminiProvider`: REST client via `safe_request` (`core/http.py`), automatic backoff retries on transient errors, markdown fence stripping for JSON responses, token counting and estimated cost calculation, persisted to `llm_calls` table in SQLite/PostgreSQL.
   - `MockLLMProvider`: deterministic mock provider supporting text and structured responses, error injection, and call introspection without network calls.
2. **Search Client & Quota Pacing (`core/news/search.py`)**:
   - `TavilySearchClient` (primary) and `BraveSearchClient` (fallback) communicating through `core/http.py:safe_request`.
   - Quota tracking integrated into `api_quota_usage` and `quota_counters` (`SearchQuotaExceededError` if monthly cap exceeded).
   - Window deduplication: `has_searched_window` and `search_news_for_match` prevent redundant API consumption for candidates within `T-24h` and `T-3h` windows.
   - `FallbackSearchClient`: automatic seamless fallback to Brave when Tavily encounters errors.
3. **News Extraction & Prompt Injection Defense (`core/news/extractor.py`)**:
   - Schema validation via `NewsItem` and `NewsExtractionResult` (`player`, `team`, `status`, `source_url`, `quote_summary`, `published_at`).
   - Untrusted search snippets wrapped in `<untrusted_news_data>...</untrusted_news_data>`.
   - Strict system prompt explicitly instructs the LLM to treat untrusted blocks purely as passive text data and disregard all instructions inside them.
   - 1 retry on malformed JSON; graceful error handling returning empty results on failure.
4. **User Briefing Generator (`core/news/brief.py`)**:
   - Generates objective 2–3 paragraph Russian analytical summary of confirmed absences.
   - Strict negative prompt prohibits recommending bets, assessing win probabilities, or altering prices.
5. **Freshness Warning Flag (`core/news/freshness.py`)**:
   - `is_news_newer_than_market` compares normalized UTC publication time against odds snapshot time.
   - `get_freshness_warnings` displays alert:
     `"⚡ Свежая новость: рынок мог не успеть учесть отсутствие игрока X, обновите снимок"` for impactful absences (`out`, `suspended`, `doubtful`).
6. **Strict Lineup Matching Verification**:
   - Validated integration with `core/value/engine.py:match_key_player` ensuring normalized full name matching and `"нет данных о составе"` when lineups are unavailable.

---

## 2. Review Checklist Verification

| Checklist Item | Status | Verification Detail |
| :--- | :---: | :--- |
| **LLM does not affect probabilities / fair prices** | PASS | Fair prices and probabilities are derived exclusively from the devigged closing sharp matrix (`core/fair_matrix/`). LLM output is purely informational for the user. |
| **Prompt injection defense** | PASS | Untrusted content is isolated in `<untrusted_news_data>`. System prompts mandate passive evaluation and reject betting advice. |
| **Search quota & limits respected** | PASS | `check_and_increment_quota` verifies monthly cap in `quota_counters` before firing requests. |
| **Window deduplication** | PASS | Matches searched in window `T-24h` or `T-3h` are marked in `api_quota_usage` and skipped on subsequent runs. |
| **Strict player matching** | PASS | Normalized whole-name comparison; returns `"нет данных о составе"` when lineup data is absent. |
| **Freshness flag timestamp comparison** | PASS | UTC normalization handles naive/aware timestamps; flags absences published strictly after snapshot time. |
| **Tests green with mocks** | PASS | 7 passed in `test_llm.py`, 14 passed in `test_news.py`, full suite 306 passed, 0 failed. |

---

## 3. Test & Verification Output

- `uv run ruff check .`: All checks passed!
- `uv run mypy`: Success: no issues found in 77 source files.
- `uv run mypy --strict core/model core/backtest core/value`: Success: no issues found in 17 source files.
- `uv run pytest`: `306 passed, 3 skipped, 1 warning in 31.05s`.
