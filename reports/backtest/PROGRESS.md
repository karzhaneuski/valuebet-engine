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
- [IN PROGRESS] core/backtest module.
