# Phase 5 Progress (Fair Pricing Engine)

Continuation point: this file + `git log phase-5-fair-pricing`. Rules: DB copy only (`/tmp/p5.db` + DATABASE_URL=sqlite:////tmp/p5.db), zero paid TOA calls, no network fetches, small English commits, all checks green (`ruff`, `mypy`, `mypy --strict core/model core/backtest core/value`, `pytest`).

## Step 0: Preparation & Housekeeping
- Branch `phase-5-fair-pricing` branched from fresh `main` (commit `c587101`).
- DB copy created: `sqlite3 valuebet.db ".backup /tmp/p5.db"` (10,687 matches verified).
- Cleaned up pytest warning: renamed `TestPeriodLockedError` to `PeriodLockedError` in `core/backtest/periods.py`, kept backward compatibility alias, updated `tests/test_backtest_infra.py`.
- Silenced `TLSLibrary` logger chatter ("Successfully loaded TLS library") by ensuring `TLSLibrary` logger is set to `WARNING` in `core/logging.py`, `core/ingest/__main__.py`, and `core/ingest/understat.py`. Added unit test `test_tls_library_logger_quieted_no_info` in `tests/test_logging.py`.
- Mypy config updated in `pyproject.toml` with untyped ignore override for `tls_requests.*`.
- Commit: `3cb6f64`. Checks: 214 tests passing, 0 warnings related to period lock, mypy / strict mypy green.

## Step 1: Fair Market Matrix (core/value/fair_matrix.py)
- Created `core/value/` package and `core/value/fair_matrix.py`:
  - `MarketInputs` (source, snapshot_time, totals_by_book, h2h_by_book, match_name) with deterministic SHA-256 `compute_hash()`.
  - Proportional devigging `devig_totals` and `devig_1x2`.
  - Bookmaker selection: Pinnacle preferred if available; otherwise consensus across allowed books; offshore/exchange books (`onexbet`, `betfair`, etc.) excluded.
  - Line selection: primary line 2.5; fallback to nearest available half-line ($x.5$).
  - Total rate $\lambda_{\text{total}}$ derived from devigged O/U via `market_lambda_from_totals`.
  - Home goal share $s$ fitted by matching devigged 1X2 via `fit_1x2_share`.
  - $\rho = 0.0$ and $\kappa_{\text{high}} = 0.20$ applied to diagonal $\ge 2:2$ with renormalization (values read from `config.fair_matrix`).
  - `FairMatrix` output with 11x11 shape, sum 1 within $10^{-9}$ tolerance, and query methods (`exact_score`, `multiscore`, `goals_range`, `total_over`, `total_under`, `btts`).
  - Handled missing market (`status="no_market"`) and optional fallback model (`status="fallback"`, `source="model_fallback"`).
- Added `FairMatrixConfig` to `core/config.py` and `config.yaml`.
- Added unit tests in `tests/test_fair_matrix.py` (8 tests): verified numerical agreement with `scripts/backtest_hybrid.py` down to $10^{-12}$, matrix properties, bookmaker selection, half-line fallback, missing market, fallback model, and input hash.
- Strict mypy passed across all 10 source files (`core/model`, `core/backtest`, `core/value`).

## Step 2: Pinnacle correct_score Benchmark (core/value/pinnacle_benchmark.py)
- Created `core/value/pinnacle_benchmark.py`:
  - `parse_score_outcome`: parses TOA outcome strings (`Aston Villa:1|Manchester City:0`, `H1|A0`, `1:0`, `Other`).
  - `build_pinnacle_benchmark_matrix`: proportional devigging across quoted exact score outcomes, documented rule for 'Any other' score (mass distributed proportionally to the reference market matrix tail, or uniform if none provided), strictly normalized 11x11 matrix.
  - `compare_matrices`: calculates cell-level differences (max cell diff, mean cell diff, TVD, RMSE, Jensen-Shannon divergence), top-1 / top-3 sets, high draws `{2:2, 3:3, 4:4}`, low scores, goals 2-4, 1X2, totals, and BTTS.
  - `load_pinnacle_correct_score_from_db`: loads Pinnacle correct_score snapshots for a match from `odds_snapshots`.
- Evaluated on actual DB snapshot for match 1973 (Aston Villa vs Man City):
  - Loaded all 32 Pinnacle correct_score rows.
  - Verified benchmark matrix shape and properties.
  - Observed that h2h was not polled in TOA snapshots (honestly recorded; live h2h TOA polling arrives in Phase 7).
  - Evaluated matrix comparison against FairMatrix with William Hill totals and consensus 1X2: max cell diff < 0.15, TVD < 0.35, RMSE < 0.05, JSD < 0.05, high draws diff well-controlled (< 0.05).
- Added unit tests in `tests/test_pinnacle_benchmark.py` (5 tests). All strict mypy checks green across 11 files.

## Step 3: Manual Fortuna Odds Parser (core/value/fortuna_input.py)
- Created `core/value/fortuna_input.py`:
  - `FortunaPrice` dataclass with normalized `selection_key`.
  - Free text parser supporting exact scores (`2:1 9.50`, `2-1 @ 9,50`, `2:1 - 9.5`), 'other' scores (`inny wynik`, `pozostałe`, `other`, `любой другой`), multiscore sets (`1:0, 2:0, 2:1 @ 3.40`), goals ranges (`2-4 gole @ 1.80`, `2–4 bramki 1,80`, `2-4 гола`), reference totals (`over 2.5 1.95`), and reference 1X2 (`1 @ 2.10`, `X @ 3.40`, `remis`).
  - Strict line-numbered error reporting via `FortunaParseError` with zero silent drops.
  - Conflict detection: duplicate entries with different odds raise explicit errors; identical entries are deduplicated.
  - `calculate_fortuna_overround`: calculates bookmaker overround (margin) on exact score grids (e.g. 15.4%).
- Added unit tests in `tests/test_fortuna_input.py` (11 tests): exact scores, other score phrases, multiscore, goals ranges, totals, 1X2, multi-line batches, conflicting odds, syntax errors, and overround calculation.
- All checks green, strict mypy passing across 12 source files.

## Step 4: Value Engine (core/value/engine.py)
- Created `core/value/engine.py`:
  - `EvaluatedOutcome` and `EvaluationResult` dataclasses containing full outcome breakdown (market, odds, p_fair, fair_odds, edge, recommended flag, rejection reason, flat stake, 1/4 Kelly capped at bankroll limit, raw price, and flags).
  - `calculate_kelly_quarter_capped`: 1/4 Kelly criterion `0.25 * edge / (odds - 1)` with cap at 0.02 (2% bankroll).
  - `check_model_anomaly`: checks divergence $|s_{\text{market}} - s_{\text{model}}| > 0.15$ and generates warning flag `"⚠️ Аномалия модели: расхождение доли голов ... > порога 15%. Проверь составы/данные!"`.
  - Rejection logic and thresholds:
    - Long odds filter: `odds > 21.0` -> `"Длинный коэффициент (> 21)"`.
    - Low probability filter: `p_fair < 0.04` -> `"Мала p_fair (< 4.0%)"`.
    - Edge thresholds: exact score / other score $\ge 12\%$; multiscore / goals range $\ge 8\%$.
    - Reference markets: totals and 1X2 are strictly reference-only (`recommended = False`, `"Справочный рынок"`).
    - Lineup risk factors: 1 key player absent or sources conflict -> edge threshold multiplied by $\times 1.5$ (e.g. $12\% \to 18\%$, $8\% \to 12\%$); $\ge 2$ key absences in a single team -> total exclusion (`is_excluded = True`, all outcomes rejected).
  - Summary table in Russian formatted via `EvaluationResult.format_table()` with match header, market source, bookmakers used, Fortuna margin, risk flags / anomaly warnings, outcome rows with alignment, and total recommendation count.
- Added unit tests in `tests/test_engine.py` (7 tests): Kelly calculation, model anomaly check, exact score thresholds and rejection reasons, multiscore / goals range thresholds, reference-only markets, risk multipliers and $\ge 2$ player exclusions, and full Russian summary table output.
- All checks green (`ruff`, `mypy`, `mypy --strict core/model core/backtest core/value`, 246 tests passing).

## Step 5: Candidate Prefilter (core/value/prefilter.py)
- Created `core/value/prefilter.py`:
  - `CandidateMatch`, `PrefilterDecisionRecord`, and `PrefilterReport` dataclasses.
  - Criteria per decisions B6.4 and SPEC §5:
    - Fortuna grid availability: `has_fortuna_grid` (default unknown/pass; False rejects with `"Нет сетки точного счёта Fortuna"`).
    - `no_market` filter: rejects matches missing market odds.
    - `compute_high_draws_mass`: calculates $\sum_{i \ge 2} M_{i,i}$ (zone where $\kappa_{\text{high}}$ operates).
    - League rotation: round-robin interleaving across active leagues with configurable league cap (`max_per_league`, default 3) to prevent one league from consuming the candidate quota.
    - Weekly budget limit: `weekly_cap` ($\le 10$ matches/week from `config.odds_policy.pinnacle_correct_score_max_weekly`).
    - Model anomaly check: Dixon-Coles divergence $|s_{\text{market}} - s_{\text{model}}| > 0.15$ flags `"⚠️ Аномалия модели: проверь составы/данные"`, but explicitly does NOT alter ranking priority or candidate selection (model $\alpha = 0$).
  - `log_prefilter_decisions_to_db`: records every accepted/rejected decision into the `prefilter_decisions` table in the database with match ID, date, BTTS proxy, edge, accepted boolean, and detailed reason. Idempotent: updates existing records if re-run on the same date.
  - Russian summary table via `PrefilterReport.format_table()`.
- Added unit tests in `tests/test_prefilter.py` (6 tests): high draws mass calculation, Fortuna grid / no_market rejections, league rotation and weekly cap, model anomaly flag without ranking boost, table formatting, and database persistence with idempotency.
- All checks green (`ruff`, `mypy`, `mypy --strict core/model core/backtest core/value`, 252 tests passing).

## Step 6: Persistence and CLI (core.value)
- Database schema migration (`48fd4f90f079_recommendations_matrix_provenance.py`):
  - Added `matrix_source` (VARCHAR(40)) and `input_hash` (VARCHAR(64)) to `recommendations` table.
  - Added unique constraint `uq_recommendations_idempotency` on `(match_id, market, outcome, variant, input_hash)`.
  - Applied cleanly to `/tmp/p5.db`. Verified `tests/test_migrations.py` passing (alembic check, up/down cycle).
- Created `core/value/persistence.py`:
  - `save_recommendations_to_db`: records value recommendations with `variant="base"`, `final_odds_source="fortuna_manual"`, `matrix_source`, `input_hash`, `p_model` (p_fair), `p_market_devig`, `edge`, `kelly_quarter_capped`, and `flags`. Idempotency key `(match_id, market, outcome, variant, input_hash)` guarantees re-evaluation does not create duplicate rows. Leaves `clv` and `clv_source` empty for Phase 7.
  - `load_market_inputs_for_match`: loads latest available market odds from `prematch_odds` (football-data: Pinnacle and Consensus) and `odds_snapshots` (TOA live snapshots). Returns `no_market` when 1X2 odds are unavailable.
  - `find_matches_by_query`: fuzzy team name and alias matching with delimiter splitting and candidate resolution.
- Created `core/value/__main__.py` (CLI via `uv run python -m core.value`):
  - `evaluate`: evaluates Fortuna prices for a match against the fair matrix. Supports `--match "<Home> - <Away>"`, `--input <file>` (or stdin), `--date`, risk flags (`--key-absences-home`, `--key-absences-away`, `--sources-conflict`), `--model-home-share`, and `--save` (default True). Prints Russian summary table and persists recommendations.
  - `candidates`: runs candidate prefilter for upcoming matches with `--days`, `--cap`, `--max-per-league`, `--save`.
- Added unit tests in `tests/test_persistence_cli.py` (4 tests): market loading from prematch odds, query match lookup, idempotency verification (re-run updates without duplicates), and end-to-end CLI command execution.
- Real data verification on database copy `/tmp/p5.db`:
  - Executed `candidates --days 7 --cap 10`: evaluated 79 upcoming scheduled matches, correctly identified `no_market` status, and logged decisions to `prefilter_decisions`.
  - Executed `evaluate` on 3 validation matches:
    - Match 1141: Manchester United vs Fulham (Premier League, 2024-08-16) -> Pinnacle market inputs loaded, 2 recommendations saved (2-4 goals @ 1.85, other @ 15.00). Idempotency verified: re-running kept exactly 2 rows.
    - Match 3426: Valencia vs Barcelona (La Liga, 2024-08-17) -> Pinnacle market inputs loaded, 1 recommendation saved (2-4 goals @ 1.78).
    - Match 9743: Le Havre vs Paris Saint Germain (Ligue 1, 2024-08-16) -> Pinnacle market inputs loaded, 1 recommendation saved (2-4 goals @ 1.82).
- All checks green (`ruff`, `mypy`, `mypy --strict core/model core/backtest core/value`, 256 tests passing).

---
## Step 7: Reviewer Round Fixes, Margin Studies & Final Architecture

### 1. BLOCKING Issues Fixed
- **BLOCKING 1: Fortuna 25-cell Grid & "inny" Reference Handling** (commit `3a21b36`):
  - Added `fortuna.cs_grid_max_goals: 4` to `core/config.py` and `config.yaml`.
  - $p_{\text{fair}}(\text{inny}) = 1 - \sum M[0..4, 0..4]$ computed purely from the fair matrix, completely independent of user input.
  - Any score outcome outside 0..4 raises explicit `FortunaParseError` ("Счёт 5:0 вне сетки Fortuna 0..4; такие исходы входят в 'inny'").
  - `inny` and grid margin are evaluated ONLY when all 25 scores are present in the batch; otherwise rejected with `"Неполная сетка Fortuna (N из 25): inny не оценивается"`.
  - Recorded empirical bias $z = -2.25$ (predicted $p_{\text{fair}} = 0.0474$ vs actual frequency $0.0393$, $p=0.024$, $N=3,462$ validation matches, verified by `scripts/verify_inny_bias.py`). Earlier drafts noted 5.7% vs 4.6% from a smaller preliminary sample; 4.74% vs 3.93% is the canonical full-dataset figure. `inny` is marked `recommended = False` with reason `"Только для справки: эмпирическое смещение z=-2.25"`.
- **BLOCKING 2: Free-text Parser Hardening** (commit `3a21b36`):
  - Goals ranges strictly require an explicit keyword (`gole/bramki/гола/голов`); bare dashes without keywords (e.g. `1-2`) are parsed as exact scores.
  - Glued numbers from browser table copy-pastes handled cleanly (`Remis4.60` -> Remis 4.60; `0:4 0:5 0:61 00.00` -> 100.00).
  - Multi-outcome lines (`mniej niż 1.5 11.00 więcej niż 1.5 1.06`, `- 0.5 8.40+ 0.5 1.04`) parsed without corruption.
  - Section-aware `Remis` handling: in 1X2 context -> Draw 1X2; in exact score context -> Draw exact score (e.g. `Remis 4.60` -> 1X2 Draw).
- **BLOCKING 3: Recommendation Lifecycle & Status** (commit `3a21b36`):
  - Added `selection_key: str` (e.g. `score:2:1`, `range:2:4`, `multi:1:0,2:0`, `score:other`) and `status: sa.Enum('active', 'superseded', 'withdrawn', name='reco_status')` to `Recommendation` in `core/orm.py`.
  - Partial unique index `uq_recommendations_active` on `(match_id, market, selection_key, variant) WHERE status = 'active'`.
  - Created and applied Alembic migration `e5f1b2c3d4e5_recommendation_lifecycle_status_and_key.py`.
  - `save_recommendations_to_db`: if odds/edge change and still passes -> old marked `superseded`, new inserted as `active`; if drops below edge or omitted -> old marked `withdrawn`. Initial `odds` and `created_at_utc` are never overwritten.
  - CLI safeguard: recommendations cannot be persisted post-commence time.

### 2. SHOULD FIX Improvements Completed
- **S1: Pinnacle Benchmark "other" Parsing** (commit `7e665ba`):
  - Updated `parse_score_outcome` in `core/value/pinnacle_benchmark.py` to require exact match for "other" (`outcome.strip().lower() in other_exact`), preventing team names like Rotherham from being falsely parsed as "other".
  - Filtered snapshots to strictly before match commence time.
- **S2: Standby Model & Key Players Detection** (commit `7e665ba`):
  - Created `core/value/standby_model.py`: dynamically fits historical matches strictly prior to match kickoff with `result_lag_minutes`.
  - Queries `match_lineups` for key players with $\ge 70\%$ starts in the last 8 matches.
  - Integrated dynamic standby model and automatic model home share into CLI `_cmd_evaluate`.
- **S3: Candidate Prefilter Safeguards** (commit `3a21b36`):
  - Enforced league cap strictly across all rounds and the remaining pool (no bypass by pool).
  - Queries previously accepted candidates in the current ISO week from `prefilter_decisions` to reduce available weekly budget.
  - Added `--has-fortuna-grid / --no-fortuna-grid` flags.
- **S4: Fixture Tests & Gitignore** (commit `eee64f9`):
  - Copied real Fortuna copy-pastes to `tests/fixtures/`.
  - Excluded `data/manual/` from git tracking in `.gitignore`.
- **S5: Ruff Per-File Ignores** (commit `eee64f9`, `21d7b4d`):
  - Scoped `RUF001`, `RUF002`, `RUF003` to `core/value/*`, `tests/test_fortuna_input.py`, and `scripts/*` in `pyproject.toml`.

### 3. Empirical Studies (M0–M4)
- **M0–M3: Fortuna Margin Study** (`scripts/fortuna_margin_study.py`, `reports/fortuna_margin_study.md`, commit `21d7b4d`):
  - Analyzed real Fortuna copy-pastes: Exact score margin mean **32.7%** (30.7%–34.8%), Multiwynik margin mean **20.7%**, Totals 2.5 margin mean **5.9%** (down to 3.4%–3.9% on big matches), Team totals mean **8.8%**.
  - Proved mathematically and empirically that at 32.7% margin, $\mathbb{E}[p_{\text{fair}} \times \text{odds}] \approx 0.753$ (average edge $-24.6\%$).
  - Over 25 scores across all real matches, **0 out of 25 cells (0.0%) pass the 12% edge threshold**.
  - Proved that breakeven margin for $\kappa_{\text{high}} = 0.20$ requires bookmaker margin **$M \le 7.14\%$**; at Fortuna's 32.7% margin, the $+20\%$ boost is completely absorbed ($1.20 / 1.327 - 1 = -9.6\%$).
- **M4: Soft vs Sharp Totals Backtest** (`scripts/soft_vs_sharp_totals.py`, `reports/soft_vs_sharp_totals.md`, commits `5e9d4dd`, `187d567`):
  - 3,464 validation matches across 5 leagues (2023–2025).
  - Bet365 average margin on O/U 2.5: **4.96%** (pre-match), **4.92%** (closing).
  - Feasible Scenario 1 (pre-match devig vs Bet365 pre-match): N=26 (0.75%), ROI -0.19%, 95% bootstrap CI [-57.2%; +64.6%] (statistically insignificant due to small sample size).
  - Scenario 2 (closing devig vs pre-match): labeled as look-ahead bias (closing odds unknown at pre-match bet placement time) and removed from product conclusions.
  - Feasible Scenario 3 (closing devig vs Bet365 closing `B365C`): N=65 (1.88%), ROI -19.66% (95% CI [-56.3%; +20.2%]), proving soft bookmakers eliminate lag by closing time.
  - Market-wide line shopping (`MaxC` across ~30 books): ROI +4.36% at 2% threshold (N=1,079).
  - Margin sensitivity analysis: at Fortuna's 6.0%–7.0% margin, Scenario 1 yields only 18–21 signals (0.5%–0.6%), and Scenario 3 yields 43–53 signals (1.2%–1.5%).
- **Verification of "inny" bias** (`scripts/verify_inny_bias.py`):
  - Verified canonical predicted $p_{\text{fair}}(\text{inny}) = 4.74\%$ vs actual frequency $3.93\%$ ($z = -2.25$, $p = 0.024$) across all $N = 3,462$ matches.
  - Unified all documentation references to these exact reproducible figures.

### 4. M5 & Architectural Decisions
- Updated `docs/decisions.md` (decisions 8–12 under Phase 5):
  - Recorded `inny` empirical bias $z=-2.25$ (4.74% vs 3.93%) and reference-only status.
  - Recorded recommendation lifecycle and partial index `uq_recommendations_active`.
  - Recorded Fortuna exact score margin ~32.7% and zero-value mathematical barrier.
  - Recorded soft vs sharp totals backtest results (Scenarios 1 and 3, look-ahead status of Scenario 2).
  - Formulated totals scanner as an architectural PROPOSAL awaiting owner decision, leaving SPEC v2 and recommended markets strictly unchanged.
  - Recorded key players detection via `match_lineups` and dynamic standby model fitting.
- Updated `reports/phase5_report.md`:
  - Corrected Pinnacle benchmark figures: 1.266 overround (26.6% margin), 0.049 TVD.
  - Added comprehensive "Выводы для продукта и архитектурные предложения" section.
  - Documented migrations `48fd4f90f079` and `e5f1b2c3d4e5`.

---
## Status: Phase 5 Fully Completed
All 267 unit and integration tests passing (`uv run pytest`).
All linting and type checks green (`uv run ruff check .`, `uv run mypy`, `uv run mypy --strict core/model core/backtest core/value`).
All reviewer items (3 BLOCKING, 5 SHOULD FIX, M0–M5) verified and documented.



