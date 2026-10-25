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
| B1 | prematch_odds table (own table over a kind flag: different bookmaker set, closing CHECK constraint, CLV-by-bookmaker code) + migration a86d9da4539e + fd loader (Avg/P/Max columns) + idempotent upsert; copy load: 21,017 rows (Avg 7427 / Max 7427 / P 6163), rerun 0 updates | f42c8dc | tests/test_football_data.py::test_prematch_odds_parsed_and_stored; run on /tmp/bt4b.db |
| B2 | hybrid matrix (prematch-Avg de-vig lambda_total, model home share, model rho): validation LL 2.9067 vs model 2.9112, delta -0.0045 CI [-0.0088,-0.0001]; closing source -0.0103 [-0.0153,-0.0052] - reviewer numbers reproduced exactly | c0ceba5 | reports/backtest/4b_hybrid_prematch.json / _closing.json |
| B3 | draw correction: flat kappa breaks 0:0 (kappa .10 -> 0:0 z -2.9), separate rho leaves 2:2; chosen high-draw-only inflation (2:2/3:3/.. x1.20, renormalised): all cells \|z\|<=2, LL 2.9054 (delta -0.0058); all fix variants LL-TIED pairwise - choice by cell calibration at one parameter | c0ceba5 | 4b_hybrid_prematch_k02.json; pairwise bootstrap in decisions.md |
| B4 | logit mixture: w_model 0.18 / w_market 1.01; LL 0.6647 TIED with pure market (CI [-0.0021,+0.0006]); CLV of model bets: line moves toward us in 21.3% of 1842 (mean -1.7pp) -> O/U reference-only | 97a785b | 4b_mixture.json |
| B5 | FROZEN_CONFIG_4b.json committed BEFORE the test; single second-look run: hybrid LL 2.9043 vs model 2.9158, delta -0.0115 [-0.0163,-0.0068]; every cell \|z\|<=2 (diagonal -0.5/+0.2/-0.3); multiscore top-1 0.119/0.131, top-3 0.315/0.329, {1:0,2:0,2:1} 0.242/0.248, 2-4 goals 0.610/0.614 | ec6db49, 60aa2cc | 4b_test_second_look.json |
| B6 | phase-5 consequences: exact/multiscore from the HYBRID matrix; live lambda from TOA T-24 totals (Pinnacle else allowed-consensus de-vig); S2 prefilter compares hybrid quantities (the proxy IS the hybrid); O/U reference-only; exact-score ROI unverifiable historically - forward tracking is the arbiter | 60aa2cc | decisions.md entries 2026-10-26 |

## Headline numbers

- Hybrid vs pure model, exact-score log loss: validation -0.0045
  [-0.0088,-0.0001]; second-look test -0.0115 [-0.0163,-0.0068] - the
  market's total level carries information the model lacks, and the
  gain transfers.
- Draw fix: model/hybrid underpriced 2:2 (z +3.2/+3.4); after the
  high-draw inflation every 0:0..3:3 cell is calibrated (validation AND
  test \|z\|<=2).
- O/U 2.5: the mixture collapses onto the market (w_model 0.18); the
  model's edge-bets see the line move AGAINST them (21% with us).
  O/U stays reference-only.
- Part A honesty numbers: de-vigged AvgC beats the model on O/U 2.5
  (Brier 0.2376 vs 0.2422, CI excludes 0); model ROI is 3.3-5.0 pp
  below the margin-only fair ROI on the test period.

## Not done

- Branches not merged (owner's call); part-A fixes live on
  phase-4-backtest, part B on phase-4b-hybrid.
- The prematch load command for the MAIN database is NOT executed
  (read-only rule) - see below.
- Cell tables were not re-run for the mixture (it equals the market;
  nothing new to tabulate).

## Owner commands (after checking nothing else writes the DB)

    # migration + prematch odds load (idempotent):
    uv run alembic upgrade head
    uv run python -m core.ingest backfill          # fd section writes prematch_odds

21,017 prematch rows are expected for seasons 2022-2026 on the current
data (measured on the copy); a rerun performs 0 writes.
