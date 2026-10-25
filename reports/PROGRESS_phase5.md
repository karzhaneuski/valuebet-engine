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

---
## Next: Step 4 (core/value/engine.py - Value Engine)



