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

---
## Next: Step 2 (core/value/pinnacle_benchmark.py - Pinnacle correct_score benchmark)

