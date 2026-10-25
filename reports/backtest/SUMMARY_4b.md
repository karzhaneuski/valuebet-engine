# Phase 4 review (part A) + phase 4b hybrid (part B) — summary

Branches NOT merged: `phase-4-backtest` (part A, on top of the phase-4
work), `phase-4b-hybrid` (part B, off phase-4-backtest). valuebet.db and
data/raw untouched (all runs on copies /tmp/bt3.db, /tmp/bt4b.db);
paid TOA calls 0; no network fetches. The single second-look test run
happened AFTER FROZEN_CONFIG_4b.json was committed.

## Item -> done -> commit -> verification

### Part A (branch phase-4-backtest)

| item | what was done | commit | verified by |
|---|---|---|---|
| merge | main (phase-2-followup) merged; decisions.md conflict kept both entries chronologically | 4ad68f7 | 201 tests green |
| S1 | model vs de-vigged AvgC on the same 2096 test matches: Brier 0.2422 vs 0.2376 (diff CI95 +0.0023..+0.0067), binary LL 0.6771 vs 0.6676; fair ROI -m/(1+m) = -6.13%; random side -5.7%, always over -4.0%, always under -9.2%; model 3.3-5.0 pp below fair (adverse selection: expected 0.518, market 0.444, actual 0.432); side/league ROI tables; forbidden phrases removed; CLV dropped from SUMMARY (zero by construction) | ef12de1 | reports/backtest/part3_honesty.json + part3_test.md 3.2a/3.2b |
| S2 | phase-3 test contamination acknowledged (decisions.md); DATE lock ensure_window_allowed (window ending >= 2025-07-01 needs FROZEN_CONFIG.json) wired into runner.predictions and model_walkforward.py + test; frozen_at = real freeze-commit timestamp | cc6e904 | tests/test_backtest_infra.py::test_date_lock_refuses_windows_reaching_into_test; FROZEN_CONFIG.json |
| S3 | calibration rewritten: quantile bins + Wilson CIs (PNG), 0:0..3:3 cell and 0..7+ total z-tables. Test: all cells \|z\|<=1.6; only total=6 off (0.046 vs 0.035, z -2.5) | f4bbaf6 | reports/backtest/part3_score_calibration.json + part3_calibration_quantile.png |
| S4/S5 | no O/U threshold is positive at executable prices -> phase 5 emits no raw-model O/U signals; MaxC = unreachable upper bound (1104/3504 = 31.5% riskless forks on validation); AvgC margin 4.79% vs 6.53% -> no cross-period ROI comparisons | cac5bc8, a0678dc | part3_margin.json; decisions.md entries |
| NIT | calendar-week bootstrap clusters; core.backtest under strict mypy; records field p_score (readers migrate); complexity as an explicit override-derived field (selection re-run: chosen unchanged) | b0d07ff | pyproject override; backtest_select re-run |

### Part B (branch phase-4b-hybrid)

