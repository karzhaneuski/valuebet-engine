# Phase 4 progress (backtest & calibration)

Continuation point: this file + `git log phase-4-backtest`. Rules for the
run: DB copy only (`sqlite3 valuebet.db ".backup /tmp/bt.db"` +
DATABASE_URL), zero paid TOA calls, no network fetches, test-period
metrics forbidden until FROZEN_CONFIG.json exists.

## Part 0 (branch phase-2-followup, NOT merged)
- 0.1+0.3 (commit b499dee): understat served match 30804 with every
  roster row duplicated (61 rows/32 players) -> IntegrityError on
  (match_id, understat_player_id); lineup_rows dedups (max minutes);
  failed_matches carry "Class: message"; lineups backfill offline
  (one reader per league-season, schedule pinned with force_cache),
  soccerdata logs at WARNING. Verified on a copy: match 4215 -> 32 rows,
  re-run idempotent. Owner reload command (do NOT run while the night
  backfill may be alive):
  `uv run python -m core.ingest backfill --lineups-since 2026-09-05`
- 0.2 (commit c47ba52): fd report always serializes quarantined +
  inverted_pairs; far-date rows journalled into date_mismatches only
  after inverted/quarantine resolution; 10559 is an inverted-pair record
  of the played PSG-Rennes 10363 (verified on a copy).

## Part 1 (this branch)
- 1.1 (commit d2a1cd3): core/backtest (periods + test lock, walk-forward
  runner with model cache, metrics) + tests (lock, leak, cache).
- 1.2 baseline (validation): 30.0s wall clock, 354 refit groups, 3504
  matches; log loss 2.9148; bins 0/1-5/6-15/>15 = 2.599/3.014/2.853/2.918;
  Brier O/U2.5 0.2393, BTTS 0.2447. Reports: part1_baseline.md +
  part1_reliability.png; records .cache/bt_records/baseline.json.
  Grid cost estimate: one config ~30s -> full 16-cell xi*w grid ~8 min.
- 2.1a xi*w grid (16 configs, reports/backtest/grid_xiw.jsonl): best
  xi=0.002 w=0.5 (LL 2.9112); top-6 within 0.004; w=1.0 clearly worst
  (2.9218-2.9399), bin 1-5 worsens with w monotonic.
- 2.1b prior_sigma (grid_sigma.jsonl): sigma 0.75 marginally best
  (2.9113 at w0.3 / 2.9114 at w0.5) - differences ~0.000x, essentially
  flat over 0.25-0.75.
- 2.1c options (grid_options.jsonl): base 2.9112 / xgscale 2.9115 /
  levelgoals 2.9157 / commonrho 2.9110 / decay0.5 2.9109 / decay0.25
  2.9107 - all within noise.
- 2.2 selection (part2_selection.json): paired week bootstrap -> every
  variant TIED with the best; chosen = SIMPLEST: xi=0.002, w=0.5,
  prior_sigma=0.5, NO options (LL 2.9112, bin1-5 3.0133).
- 2.3: part2_tuning.md + FROZEN_CONFIG.json committed (config_hash
  5090b95a..., commit at freeze recorded). TEST PERIOD UNLOCKED.

## Part 3 (TEST, one run, after freeze commit fc789a8)
- 3.1 test run /tmp/bt3.db: frozen LL 2.9157 vs baseline 2.9221 (n=2111)
  - tuning transferred (-0.0064); bins frozen 0/1-5/6-15/>15 =
  2.722/3.129/2.858/2.908 (baseline 1-5: 3.177). Brier O/U2.5 0.2419,
  BTTS 0.2455. part3_frozen_metrics.md + part3_reliability.png.
- 3.2 ROI vs AvgC/MaxC closings (part3_roi.json): AvgC -9..-11% CI<0;
  MaxC -4..-6% CI spans 0 -> NO edge, honestly negative.
- 3.3 score calibration (part3_score_calibration.json): top1 0.118/0.126,
  top3 0.311/0.332, {1:0,2:0,2:1} 0.239/0.248, 2-4 goals 0.608/0.614.
- 3.4 config.yaml -> xi 0.002, w 0.5; decisions.md entry added.

## Part A (phase-4 review fixes, 2026-10-26)
- merge main (phase-2-followup) -> 4ad68f7 (decisions.md conflict kept
  both entries chronologically).
- S2 (cc6e904): test contamination by phase 3 acknowledged in
  decisions.md; DATE lock ensure_window_allowed (>=2025-07-01 needs
  frozen config) in runner.predictions + model_walkforward; frozen_at =
  real freeze-commit time.
- S1: scripts/backtest_honesty.py on saved test records - market
  de-vig AvgC BEATS model on O/U 2.5 (Brier 0.2422 vs 0.2376, diff CI
  [+0.0023,+0.0067]; LL 0.6771 vs 0.6676); fair ROI -6.13%, random
  -5.7%, always-over -4.0%, always-under -9.2%; model -3.3..-5.0pp below
  fair (adverse selection: expected 0.518, market 0.444, actual 0.432).
  part3_test.md sections 3.2a/3.2b; forbidden phrases removed; CLV
  dropped from SUMMARY (zero by construction).

