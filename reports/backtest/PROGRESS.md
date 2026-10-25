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
- 2.1c options (grid_options.jsonl): [RUNNING] xgscale / levelgoals /
  commonrho / decay on xi0.002 w0.5.
- Selection (2.2): scripts/backtest_select.py - paired week-clustered
  bootstrap of dLL; ties -> simpler config.
