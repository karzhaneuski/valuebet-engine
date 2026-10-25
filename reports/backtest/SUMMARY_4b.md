# Phase 4 review (Part A) + Phase 4b hybrid (Part B) + Ingest & Spec v2 (Parts C & D) — Summary

Branches NOT merged into main: `phase-4-backtest` (Part A, merged into `phase-4b-hybrid`), `phase-4b-hybrid` (active branch, contains Parts A, B, C, D).
`valuebet.db`, `data/raw/`, `.env`, and `backups/` were untouched (all DB operations and migrations ran on copy `/tmp/bt4b.db`).
No network fetches or paid API calls were made (all tests and computations offline).
Test period (kickoff >= 2025-07-01) was not touched for tuning; all evaluations performed on validation (seasons 2023, 2024) with calendar-week bootstrap clusters and cross-season fitting.

---

## 1. Item -> What was done -> Commit -> Verification

### Step 0: Repository State & Branch Continuity
| Item | What was done | Commit | Verified by |
|---|---|---|---|
| Step 0 | Clean working tree verified; checked commit continuity: `b499dee` and `c47ba52` verified in `main` history | — | `git status`, `git branch --contains b499dee` |

### Part A: Phase 4 Backtest Fixes (branch `phase-4-backtest`, merged into `phase-4b-hybrid`)
| Item | What was done | Commit | Verified by |
|---|---|---|---|
| S1 | `part3_test.md`: removed misleading claims ("consistent with an efficient closing market", "the calibrated probabilities are the product...", "slight conservative bias … fewer false 'value' signals"); fixed §3.2b table `fair ROI` from duplicating model ROI to `-0.0613` (fair market de-vig); updated random benchmark to expected `-0.0659` (-6.6%, mean of always over/under) | `c9297a5` | `reports/backtest/part3_test.md` §3.2a/§3.2b |
| NIT | Strict mypy override added to `pyproject.toml` for `core.backtest.*`; fixed 13 `[type-arg]` type annotations in `core/backtest/metrics.py` and `core/backtest/runner.py`; cleaned runner `predictions` docstring; explicitly re-exported `ensure_window_allowed` | `35ee029` | `uv run mypy` (0 errors in core.backtest), `uv run ruff check .` |
| Merge | Merged `phase-4-backtest` into `phase-4b-hybrid`; clean merge commit with resolved PROGRESS.md | `010287c` | `git merge phase-4-backtest`, 201 tests passing |

