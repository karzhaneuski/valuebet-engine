# Self-Review Report: Phase 7 (Tracking, CLV Settlement, and Scheduler)

**Date**: 2026-10-26  
**Branch**: `phase-7-tracking`  
**Parent**: `phase-6-news` (`5df8d9b`)  

---

## 1. Scope & Implementation Details (SPEC §7 & §9)

Phase 7 implements user bet tracking, automated settlement across all football markets, pre-kickoff CLV auditing, portfolio statistics with bootstrap confidence intervals, and the APScheduler background automation with quota pacing:

1. **User Bet Tracking & Schema Migration (`core/orm.py`, `alembic/versions/f8677b5ff97c_create_user_bets_table.py`)**:
   - `UserBet` model: `id`, `match_id`, `market`, `selection_key`, `line`, `odds`, `stake_units`, `bookmaker`, `placed_at_utc`, `p_fair_at_placement`, `matrix_snapshot_id`, `status` (`open`, `won`, `lost`, `push`, `void`), `payout_units`, `pnl_units`, `clv_fair`, `clv_source`, `notes`, `created_at_utc`, `settled_at_utc`.
   - Migration `f8677b5ff97c` successfully generated and applied (`alembic upgrade head` verified clean and reproducible from scratch).
2. **Outcome Settlement Across All Football Markets (`core/tracking/settlement.py`)**:
   - `exact_score`: supports standard scores (`2-1`, `2:1`) and Fortuna `"inny"` / `"other"` grid overflow (>4 goals per team).
   - `multiscore`: cluster sets (`1-0, 2-0, 3-0`).
   - `goals_range`: bounded ranges (`2-3`, `4-6`) and open-ended ranges (`7+`).
   - `totals`: half-lines and integer lines with push return (`push` sets payout = stake, pnl = 0.00).
   - `btts`: both teams to score (`yes`/`no`).
   - `h2h`: match outcome 1X2 (`1`, `X`, `2`).
   - `team_totals`: home or away team goals with push on whole lines.
3. **Pre-Kickoff CLV Calculation (`core/tracking/clv.py`)**:
   - Implements formula:
     $$\text{CLV}_{\text{fair}} = \text{odds}_{\text{placed}} \cdot p_{\text{fair, closing}} + p_{\text{push}} \cdot 1.0 - 1.0$$
   - Closing fair matrix constructed strictly from snapshots BEFORE kickoff (`captured_at_utc < commence_time_utc`), prioritizing official closing lines (`ClosingOdds` PC/AvgC).
   - Provenance tracked via `clv_source = "fair_market_matrix"`.
4. **Portfolio Performance & Statistics (`core/tracking/stats.py`)**:
   - Aggregate P/L, total stake, ROI (yield on turnover), hit rate.
   - Average CLV, % positive CLV bets, Brier score.
   - 95% bootstrap confidence intervals for ROI and CLV (1,000 resamples).
   - Mandatory small-sample disclaimer for $N < 100$ settled bets:
     `"⚠️ Выборка (N={N}) статистически недостаточна для выводов о прибыльности. Высокая дисперсия."`
5. **Quota Pacing & Degradation Ladder (`core/scheduler/pacing.py`)**:
   - Projected monthly burn: $\text{burn} = \text{credits\_used} + \text{daily\_rate} \cdot \text{days\_left}$.
   - Dynamically reads `config.odds_policy.degradation_ladder` (`cap_correct_score_5_week`, `disable_t3_h2h`, `t3_slot_to_day`).
   - Hard emergency stop at 450 credits disables all paid calls.
6. **Automation Tasks & Scheduler Service (`core/scheduler/tasks.py`, `core/scheduler/main.py`)**:
   - Scheduled tasks: daily history refresh, TOA odds snapshots (totals + h2h), weekly prefilter candidates ($\le 10$/week), news searches (T-24h, T-3h), match settlement & CLV calculation.
   - `--dry-run` CLI execution mode tested and operational (`python -m core.scheduler.main --dry-run`).
   - FastAPI health endpoints (`/health`, `/status`, `/run-sync`).

---

## 2. Review Checklist Verification

| Checklist Item | Status | Verification Detail |
| :--- | :---: | :--- |
| **Settlement accurate across all markets** | PASS | Exact score (including inny >4 goals), multiscore, goals range, totals with integer push, BTTS, 1X2, team totals. |
| **CLV strictly pre-kickoff** | PASS | Closing matrix built from closing quotes / snapshots strictly before `commence_time_utc`. |
| **CLV primary, ROI disclaimed** | PASS | ROI accompanied by bootstrap CI and mandatory variance disclaimer when $N < 100$. |
| **TOA quota budget & hard stop** | PASS | Budget ~393 credits/30d monitored; hard stop at 450 blocks all paid requests. |
| **Dynamic degradation ladder** | PASS | Reads `config.odds_policy.degradation_ladder` sequentially upon pacing alerts. |
| **Dry-run mode operational** | PASS | CLI `--dry-run` runs all jobs synchronously without mutations. |
| **Fault-tolerant scheduling** | PASS | Individual task failures caught and logged without aborting scheduler. |
| **Tests green** | PASS | 11 passed in `test_tracking.py`, 6 passed in `test_scheduler.py`, full suite 323 passed, 0 failed. |

---

## 3. Test & Verification Output

- `uv run ruff check .`: All checks passed!
- `uv run mypy`: Success: no issues found in 84 source files.
- `uv run mypy --strict core/model core/backtest core/value`: Success: no issues found in 17 source files.
- `uv run pytest`: `323 passed, 3 skipped, 1 warning in 31.87s`.
