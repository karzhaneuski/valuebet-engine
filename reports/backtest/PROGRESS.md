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