### Part B: Phase 4b Hybrid & Market Matrix (branch `phase-4b-hybrid`)
| Item | What was done | Commit | Verified by |
|---|---|---|---|
| B1 (prematch table) | `prematch_odds` table (own table with closing CHECK constraint, CLV-by-bookmaker support) + migration `a86d9da4539e` + fd loader (Avg/P/Max totals) + idempotent upsert | `f42c8dc` | `tests/test_football_data.py::test_prematch_odds_parsed_and_stored` |
| B1 (1X2 load) | Added `home_price`, `draw_price`, `away_price` columns to `prematch_odds` and `closing_odds` + Alembic migration `b13589d81812`; fd loader parses AvgH/D/A, PSH/D/A, MaxH/D/A, AvgCH/D/A, PSCH/D/A, MaxCH/D/A; idempotent upsert on `/tmp/bt4b.db`: 21,017 updated, 5,543 written; re-run: 0 writes / 0 updates | `49da65a` | `tests/test_football_data.py` (all passing), run on `/tmp/bt4b.db` |
| B1 (CLV fix) + S5 | `scripts/backtest_mixture.py`: fixed CLV bet selection to prematch price ($p \cdot \text{price}_0 - 1 \ge 0.02$), measured shift prematch -> closing, ties separated. Validation: 1533 bets, line moved toward us in 47.4% (48.5% ex ties), mean shift 0.00 pp. S5 cross-validation across seasons: OOS LL 0.6656 vs pure market 0.6654 (CI [-0.0018, +0.0021]) -> O/U stays reference-only | `9647a5d` | `reports/backtest/4b_mixture.json` |
| B1c, B2, B3, S3 | `scripts/backtest_hybrid.py`: added `--share {model, market_1x2}`, `--all-market`, cross-fit `--cross-fit`, and frozen-config lock. Exact-score validation LL: 1X2 share vs model share -0.0157 [-0.0205, -0.0113]; all-market -0.0163 [-0.0213, -0.0118]; alpha cross-fit = 0.0000 in both season directions; rho model vs rho=0 -0.0006 [-0.0026, +0.0014]; Pinnacle -0.0159. S3 kappa cross-fit by season: 2023->2024: 0.337, 2024->2023: 0.179 | `0acaf64` | `reports/backtest/4b_hybrid_market_1x2.json`, `4b_hybrid_all_market.json`, `4b_hybrid_market_1x2_k02.json` |
| S2 Lock | Test lock gate verified: `scripts/backtest_hybrid.py` and `scripts/backtest_mixture.py` require `FROZEN_CONFIG_4b.json` under `--period test`; absent path raises `TestPeriodLockedError` | `0acaf64` | `tests/test_backtest_infra.py::test_phase4b_scripts_test_lock` |
| B6, S4, docs | Updated `docs/decisions.md` (B3, B4, B5, B6 rewritten for Phase 5 design); updated `reports/backtest/SUMMARY_4b.md` and `PROGRESS.md`; recorded football-data timestamp caveat (no outcome leakage) and generic CHECK constraint | `50d0a0d` | `docs/decisions.md` |

### Part C: Ingest CLI & Robustness (branch `phase-4b-hybrid`)
| Item | What was done | Commit | Verified by |
|---|---|---|---|
| C1 | Added `--lineups-only` CLI flag to `core.ingest backfill` (requires `--lineups-since`), skipping Understat schedule discovery and football-data downloads/parsing entirely; unit tests verify validation error when `--lineups-since` is missing and ensure schedule/fd loaders are bypassed | `0c43604` | `tests/test_lineups_backfill.py::test_backfill_lineups_only_*` |
| C2 | Wrapped Understat schedule reading in `UnderstatLoader.schedule_rows` with retry loop and exponential backoff on transient errors (e.g. TLS client timeouts) with informative error messages; `cmd_backfill` catches per-league/season schedule errors, records them in `report['understat_errors']`, and continues backfill for subsequent leagues without crashing | `7ecf965` | `tests/test_ingest_review.py::test_c2_understat_*` (flaky retry, exhausted retry, backfill error report) |
| C3 | Quieted `soccerdata`, `TLSLibrary`, `TLSRequests`, and `urllib3` loggers to WARNING across all CLI entry points and `UnderstatLoader`; set `SOCCERDATA_LOGLEVEL=WARNING` before library imports to eliminate startup team/league dictionary announcements and disk caching info messages | `124d116` | `tests/test_logging.py::test_c3_soccerdata_loggers_quieted` |

### Part D: SPEC v2 & PROMPTS.md Updates (branch `phase-4b-hybrid`)
| Item | What was done | Commit | Verified by |
|---|---|---|---|
| SPEC v2 | Updated `SPEC.md` to v2: added top Changelog v2 referencing decisions B3-B6; target defined as fair market matrix of exact score with $\kappa_{\text{high}}=0.20$ and edge against Fortuna odds; Dixon-Coles model assigned to fallback and anomaly detection; exact score and multiscore are recommendations, totals and BTTS are reference-only, 1X2 is strictly input-only; TOA sport-level quota budget documented (~188 credits/30d); CLV defined as Phase 7 quality arbiter | `c478aa0` | `SPEC.md`, `git diff` |
| PROMPTS.md | Updated task descriptions and review checklists for Phases 5, 6, and 7 to align with SPEC v2 (fair market matrix, Fortuna manual odds entry, edge calculation, anomaly detection, CLV tracking) | `c478aa0` | `PROMPTS.md`, `git diff` |

