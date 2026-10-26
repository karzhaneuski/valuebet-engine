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
| **3. Парсер Fortuna** | `core/value/fortuna_input.py`: парсер точного счёта (`2:1 9.50`), другого счёта (`inny wynik`, `other`), мультисчёта (`1:0, 2:0, 2:1 @ 3.40`), диапазона голов (`2-4 gole @ 1.80`), справочных тоталов/1X2; детектор конфликтов; валидация 25 счетов сетки Fortuna (0..4), оценка `inny` только при полной сетке; расчёт маржи. | `0295765`, `3a21b36` | `tests/test_fortuna_input.py` (28 тестов): синтаксический мусор, дубликаты, маржа, сетка |
| **4. Value Engine** | `core/value/engine.py`: расчёт edge, пороги (точное $\ge 12\%$, мультисчёт $\ge 8\%$, odds $\le 21$, $p \ge 4\%$), ¼ Келли с потолком 2%, флаги риска (отсутствие ключевого игрока $\times 1.5$, исключение при $\ge 2$), детектор аномалии модели, вывод таблицы на русском; `inny` строго справочный (прогноз 4.74% vs факт 3.93%, $z=-2.28$, `scripts/verify_inny_bias.py`). | `107ac62`, `3a21b36` | `tests/test_engine.py` (11 тестов): пороги, отказ по длинным odds / малой $p$, Келли, таблица |
| **5. Префильтр кандидатов** | `core/value/prefilter.py`: отбор матчей для ручного ввода котировок по сетке Fortuna (`--has-fortuna-grid`), массе высоких ничьих $\sum_{i \ge 2} M_{i,i}$, ротации лиг (round-robin с лимитом 3 на лигу) и недельному лимиту $\le 10$ с учётом ранее отобранных; запись в `prefilter_decisions`. | `caef20d`, `3a21b36` | `tests/test_prefilter.py` (10 тестов); прогон на 79 матчах в `/tmp/p5.db` |
| **6. Хранение и CLI** | Миграции `48fd4f90f079` и `e5f1b2c3d4e5` (`selection_key`, `status: active/superseded/withdrawn`); `core/value/persistence.py`: жизненный цикл рекомендаций; запрет сохранения после commence time; CLI `core.value` (`evaluate`, `candidates`). | `0f2504d`, `3a21b36`, `2a3f2a5` | `tests/test_persistence_cli.py`, `tests/test_migrations.py`; 277 тестов green (3 skipped) |
| **7. Исследования маржи** | `scripts/fortuna_margin_study.py` (M0–M3), `scripts/soft_vs_sharp_totals.py` (M4), `scripts/verify_inny_bias.py`: замер реальной маржи Fortuna (32.7% на точном счёте), доказательство барьера zero-value, бэктест софта на тоталах (реализуемые сценарии 1 и 3, look-ahead статус сценария 2, line shopping MaxC), верификация смещения inny ($z = -2.28$). | `21d7b4d`, `187d567`, `6233e63` | `reports/fortuna_margin_study.md`, `reports/soft_vs_sharp_totals.md` |

---

## Detailed Step Breakdown & Findings

### Step 1: Fair Market Matrix
- **Implementation**: [`core/value/fair_matrix.py`](core/value/fair_matrix.py)
- **Numerical Validation**:
  Compared against `scripts/backtest_hybrid.py` for synthetic match ($O/U=1.85/2.05$, $1X2=1.95/3.60/4.20$):
  - $\lambda_{\text{total}} = 2.679633391807$
  - Home goal share $s = 0.548773950485$
  - Deviation between new `FairMatrix` and reference hybrid script: $< 10^{-12}$ across all 121 cells.
  - Sum of probabilities: $\sum_{i,j} M_{i,j} = 1.000000000000 \pm 10^{-14}$.
- **Fallback Behavior**:
  If line 2.5 is unavailable, the engine falls back to the nearest available half-line ($x.5$) with line recorded in metadata. If neither 1X2 nor totals exist, `status="no_market"`.

### Step 2: Pinnacle correct_score Benchmark
- **Implementation**: [`core/value/pinnacle_benchmark.py`](core/value/pinnacle_benchmark.py)
- **Actual DB Evaluation (Match 1973: Aston Villa vs Manchester City)**:
  - Loaded all 32 Pinnacle correct_score rows from `odds_snapshots`.
  - Overround: 1.266 (Pinnacle exact score margin ~26.6%).
  - Comparison against FairMatrix constructed with matching totals and 1X2:
    - TVD: 0.049
    - RMSE: 0.004
    - High draws difference ($\sum_{i \ge 2} M_{i,i}$): 0.034 (well-controlled under $\kappa_{\text{high}} = 0.20$).
  - **Honest Finding**: Live h2h was not polled in historic TOA snapshots (sport-level polling with `markets=totals,h2h` is scheduled for Phase 7).

