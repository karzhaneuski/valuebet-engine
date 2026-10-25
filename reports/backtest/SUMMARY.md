# Night run summary (2026-10-25): phase-2 follow-up + phase 4 backtest

Branches NOT merged: `phase-2-followup` (part 0), `phase-4-backtest`
(parts 1-3). valuebet.db / data/raw untouched (all runs on copies under
/tmp via DATABASE_URL); paid TOA calls: 0; no network fetches.

## Item -> done -> commit -> verification

| item | what was done | commit | verified by |
|---|---|---|---|
| 0.1 lineups 4215 | understat served match 30804 with every roster row duplicated (61 rows / 32 players) violating the (match, understat_pid) unique key; lineup_rows dedups (max minutes); failed_matches carry "Class: message" | b499dee (phase-2-followup) | offline rerun on a copy: 32 rows written, re-run idempotent; tests test_lineup_rows_deduplicates_understat_glitch, test_failed_matches_carry_error_text |
| 0.2 fd report | quarantined/inverted_pairs always serialized; far-date rows journalled into date_mismatches only after inverted/quarantine resolution; 10559 reclassified as inverted-pair of played PSG-Rennes 10363 | c47ba52 (phase-2-followup) | run on a copy: date_mismatches [], inverted_pairs 1 (fragment in decisions.md); tests test_far_date_inverted_row_classified_once, test_far_date_without_stats_is_quarantined_only |
| 0.3 soccerdata | loggers at WARNING; one reader per league-season with the schedule frame pinned via force_cache (soccerdata bypasses the disk cache for the current season) - intertwined with 0.1, same commit | b499dee | test_one_reader_and_one_schedule_read_per_league_season (1 factory + 1 schedule read for 3 matches) |
| 1.1 core/backtest | periods + test lock (file gate), weekly walk-forward, model cache keyed by (league, as_of, config_hash), metrics (LL, bins, Brier, reliability) | d2a1cd3 | tests: lock refuses/unlocks, leak test (week's own results never train it), cache invalidates on config change |
| 1.2 baseline | validation walk-forward 3504 matches in 30s; LL 2.9148 | 04eb59d | reports/backtest/part1_baseline.md + part1_reliability.png |
| 2.1a-c grids | xi*w (16), prior_sigma (9), model options implemented as ModelConfig flags with tests (xG goals scaling, goals-only level, common rho, slower league decay via fixed_params two-pass) | ef31d0f, 4575d5e | grid_*.jsonl; option tests in tests/test_dixon_coles.py incl. FD check of the two-level gradient |
| 2.2 selection | paired week-clustered bootstrap; all variants TIED -> simplest chosen | 4575d5e | part2_selection.json |
| 2.3 freeze | part2_tuning.md + FROZEN_CONFIG.json committed BEFORE any test scoring | fc789a8, c7e0e51 | git log order; lock test now uses its own absent path |
| 3.1 test metrics | frozen LL 2.9157 vs baseline 2.9221 (n=2111) | 495d159 | part3_test.md, part3_frozen_metrics.md, part3_reliability.png |
| 3.2 ROI/CLV | O/U 2.5 vs AvgC/MaxC closings, thresholds 2/4/6/8%, week bootstrap CIs | 495d159 | part3_roi.json + table in part3_test.md |
| 3.3 score calibration | top-1 exact, top-3 set, {1:0,2:0,2:1}, 2-4 goals | 495d159 | part3_score_calibration.json |
| 3.4 config | config.yaml model: xi 0.002, w 0.5; decisions.md entry | 495d159 | uv run python -c "load_config()" -> 0.002 0.5 |

## Headline numbers

- Validation LL: baseline 2.9148 -> chosen 2.9112 (w=1.0 uniformly
  worst, 2.9218-2.9399; prior_sigma flat 0.25-0.75; every option within
  noise - paired bootstrap CI95 spans 0 for all comparisons).
- TEST LL: frozen 2.9157 vs baseline 2.9221 - tuning transferred
  (-0.0064); bins 0/1-5/6-15/>15 = 2.722/3.129/2.858/2.908.
- Calibration (test): top-1 exact score p 0.118 vs observed 0.126;
  top-3 set 0.311/0.332; O/U 2.5 Brier 0.2419; BTTS 0.2455.
- ROI O/U 2.5 (test): AvgC -9.4%..-11.1% (CI excludes 0 at every
  threshold); MaxC -4.3%..-5.7% (CI spans 0). NO edge over the closing
  market - expected, recorded honestly.

## Not done / caveats

- Branches not merged (owner's call); part 0 lives on phase-2-followup.
- Per-league ROI is in part3_roi.json (roi_<league>) but the markdown
  shows the aggregate only.
- The nightly owner lineups backfill may still be running; nothing in
  this session wrote to valuebet.db.

## Owner action

Match 4215 (Villarreal - Deportivo, understat 30804) was NOT reloaded
into valuebet.db (read-only rule). Command to reload after checking the
backfill is idle:

    uv run python -m core.ingest backfill --lineups-since 2026-09-05

(the run is resumable/idempotent: only matches without lineups are
fetched, and 4215 now loads with the dedup fix - verified on a copy).