| item | what was done | commit | verified by |
|---|---|---|---|
| B1 (prematch table) | prematch_odds table (own table over a kind flag: different bookmaker set, closing CHECK constraint, CLV-by-bookmaker code) + migration a86d9da4539e + fd loader (Avg/P/Max columns) + idempotent upsert; copy load: 21,017 rows (Avg 7427 / Max 7427 / P 6163), rerun 0 updates | f42c8dc | tests/test_football_data.py::test_prematch_odds_parsed_and_stored; run on /tmp/bt4b.db |
| B1 (1X2 load) | 1X2 odds added to prematch_odds and closing_odds (home_price, draw_price, away_price) + migration b13589d81812; fd loader parses AvgH/D/A, PSH/D/A, MaxH/D/A, AvgCH/D/A, PSCH/D/A, MaxCH/D/A; idempotent copy load: 21,017 prematch updated, 5,543 written; rerun 0 writes / 0 updates | (this round) | tests/test_football_data.py; run on /tmp/bt4b.db |
| B1 (CLV fix) + S5 | scripts/backtest_mixture.py selects bets at prematch price (p * price0 - 1 >= 0.02), measures shift prematch -> closing, ties separated. Validation: 1533 bets, line moved toward us in 47.4% (48.5% ex ties), mean shift 0.00 pp. S5 cross-validation across seasons: OOS LL 0.6656 vs market 0.6654 (CI [-0.0018, +0.0021]) -> O/U stays reference-only | (this round) | reports/backtest/4b_mixture.json |
| B2/B3 + B1c | hybrid & market matrix in scripts/backtest_hybrid.py: --share {model, market_1x2} and --all-market. Validation: 1X2 share vs model share -0.0157 [-0.0205, -0.0113]; all-market -0.0163 [-0.0213, -0.0118]; alpha cross-fit = 0.0000; rho model vs rho=0 -0.0006 [-0.0026, +0.0014]; Pinnacle -0.0159. S3 kappa cross-fit by season (2023->2024: 0.337, 2024->2023: 0.179) | (this round) | reports/backtest/4b_hybrid_market_1x2.json, 4b_hybrid_all_market.json |
| S2 lock | backtest_hybrid.py and backtest_mixture.py require FROZEN_CONFIG_4b.json under --period test; absent path raises TestPeriodLockedError | (this round) | tests/test_backtest_infra.py::test_phase4b_scripts_test_lock |
| B5/B6 decisions | decisions.md B3/B4/B5/B6 updated: market exact-score matrix (O/U 2.5 + 1X2 + kappa_high) as base; model is fallback and anomaly detector; value against Fortuna; candidate prefilter on divergence; credit budget with h2h ~188/30d; 1X2 input-only; CLV arbiter for phase 7 | (this round) | docs/decisions.md |

## Headline numbers

- Market matrix beats pure model and model-share hybrid on validation exact-score log loss:
  share from 1X2 vs share from model: -0.0157 [-0.0205, -0.0113];
  all from market vs share from model: -0.0163 [-0.0213, -0.0118];
  total gain of market matrix over pure model: -0.0202 (1X2 share) / -0.0208 (all-market).
- Model share weight alpha in cross-fitting is exactly 0.0000 in both season directions:
  the statistical model adds no value over the devigged market 1X2.
- Rho parameter: rho from model vs rho=0 gives delta -0.0006 [-0.0026, +0.0014] (tied).
- O/U 2.5 CLV & mixture: CLV is neutral (47.4% moved towards us, 48.5% ex ties, mean shift 0.00 pp);
  mixture out-of-sample log loss (0.6656) does not beat pure market (0.6654).
  O/U stays strictly reference-only.
- Draw fix: kappa_high=0.20 brings all cells to |z|<=2; cross-fitting confirms k_opt in [0.18, 0.34]
  transfers across seasons. Justified a priori by bivariate Poisson underestimation of high draws.
- Second-look test caveats: top-1 (0.119 vs 0.131) and top-3 (0.315 vs 0.329) are underpredicted,
  meaning tails are overpredicted; risk of false value on long odds. Test period is considered exhausted.

## Not done

- Branches not merged (owner's call); part-A fixes live on
  phase-4-backtest, part B on phase-4b-hybrid.
- The prematch load command for the MAIN database is NOT executed
  (read-only rule) - see below.
- Cell tables were not re-run for the mixture (it equals the market;
  nothing new to tabulate).

## Owner commands (after checking nothing else writes the DB)

    # 1. migration to add 1X2 columns + prematch & closing odds load (idempotent):
    uv run alembic upgrade head
    uv run python -m core.ingest backfill          # fd section writes prematch & closing 1X2 and totals

21,017 existing prematch rows are updated with 1X2 prices, and 5,543 prematch rows written (measured on copy);
a rerun performs 0 writes and 0 updates.

    # 2. reload match 4215 via --lineups-only:
    uv run python -m core.ingest backfill --lineups-only --lineups-since 2026-09-05