- S3 (f4bbaf6): quantile calibration + Wilson CIs + cell/total z-tables
  (test: all 0:0..3:3 cells |z|<=1.6; only total=6 off, z=-2.5).
- S4/S5 (cac5bc8, a0678dc): no-threshold-positive verdict recorded; MaxC
  unreachable upper bound (1104/3504 forks, 31.5%); AvgC margin 4.79%
  vs 6.53% -> no cross-period ROI comparisons.
- NIT: core.backtest under strict mypy (explicit flag set); records
  field log_p -> p_score (readers migrate old JSON); selection
  complexity is an explicit field computed from overrides (re-run:
  chosen unchanged opt_base_xi0.002_w0.5); bootstraps cluster on
  calendar weeks (kickoff ISO week in honesty, as_of week elsewhere).
- S1 review update & NIT fixes:
  - part3_test.md: removed misleading phrases ("consistent with an efficient
    closing market", "the calibrated probabilities are the product...",
    "slight conservative bias … fewer false 'value' signals"); fixed §3.2b
    table where fair ROI column duplicated model ROI (now correctly -0.0613);
    updated random side benchmark to expected value -6.6% (mean of always over/under)
    alongside single realization.
  - NIT: pyproject.toml strict override added for core.backtest.*; fixed 13
    type-arg annotations in core/backtest/metrics.py and runner.py; cleaned docstring
    in runner.predictions; re-exported ensure_window_allowed for strict import.

## Part B (phase-4b-hybrid)
- B1 (f42c8dc): prematch_odds table + loader; copy load 21,017 rows.
- B1 (1X2 load): 1X2 odds added to prematch_odds and closing_odds (home_price, draw_price, away_price) + migration b13589d81812; fd loader parses AvgH/D/A, PSH/D/A, MaxH/D/A, AvgCH/D/A, PSCH/D/A, MaxCH/D/A; idempotent copy load: 21,017 prematch updated, 5,543 written; rerun 0 writes / 0 updates.
- B1 (CLV fix) + S5: backtest_mixture.py selects bets at prematch price (p * price0 - 1 >= 0.02), measures shift prematch -> closing, ties separated. Validation: 1533 bets, line moved toward us in 47.4% (48.5% ex ties), mean shift 0.00 pp. S5 cross-validation across seasons: OOS LL 0.6656 vs market 0.6654 (CI [-0.0018, +0.0021]) -> O/U stays reference-only.
- B2/B3 + B1c: hybrid & market matrix in scripts/backtest_hybrid.py: --share {model, market_1x2} and --all-market. Validation: 1X2 share vs model share -0.0157 [-0.0205, -0.0113]; all-market -0.0163 [-0.0213, -0.0118]; alpha cross-fit = 0.0000; rho model vs rho=0 -0.0006 [-0.0026, +0.0014]; Pinnacle -0.0159. S3 kappa cross-fit by season (2023->2024: 0.337, 2024->2023: 0.179).
- S2 lock: backtest_hybrid.py and backtest_mixture.py require FROZEN_CONFIG_4b.json under --period test; absent path raises TestPeriodLockedError.
- B5/B6 decisions: decisions.md B3/B4/B5/B6 updated: market exact-score matrix (O/U 2.5 + 1X2 + kappa_high) as base; model is fallback and anomaly detector; value against Fortuna; candidate prefilter on divergence; credit budget with h2h ~188/30d; 1X2 input-only; CLV arbiter for phase 7.

## Part C (Ingest CLI & Robustness)
- C1: CLI backfill added `--lineups-only` (requires `--lineups-since`), skipping Understat schedule discovery and football-data downloads/parsing entirely; unit tests verify validation error when `--lineups-since` is missing and ensure schedule/fd loaders are not called.
- C2: Understat schedule fetching in UnderstatLoader.schedule_rows now uses retry loop with exponential backoff on transient errors (e.g. TLS client timeouts) and provides clear human-readable error messages; cmd_backfill catches league-season schedule errors, records them in report['understat_errors'], and continues backfill for subsequent leagues; unit tests cover transient retry, exhausted retry, and backfill error recording without network calls.
- C3: Quieted soccerdata, TLSLibrary, TLSRequests, and urllib3 loggers to WARNING across all CLI entry points and UnderstatLoader, and set SOCCERDATA_LOGLEVEL=WARNING before imports to silence team/league dictionary announcements and disk caching info messages; unit tests verify log levels and environment configuration.

## Part D (SPEC v2 & PROMPTS.md)
- SPEC.md updated to v2: added top Changelog v2 referencing decisions B3-B6; target defined as fair market matrix of exact score with kappa_high=0.20 and edge against Fortuna odds; Dixon-Coles model assigned to fallback and anomaly detection; exact score and multiscore are recommendations, totals and BTTS are reference-only, 1X2 is strictly input-only; TOA sport-level quota budget documented (~188 credits/30d); CLV defined as Phase 7 quality arbiter.
- PROMPTS.md updated: task descriptions and review checklists for Phases 5, 6, and 7 updated to align with SPEC v2 (fair market matrix, Fortuna manual odds entry, edge calculation, anomaly detection, CLV tracking).
