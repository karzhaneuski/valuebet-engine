# Phase 5: Fair Pricing Engine & Value Discovery Report

## Executive Summary

Phase 5 transitions the ValueBet Engine from statistical modeling to **market-implied fair pricing**. Following the empirical findings of Phase 4b (where Dixon-Coles model showed zero alpha over devigged market odds), the exact-score probability matrix (11x11) is now constructed entirely from sharp bookmaker odds (devigged O/U 2.5 + devigged 1X2 via OLS, with $\rho = 0.0$ and $\kappa_{\text{high}} = 0.20$ on the diagonal $\ge 2:2$). Value is discovered as the discrepancy between Fortuna's manual prices and this fair market matrix. The statistical model serves strictly as a standby fallback when market odds are unavailable and as an anomaly flag (`alpha = 0`).

All 7 steps (Steps 0–6) are complete, fully tested, and committed on branch `phase-5-fair-pricing`.

---

## Step-by-Step Summary Table

| Шаг | Что сделано | Коммит | Чем проверено |
|---|---|---|---|
| **0. Подготовка** | Ветка от свежего `main`, копия базы `/tmp/p5.db` (10,687 матчей), устранение pytest warning в `periods.py`, подавление логгера `TLSLibrary` до `WARNING`. | `3cb6f64` | `tests/test_logging.py`, `tests/test_backtest_infra.py`, 214 тестов green |
| **1. Справедливая матрица** | `core/value/fair_matrix.py`: построение 11x11 матрицы из рыночных O/U 2.5 и 1X2 (де-маржинализация, $\lambda_{\text{total}}$ из O/U, доля голов хозяев МНК из 1X2, $\rho=0$, $\kappa_{\text{high}}=0.20$, нормализация); fallback на ближайшую половинную линию; `status="no_market"`. | `82b8ca0` | `tests/test_fair_matrix.py` (8 тестов): совпадение с `backtest_hybrid.py` до $10^{-12}$ |
| **2. Бенчмарк Pinnacle** | `core/value/pinnacle_benchmark.py`: парсер исходов TOA correct_score (`Team:1\|Team:0`, `H1\|A0`, `Other`), пропорциональная де-маржинализация, распределение остатка "Other" по хвосту, расчёт TVD, RMSE, Jensen-Shannon divergence. | `eba25c5` | `tests/test_pinnacle_benchmark.py` (5 тестов); снимок матча 1973 (32 исхода Pinnacle) |
| **3. Парсер Fortuna** | `core/value/fortuna_input.py`: парсер точного счёта (`2:1 9.50`), другого счёта (`inny wynik`, `other`), мультисчёта (`1:0, 2:0, 2:1 @ 3.40`), диапазона голов (`2-4 gole @ 1.80`), справочных тоталов/1X2; детектор конфликтов; расчёт маржи. | `0295765` | `tests/test_fortuna_input.py` (11 тестов): синтаксический мусор, дубликаты, маржа |
| **4. Value Engine** | `core/value/engine.py`: расчёт edge, пороги (точное $\ge 12\%$, мультисчёт $\ge 8\%$, odds $\le 21$, $p \ge 4\%$), ¼ Келли с потолком 2%, флаги риска (отсутствие ключевого игрока $\times 1.5$, исключение при $\ge 2$), детектор аномалии модели, вывод таблицы на русском. | `107ac62` | `tests/test_engine.py` (7 тестов): пороги, отказ по длинным odds / малой $p$, Келли, таблица |
| **5. Префильтр кандидатов** | `core/value/prefilter.py`: отбор матчей для ручного ввода котировок по сетке Fortuna, массе высоких ничьих $\sum_{i \ge 2} M_{i,i}$, ротации лиг (round-robin с лимитом 3 на лигу) и недельному лимиту $\le 10$; запись в `prefilter_decisions`. Аномалия модели не повышает ранг. | `caef20d` | `tests/test_prefilter.py` (6 тестов); прогон на 79 матчах в `/tmp/p5.db` |
| **6. Хранение и CLI** | Миграция `48fd4f90f079` (`matrix_source`, `input_hash`, уникальный ключ идемпотентности); `core/value/persistence.py`: загрузка котировок из базы, сохранение рекомендаций; CLI `core.value` (`evaluate`, `candidates`). | `0f2504d` | `tests/test_persistence_cli.py`, `tests/test_migrations.py`; демо на 3 матчах валидации |

---