### Review Round 2 Fixes (Blocking Б1-Б2, Should Fix S1-S7, NITs)
| Item | What was done | Commit | Verified by |
|---|---|---|---|
| NITs | Removed "slight conservative bias" from `docs/decisions.md`; `cmd_backfill` exits with non-zero status on `understat_errors`; `UnderstatLoader.schedule_rows` retries strictly on network/transient connection errors (fails fast on logical/schema errors); test lock gate enforces canonical `reports/backtest/FROZEN_CONFIG_4b.json` (rejects arbitrary existing paths) | `bfa32e4` | `tests/test_backtest_infra.py`, `tests/test_ingest_review.py` |
| S7 | Fixed `backtest_hybrid.py`: alpha cross-fit uses model rho (not cfg.xi), properly indexes on filtered records, evaluates fitted alpha OOS on held-out seasons; kappa cross-fit evaluates OOS with week bootstrap CI95; added `--compare` mode and generated 4 committed comparison artifacts with per-match deltas and week-clustered bootstrap CI95; fixed validation sample size to 3,504 matches, 76 weeks, 2,000 resamples | `4e1b794` | `reports/backtest/compare_*.json` |
| S1 & S3 | `SPEC.md` §4 & `docs/decisions.md` (B6.1): clarified market matrix formulation — $\lambda_{\text{total}}$ from devigged O/U 2.5, home share from OLS on 1X2, rho from draw or fixed to 0.0 (model rho delta $-0.0006$ $[-0.0026, +0.0014]$); dropped overdetermined requirement; clarified $\kappa_{\text{high}}$ diagonal scope $\ge 2:2$. Documented honest cross-season transfer intervals for $\kappa_{\text{high}}$ ($2024 \to 2023: -0.0023$ $[-0.0044, -0.0002]$; $2023 \to 2024: -0.0003$ $[-0.0034, +0.0029]$) | `76fa32a` | `SPEC.md`, `docs/decisions.md` |
| Б1 | Recalculated 30-day TOA credit budget from measured 28d calls in `reports/recon/odds_budget.json`; showed all-tier `totals+h2h` overflows 450 hard stop (486.43 credits/30d); adopted default policy: T-48 OFF, T-24 ON (totals+h2h), T-3 ON by slot (totals+h2h), UCL /scores, Pinnacle correct_score <= 10/wk -> 392.14 ≈ 393 credits/30d (headroom 57.86); defined degradation ladder ((a) CS -> 5/wk, (b) disable T-3 h2h, (c) T-3 slot -> day, (d) T-3 never disabled); implemented `scripts/calc_odds_budget.py` and saved artifact `reports/recon/odds_budget_30d.json`; updated `core/config.py`, `config.yaml`, `tests/test_config.py`, `SPEC.md` §3, `docs/decisions.md` (B6.5), `PROMPTS.md` Phase 7 and checklist | `20bbe86` | `scripts/calc_odds_budget.py`, `reports/recon/odds_budget_30d.json`, `tests/test_config.py` |
| Б2 | Defined CLV arbiter formula: $\text{CLV}_{\text{fair}} = \text{odds}_{\text{fortuna}} \times p_{\text{fair}} - 1$, where $p_{\text{fair}}$ is from market matrix built from last snapshot of `totals+h2h` strictly BEFORE kickoff (not after); secondary benchmark against pre-kickoff devigged Pinnacle correct_score; documented `clv_source` tracking (`fair_market_matrix`, `pinnacle_correct_score`) for Phase 7; documented realized ROI as informational only (~12k bets needed for $\pm 5\%$ CI on ~9 odds); updated `SPEC.md` §7, `docs/decisions.md` (B6.7), `PROMPTS.md` Phase 7 and checklist | `f19925f` | `SPEC.md`, `docs/decisions.md`, `PROMPTS.md` |
| S2, S4, S5, S6 | S2: removed ranking by $|share_{model} - share_{1x2}|$; set candidate prefilter criteria (Fortuna grid, high draws mass, league rotation, budget limit <= 10/wk); discrepancy with model is strictly an anomaly flag without boosting bet priority. S4: removed "используется только для финального подтверждения" (test period exhausted, forward testing only); clarified exact gain of market matrix: $-0.016$ vs model share hybrid ($-0.0157$ $[-0.0205, -0.0113]$) and $-0.020 / -0.022$ vs pure model; clarified distinction between O/U logit mixture weight ($0.14-0.25$) and exact score goal shares ($\alpha = 0.0000$). S5: aligned UCL experimental scope across SPEC §1, PROMPTS Phase 11, and config.yaml. S6: restored lost v1 SPEC requirements (fallback model training strictly before forecast date with 2h result_lag; Hermes fallback REST-skill; Hermes scheduler notifications; dashboard "Base vs Shadow" page; link to docs/data-sources.md; Pinnacle correct_score benchmark; relative links in Changelog v2) | `23adeb6` | `SPEC.md`, `config.yaml`, `docs/decisions.md`, `PROMPTS.md` |