### Step 3: Fortuna Manual Odds Parser
- **Implementation**: [`core/value/fortuna_input.py`](core/value/fortuna_input.py)
- **Features Tested**:
  - Exact scores: `2:1 9.50`, `2-1 @ 9,50`, `2:1 - 9.5`
  - Any other score: `inny wynik`, `pozostałe`, `other`, `любой другой`
  - Multiscore sets: `1:0, 2:0, 2:1 @ 3.40`
  - Goals ranges: `2-4 gole @ 1.80`, `2–4 bramki 1,80`, `2-4 гола`
  - Reference totals & 1X2: `over 2.5 1.95`, `1 @ 2.05`
  - Conflicting duplicate odds raise `FortunaParseError` with line numbers; identical lines are deduplicated.
  - Calculated bookmaker margin on exact score grid: 32.7% (measured across real Fortuna fixtures; partial batches without 25 cells do not compute margin).

### Step 4: Value Engine
- **Implementation**: [`core/value/engine.py`](core/value/engine.py)
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
- **Implementation**: [`core/value/prefilter.py`](core/value/prefilter.py)
- **Selection Criteria**:
  1. Fortuna grid availability (`has_fortuna_grid != False`).
  2. Mass of high draws in the fair market matrix $\sum_{i \ge 2} M_{i,i}$ (zone of $\kappa_{\text{high}}$ mispricing).
  3. League rotation: round-robin interleaving across active leagues with max 3 matches per league.
  4. Weekly budget limit $\le 10$ matches/week (`config.odds_policy.pinnacle_correct_score_max_weekly`).
- **Persistence**: Logged 79 decisions into `prefilter_decisions` on `/tmp/p5.db`. Idempotent on `(match_id, decision_date)`.

### Step 6: Persistence & CLI Demonstration
- **Migration**: `alembic/versions/48fd4f90f079_recommendations_matrix_provenance.py` & `e5f1b2c3d4e5_recommendation_lifecycle_status_and_key.py`
  - Added `matrix_source` and `input_hash` to `recommendations`.
  - Initial constraint `uq_recommendations_idempotency` on `(match_id, market, outcome, variant, input_hash)` was dropped in migration `e5f1b2c3d4e5` in favor of partial unique index `uq_recommendations_active` on `(match_id, selection_key, variant)` `WHERE status = 'active'` to support recommendation lifecycle transitions (`superseded`, `withdrawn`).
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
Источник рынка: market_pinnacle (pinnacle) | Время снимка: неизвестно (football-data)
Маржа Fortuna: не определена (неполная сетка) | Хеш входа: ebd9b60e26817814...
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
Любой другой счёт        15.00       —         —        —     НЕТ        —   Неполная сетка Fortuna (6 из 25): inny не оценивается
Тотал больше 2.5          1.58   62.7%      1.60    -1.0%     НЕТ        —   Справочный рынок
Исход 1                   1.62   59.5%      1.68    -3.6%     НЕТ        —   Справочный рынок
----------------------------------------------------------------------------------------------
Итого рекомендаций: 1
==============================================================================================
[OK] Записано 1 рекомендаций в таблицу 'recommendations' (матч ID 1141).
```
*Idempotency check: re-running this command updated existing records without creating duplicates (verified: exactly 1 row in `recommendations`).*

### Match 2: Valencia vs Barcelona (La Liga, 2024-08-17)
Pinnacle market: O/U 2.5: 1.85 / 2.05; 1X2: 4.89 / 3.85 / 1.75.
```
==============================================================================================
Матч: Valencia - Barcelona
Источник рынка: market_pinnacle (pinnacle) | Время снимка: неизвестно (football-data)
Маржа Fortuna: не определена (неполная сетка) | Хеш входа: a2d543be4d5ab0c1...
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
Источник рынка: market_pinnacle (pinnacle) | Время снимка: неизвестно (football-data)
Маржа Fortuna: не определена (неполная сетка) | Хеш входа: c2abad1180b1dad3...
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

## Выводы для продукта и архитектурные предложения (Product Conclusions & Proposals)

По результатам раунда ревью и эмпирических исследований (`reports/fortuna_margin_study.md`, `reports/soft_vs_sharp_totals.md`, `scripts/verify_inny_bias.py`):