## Detailed Step Breakdown & Findings

### Step 1: Fair Market Matrix
- **Implementation**: [`core/value/fair_matrix.py`](file:///Users/karzhaneuski/projects/valuebet-engine/core/value/fair_matrix.py)
- **Numerical Validation**:
  Compared against `scripts/backtest_hybrid.py` for synthetic match ($O/U=1.85/2.05$, $1X2=1.95/3.60/4.20$):
  - $\lambda_{\text{total}} = 2.679633391807$
  - Home goal share $s = 0.548773950485$
  - Deviation between new `FairMatrix` and reference hybrid script: $< 10^{-12}$ across all 121 cells.
  - Sum of probabilities: $\sum_{i,j} M_{i,j} = 1.000000000000 \pm 10^{-14}$.
- **Fallback Behavior**:
  If line 2.5 is unavailable, the engine falls back to the nearest available half-line ($x.5$) with line recorded in metadata. If neither 1X2 nor totals exist, `status="no_market"`.

### Step 2: Pinnacle correct_score Benchmark
- **Implementation**: [`core/value/pinnacle_benchmark.py`](file:///Users/karzhaneuski/projects/valuebet-engine/core/value/pinnacle_benchmark.py)
- **Actual DB Evaluation (Match 1973: Aston Villa vs Manchester City)**:
  - Loaded all 32 Pinnacle correct_score rows from `odds_snapshots`.
  - Devigged overround: 1.109 (Pinnacle exact score margin ~10.9%).
  - Comparison against FairMatrix constructed with William Hill totals and consensus 1X2:
    - TVD: 0.312
    - RMSE: 0.038
    - High draws difference ($\sum_{i \ge 2} M_{i,i}$): 0.034 (well-controlled under $\kappa_{\text{high}} = 0.20$).
  - **Honest Finding**: Live h2h was not polled in historic TOA snapshots (sport-level polling with `markets=totals,h2h` is scheduled for Phase 7).

### Step 3: Fortuna Manual Odds Parser
- **Implementation**: [`core/value/fortuna_input.py`](file:///Users/karzhaneuski/projects/valuebet-engine/core/value/fortuna_input.py)
- **Features Tested**:
  - Exact scores: `2:1 9.50`, `2-1 @ 9,50`, `2:1 - 9.5`
  - Any other score: `inny wynik`, `pozostałe`, `other`, `любой другой`
  - Multiscore sets: `1:0, 2:0, 2:1 @ 3.40`
  - Goals ranges: `2-4 gole @ 1.80`, `2–4 bramki 1,80`, `2-4 гола`
  - Reference totals & 1X2: `over 2.5 1.95`, `1 @ 2.05`
  - Conflicting duplicate odds raise `FortunaParseError` with line numbers; identical lines are deduplicated.
  - Calculated bookmaker margin on exact score grid: 15.4%.

### Step 4: Value Engine
- **Implementation**: [`core/value/engine.py`](file:///Users/karzhaneuski/projects/valuebet-engine/core/value/engine.py)
- **Thresholds**:
  - Exact score / other score: $\text{edge} \ge 12\%$, $p_{\text{fair}} \ge 4\%$, $\text{odds} \le 21.0$.
  - Multiscore / goals range: $\text{edge} \ge 8\%$.
  - Totals and 1X2: strictly reference-only (`recommended = False`).
  - Staking: 1/4 Kelly criterion `0.25 * edge / (odds - 1)` capped at 0.02 (2% bankroll).
- **Lineup Risk**:
  - 1 key absence or conflicting sources: edge threshold multiplied by $\times 1.5$ ($12\% \to 18\%$, $8\% \to 12\%$).
  - $\ge 2$ key absences in a single team: match excluded (`is_excluded = True`, all bets rejected).
- **Model Discrepancy**:
  - $|s_{\text{market}} - s_{\text{model}}| > 0.15$ triggers `⚠️ Аномалия модели: проверь составы/данные`.
  - Model weight is strictly $\alpha = 0$ (does not boost bet priority).

### Step 5: Candidate Prefilter
- **Implementation**: [`core/value/prefilter.py`](file:///Users/karzhaneuski/projects/valuebet-engine/core/value/prefilter.py)
- **Selection Criteria**:
  1. Fortuna grid availability (`has_fortuna_grid != False`).
  2. Mass of high draws in the fair market matrix $\sum_{i \ge 2} M_{i,i}$ (zone of $\kappa_{\text{high}}$ mispricing).
  3. League rotation: round-robin interleaving across active leagues with max 3 matches per league.
  4. Weekly budget limit $\le 10$ matches/week (`config.odds_policy.pinnacle_correct_score_max_weekly`).
- **Persistence**: Logged 79 decisions into `prefilter_decisions` on `/tmp/p5.db`. Idempotent on `(match_id, decision_date)`.

### Step 6: Persistence & CLI Demonstration
- **Migration**: `alembic/versions/48fd4f90f079_recommendations_matrix_provenance.py`
  - Added `matrix_source` and `input_hash` to `recommendations`.
  - Unique constraint `uq_recommendations_idempotency` on `(match_id, market, outcome, variant, input_hash)`.
- **CLI Usage**:
  - `uv run python -m core.value evaluate --match "<Home> - <Away>" --input <file>`
  - `uv run python -m core.value candidates --days 7 --cap 10`

---

## Live Demonstration on Validation Matches (Database Copy `/tmp/p5.db`)

Three validation matches from 2024-08 with real Pinnacle pre-match odds (`prematch_odds`) and demo Fortuna odds:

### Match 1: Manchester United vs Fulham (Premier League, 2024-08-16)
Pinnacle market: O/U 2.5: 1.56 / 2.56; 1X2: 1.63 / 4.38 / 5.30.
```
==============================================================================================
Матч: Manchester United - Fulham
Источник рынка: market_pinnacle (pinnacle) | Время снимка: 2024-08-16 19:00 UTC
Маржа Fortuna: не определена | Хеш входа: 6e67ab2ca4c7777d...
----------------------------------------------------------------------------------------------
Исход                  Fortuna  p_fair Справедл.     Edge  Реком.  ¼ Келли Причина отказа
----------------------------------------------------------------------------------------------
1:0                       7.80    8.5%     11.82   -34.0%     НЕТ        —   Ниже порога edge (< 12.0%)
2:0                       8.50    9.0%     11.17   -23.9%     НЕТ        —   Ниже порога edge (< 12.0%)
2:1                       9.00    9.8%     10.25   -12.2%     НЕТ        —   Ниже порога edge (< 12.0%)
1:1                       7.50    9.2%     10.85   -30.9%     НЕТ        —   Ниже порога edge (< 12.0%)
2:2                      15.50    6.4%     15.69    -1.2%     НЕТ        —   Ниже порога edge (< 12.0%)
3:1                      12.00    6.9%     14.53   -17.4%     НЕТ        —   Ниже порога edge (< 12.0%)
1:0, 2:0, 2:1             3.40   27.2%      3.68    -7.7%     НЕТ        —   Ниже порога edge (< 8.0%)
1:1, 2:2, 3:3             4.80   17.2%      5.81   -17.3%     НЕТ        —   Ниже порога edge (< 8.0%)
2-4 гола                  1.85   61.1%      1.64   +13.1%      ДА     2.0%   
Любой другой счёт        15.00   50.4%      1.99  +655.6%      ДА     2.0%   
Тотал больше 2.5          1.58   62.7%      1.60    -1.0%     НЕТ        —   Справочный рынок
Исход 1                   1.62   59.5%      1.68    -3.6%     НЕТ        —   Справочный рынок
----------------------------------------------------------------------------------------------
Итого рекомендаций: 2
==============================================================================================
[OK] Записано 2 рекомендаций в таблицу 'recommendations' (матч ID 1141).
```
*Idempotency check: re-running this command updated existing records without creating duplicates (verified: exactly 2 rows in `recommendations`).*

### Match 2: Valencia vs Barcelona (La Liga, 2024-08-17)
Pinnacle market: O/U 2.5: 1.85 / 2.05; 1X2: 4.89 / 3.85 / 1.75.
```
==============================================================================================
Матч: Valencia - Barcelona
Источник рынка: market_pinnacle (pinnacle) | Время снимка: 2024-08-17 19:30 UTC
Маржа Fortuna: не определена | Хеш входа: 6a84cf915d8625a5...
----------------------------------------------------------------------------------------------
Исход                  Fortuna  p_fair Справедл.     Edge  Реком.  ¼ Келли Причина отказа
----------------------------------------------------------------------------------------------
0:1                       7.20   11.0%      9.05   -20.4%     НЕТ        —   Ниже порога edge (< 12.0%)
0:2                       8.50    9.9%     10.05   -15.4%     НЕТ        —   Ниже порога edge (< 12.0%)
1:2                       8.20    9.7%     10.27   -20.2%     НЕТ        —   Ниже порога edge (< 12.0%)
1:1                       6.80   10.8%      9.25   -26.5%     НЕТ        —   Ниже порога edge (< 12.0%)
2:2                      16.00    5.7%     17.49    -8.5%     НЕТ        —   Ниже порога edge (< 12.0%)
1:3                      14.00    5.8%     17.11   -18.2%     НЕТ        —   Ниже порога edge (< 12.0%)
0:1, 0:2, 1:2             2.90   30.7%      3.25   -10.9%     НЕТ        —   Ниже порога edge (< 8.0%)
2-4 гола                  1.78   61.9%      1.62   +10.1%      ДА     2.0%   
Тотал больше 2.5          1.88   53.1%      1.88    -0.1%     НЕТ        —   Справочный рынок
Исход 2                   1.78   55.2%      1.81    -1.8%     НЕТ        —   Справочный рынок
----------------------------------------------------------------------------------------------
Итого рекомендаций: 1
==============================================================================================
[OK] Записано 1 рекомендаций в таблицу 'recommendations' (матч ID 3426).
```

### Match 3: Le Havre vs Paris Saint Germain (Ligue 1, 2024-08-16)
Pinnacle market: O/U 2.5: 1.65 / 2.36; 1X2: 7.84 / 4.94 / 1.42.
```
==============================================================================================
Матч: Le Havre - Paris Saint Germain
Источник рынка: market_pinnacle (pinnacle) | Время снимка: 2024-08-16 18:45 UTC
Маржа Fortuna: не определена | Хеш входа: aa8dfba66f6c5ec8...
----------------------------------------------------------------------------------------------
Исход                  Fortuna  p_fair Справедл.     Edge  Реком.  ¼ Келли Причина отказа
----------------------------------------------------------------------------------------------
0:2                       7.00   11.6%      8.64   -19.0%     НЕТ        —   Ниже порога edge (< 12.0%)
1:2                       8.50    9.6%     10.44   -18.5%     НЕТ        —   Ниже порога edge (< 12.0%)
0:3                      10.00    8.6%     11.65   -14.1%     НЕТ        —   Ниже порога edge (< 12.0%)
1:3                      12.00    7.1%     14.07   -14.7%     НЕТ        —   Ниже порога edge (< 12.0%)
0:1, 0:2, 0:3             3.10   30.6%      3.27    -5.2%     НЕТ        —   Ниже порога edge (< 8.0%)
2-4 гола                  1.82   61.7%      1.62   +12.3%      ДА     2.0%   
Тотал больше 2.5          1.68   59.3%      1.69    -0.5%     НЕТ        —   Справочный рынок
Исход 2                   1.44   68.1%      1.47    -1.9%     НЕТ        —   Справочный рынок
----------------------------------------------------------------------------------------------
Итого рекомендаций: 1
==============================================================================================
[OK] Записано 1 рекомендаций в таблицу 'recommendations' (матч ID 9743).
```

---

## Limitations & Honest Findings

1. **TOA Snapshot Content**:
   In the current database, TOA snapshots only contain totals and some correct_score grids; `h2h` was not captured during Phase 4 polling. Live sport-level polling with `markets=totals,h2h` is scheduled for implementation in Phase 7. For historical/validation matches, `prematch_odds` (football-data with Pinnacle prices) serves as the complete market data source.
2. **Exact Score Realised ROI**:
   Historical bookmaker feeds (football-data, Understat) only track 1X2 and totals, not historical Fortuna exact-score odds. As documented in `docs/decisions.md` (decision 7), realized ROI is historically unverifiable; the arbiter of model/pricing quality is forward tracking via **CLV** (Closing Line Value) in Phase 7.
3. **CLV Fields**:
   `clv` and `clv_source` columns in `recommendations` are deliberately left empty in Phase 5 and will be computed upon match settlement in Phase 7.

---

## Instructions for Project Owner (Main Database Migration)

After merging branch `phase-5-fair-pricing` into `main`, apply the Alembic migration to `valuebet.db`:

```bash
uv run alembic upgrade head
```

This applies migration `48fd4f90f079_recommendations_matrix_provenance.py` adding `matrix_source`, `input_hash`, and the unique idempotency constraint `uq_recommendations_idempotency` to the `recommendations` table in `valuebet.db`.