---

## 2. Headline Numbers (Part B, with 95% Confidence Intervals)

All calculations on validation set (seasons 2023, 2024; 3,504 matches, 76 calendar weeks). Bootstraps clustered on calendar weeks (2,000 resamples):

1. **Exact-Score Log Loss Gain**:
   - **Share from 1X2 vs Share from Model**: $\Delta \text{LL} = -0.0157$ $[-0.0205, -0.0113]$ (Reviewer benchmark: $-0.0158$ $[-0.0206, -0.0113]$; artifact: `compare_1x2_vs_model_share.json`).
   - **All from Market vs Share from Model**: $\Delta \text{LL} = -0.0163$ $[-0.0213, -0.0118]$ (Reviewer benchmark: $-0.0165$ $[-0.0214, -0.0119]$; artifact: `compare_all_market_vs_model_share.json`).
   - **Total Gain of Market Matrix over Pure Model**: $-0.0202$ (1X2 share) / $-0.0208$ (all from market) on the validation sample; $-0.022$ on the full validation pool.
2. **Model Share Cross-Fit Weight ($\alpha$)**:
   - $\alpha_{\text{opt}} = 0.0000$ in both season directions ($2023 \to 2024$ and $2024 \to 2023$). Out-of-sample evaluation on held-out seasons yields $\Delta \text{LL} = +0.0000$ CI95 $[0.0000, +0.0000]$. The statistical Dixon-Coles model adds zero value over the devigged market 1X2.
3. **Low-Score Correlation Parameter ($\rho$)**:
   - $\rho$ from model vs $\rho = 0$: $\Delta \text{LL} = -0.0006$ $[-0.0026, +0.0014]$ (Reviewer benchmark: $-0.0008$ $[-0.0027, +0.0012]$; artifact: `compare_rho_model_vs_rho0.json`). The difference is statistically indistinguishable from zero.
4. **Pinnacle Prematch Odds vs Consensus**:
   - Pinnacle 1X2 share vs Pinnacle model share: $\Delta \text{LL} = -0.0157$ $[-0.0204, -0.0113]$ (3,462 matches; artifact: `compare_pinnacle_1x2_vs_model_share.json`).
5. **O/U 2.5 CLV & Mixture**:
   - Fixed prematch selection ($p \cdot \text{price}_0 - 1 \ge 0.02$): 1,533 bets selected.
   - Line moved towards us in 47.4% of bets (48.5% excluding ties). Mean shift: $0.00$ percentage points.
   - Cross-season logit mixture: Out-of-sample log loss $0.6656$ vs pure market $0.6654$ (CI $[-0.0018, +0.0021]$). CLV is strictly neutral; O/U remains reference-only.