### 1. Рынок точного счёта Fortuna (Exact Score, маржа ~32.7%)
- **Математический барьер**: Среднее матожидание цен $\mathbb{E}[p_{\text{fair}} \times \text{odds}] = \frac{1}{1 + M} \approx 0.753$ (средний edge $-24.6\%$).
- **Недостижимость порога**: Для преодоления порога `exact_score_edge: 0.12` требуется, чтобы коэффициент Fortuna был выше справедливого рынка более чем на **+48.7%**.
- **Эмпирический факт**: На реальных данных ни одна клетка из 25 счетов сетки не проходит фильтры (`odds <= 21`, `p_fair >= 0.04`). Доля сигналов — **0.0%**.
- **Инфляция ничьих $\kappa_{\text{high}}$**: Преимущество $+20\%$ на диагонали от 2:2 полностью съедается маржой ($1.20 / 1.327 - 1 = -9.6\%$). Точка безубыточности для $\kappa=0.20$ требует маржи букмекера **$M \le 7.14\%$**.
- **Роль в продукте**: Рынок точного счёта Fortuna остаётся сугубо вспомогательным (поиск грубых опечаток букмекера либо справочный анализ).

### 2. Рынок мультисчёта (Multiwynik, маржа ~20.7%)
- Маржа на **12 процентных пунктов ниже**, чем на точном счёте.
- При пороге `multiscore_edge: 0.08` группы исходов имеют существенно более высокие шансы на прохождение фильтра при локальных перекосах софта.

### 3. Рынок тоталов (Totals O/U 2.5, маржа 4.9% – 5.9%)
- **Исторический бэктест на валидации (n = 3,464 матча, `reports/soft_vs_sharp_totals.md`)**:
  1. *Сценарий 1 (Пре-матч)*: Bet365 плотно повторяет Pinnacle; расхождения $\ge 1\%$ редки (0.75% матчей, N=26 ставок за 2 сезона), а 95% доверительный интервал бутстрепа покрывает ноль [-57.2%; +64.6%]. Выборка статистически незначима.
  2. *Сценарий 3 (Лаг на закрытии)*: К моменту закрытия Bet365 и рыночный консенсус устраняют расхождения с Pinnacle; оставшиеся расхождения $\ge 1\%$ (1.88% матчей, N=65) показывают отрицательный ROI (-19.7%, 95% CI [-56.3%; +20.2%]).
  3. *Сценарий 2 (Закрытие против пре-матча)*: Содержит look-ahead bias (закрывающая линия Pinnacle неизвестна в момент предматчевой ставки) и исключён из выводов как нереализуемый на практике.
  4. *Line Shopping (Рыночный максимум MaxC)*: Перевес возникает только при поиске лучшей цены среди ~30 контор (MaxC: ROI +4.4% при пороге 2%, N=1,079).
- **Архитектурное предложение (ожидает решения владельца проекта)**: Ввиду нулевой доходности точного счёта при марже 32.7% и отсутствия статистически значимого перевеса на статических тоталах у одного букмекера, переход к мониторингу низкомаржинальных тоталов или мультибукмекерскому сканеру формулируется как **предложение для обсуждения roadmap**. Текущая спецификация SPEC v2 и рекомендуемые рынки (точный счёт, мультисчёт, диапазоны голов) остаются без изменений, а тоталы остаются строго справочными до явного решения владельца проекта.

---

## Instructions for Project Owner (Main Database Migration)

After merging branch `phase-5-fair-pricing` into `main`, apply the Alembic migrations to `valuebet.db`:

```bash
uv run alembic upgrade head
```

This applies two migrations:
1. `48fd4f90f079_recommendations_matrix_provenance.py`: adds `matrix_source` and `input_hash`.
2. `e5f1b2c3d4e5_recommendation_lifecycle_status_and_key.py`: adds `selection_key`, `status` (`active`, `superseded`, `withdrawn`), and partial unique index `uq_recommendations_active` (dropping initial constraint `uq_recommendations_idempotency`).

---

## Test Verification Output (`uv run pytest`)

```
=============================== test session starts ===============================
platform darwin -- Python 3.12.9, pytest-8.3.4, pluggy-1.5.0
rootdir: /Users/karzhaneuski/projects/valuebet-engine
configfile: pyproject.toml
collected 280 items

........................................................................ [ 25%]
........................................................................ [ 51%]
........................................................................ [ 77%]
....................................s........................ss.         [100%]
=============================== warnings summary ===============================
.venv/lib/python3.12/site-packages/fastapi/testclient.py:1
  /Users/karzhaneuski/projects/valuebet-engine/.venv/lib/python3.12/site-packages/fastapi/testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
    from starlette.testclient import TestClient as TestClient  # noqa

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
277 passed, 3 skipped, 1 warning in 31.03s
```