6. **High-Draw Correction ($\kappa_{\text{high}}$)**:
   - Cross-fitting confirms transferability: $2023 \to 2024$ gives $k_{\text{opt}} = 0.337$, eval delta $-0.0003$ $[-0.0034, +0.0029]$; $2024 \to 2023$ gives $k_{\text{opt}} = 0.179$, eval delta $-0.0023$ $[-0.0044, -0.0002]$. Fixed value $\kappa_{\text{high}} = 0.20$ brings all score cells $0:0..3:3$ within $|z| \le 2.0$.
7. **Credit Budget under `markets=totals,h2h` (30-day simulation, review B1)**:
   - Measured 28-day calls (`reports/recon/odds_budget.json`) across 6 sports (top-5 + UCL): T-48: 64, T-24: 68, T-3 (slots): 91 (= 239/30d at 1 credit/market).
   - Linear pricing: `markets * regions` (totals+h2h in EU = 2 credits/call). All tiers with totals+h2h: $(64+68+91) \times 2 \times 30/28 = 477.86$ + UCL `/scores` (8.57) = **486.43 ≈ 487 credits/30d**, overflowing 450 hard stop!
   - Adopted default policy (`scripts/calc_odds_budget.py` -> `reports/recon/odds_budget_30d.json`):
     - T-48: OFF (0 calls);
     - T-24: ON (`totals,h2h` = 2 credits) -> 145.71 credits/30d;
     - T-3 (by slots): ON (`totals,h2h` = 2 credits) -> 195.00 credits/30d;
     - UCL `/scores`: 8.57 credits/30d (≈ 9);
     - Pinnacle `correct_score` <= 10 matches/week: 42.86 credits/30d (≈ 43);
     - **Total**: **392.14 ≈ 393 credits/30d** (headroom 57.86 to 450 stop, 107.86 to 500 quota).
   - Degradation ladder:
     - (a) `cap_correct_score_5_week`: 370.71 ≈ 371 credits/30d;
     - (b) `disable_t3_h2h`: 273.21 ≈ 273 credits/30d (totals only at T-3, home share from T-24);
     - (c) `t3_slot_to_day`: 248.57 ≈ 249 credits/30d;
     - (d) `t3_never_disabled`: floor maintained. All timings use T-3 (Phase 0 decision), not T-2.

---

## 3. What Was Not Done and Why

1. **Branches not merged into `main` and not pushed to remote**:
   - Explicit autonomous executor rule: `git push`, merge to `main`, and rewriting history are strictly forbidden. All work is preserved on branch `phase-4b-hybrid` (which contains merged `phase-4-backtest`).
2. **Main database `valuebet.db` was not modified**:
   - Strict read-only rule for production data. Migrations and loads were executed and verified exclusively on `/tmp/bt4b.db`.
3. **No network calls executed**:
   - Network fetches for Understat, football-data, and The Odds API were avoided. All tests and verification scripts used existing local caches and DB copies.
4. **Test period (kickoff >= 2025-07-01) was not used for model/parameter tuning**:
   - All parameters ($\xi, w, \kappa, \alpha$) and comparative evaluations were performed strictly on the validation period (seasons 2023, 2024).

---

## 4. Commands for the Owner on the Main Database (Post-Merge)

Once the PR is merged into `main`, execute the following commands on the main database:

```bash
# 1. Apply Alembic migration b13589d81812 (adds home_price, draw_price, away_price to prematch_odds and closing_odds):
uv run alembic upgrade head

# 2. Backfill 1X2 odds and totals into prematch_odds and closing_odds (idempotent):
uv run python -m core.ingest backfill

# Verification: on the copy database, 21,017 prematch rows were updated and 5,543 written. A second run produces 0 writes and 0 updates.

# 3. Reload lineups for match 4215 using the new --lineups-only mode (bypasses schedule discovery):
uv run python -m core.ingest backfill --lineups-only --lineups-since 2026-09-05
```
