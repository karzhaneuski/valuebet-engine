# Part 3: final TEST evaluation (2025/26 + 2026/27, n=2111)

Run ONCE, after FROZEN_CONFIG.json (commit fc789a8). DB copy /tmp/bt3.db; zero TOA credits; no network.

## 3.1 Metrics: frozen vs baseline config

| config | log loss | bin 0 | bin 1-5 | bin 6-15 | bin >15 | Brier O/U 2.5 | Brier BTTS |
|---|---|---|---|---|---|---|---|
| frozen (xi=0.002, w=0.5) | 2.9157 | 2.7220 | 3.1294 | 2.8576 | 2.9083 | 0.2419 | 0.2455 |
| baseline (xi=0.005, w=0.5) | 2.9221 | 2.7181 | 3.1773 | 2.8646 | 2.9117 | 0.2430 | 0.2460 |

Validation-trained tuning TRANSFERRED: the frozen config beats the
phase-3 default out of sample by 0.0064 log loss (2.9157 vs
2.9221), improving every history bin except bin 0 (2.722 vs 2.718,
n=28 - noise). The 1-5 bin improves 3.177 -> 3.129. Reliability
curves: reports/backtest/part3_reliability.png (O/U 1.5/2.5/3.5,
BTTS).

## 3.2 ROI / CLV on O/U 2.5 vs football-data closings

Market probability = de-vigged AvgC pair; bet if
edge = p_model * price - 1 >= threshold; flat 1 unit; 95% CI by
week-clustered bootstrap (2000).

| execution | threshold | bets | hit | ROI | 95% CI | over/under |
|---|---|---|---|---|---|---|
| AvgC | 2% | 984 | 0.432 | -0.094 | [-0.159, -0.024] | 651/333 |
| AvgC | 4% | 793 | 0.416 | -0.111 | [-0.183, -0.035] | 521/272 |
| AvgC | 6% | 636 | 0.410 | -0.106 | [-0.186, -0.028] | 415/221 |
| AvgC | 8% | 510 | 0.412 | -0.089 | [-0.176, +0.007] | 323/187 |
| MaxC | 2% | 1420 | 0.437 | -0.053 | [-0.109, +0.005] | 881/539 |
| MaxC | 4% | 1199 | 0.435 | -0.043 | [-0.103, +0.021] | 748/451 |
| MaxC | 6% | 996 | 0.418 | -0.057 | [-0.125, +0.016] | 624/372 |
| MaxC | 8% | 818 | 0.417 | -0.044 | [-0.124, +0.036] | 517/301 |

**Honest conclusion: NO edge over the market.** At realistic AvgC
execution the ROI is -9% to -11% with confidence intervals
excluding zero at every threshold; at the optimistic MaxC prices it
is -4% to -6% with intervals straddling zero. The model finds many
'edges' >= 2% against the de-vigged AvgC pair (984 bets), but they
do not survive the margin - consistent with an efficient closing
market. This is not a phase failure (the task explicitly expects
it); the calibrated probabilities are the product, the betting
thresholds stay conservative.

## 3.3 Exact-score and multiscore calibration (test)

| market | n | mean p | observed | Brier |
|---|---|---|---|---|
| top1_exact | 2111 | 0.1179 | 0.1265 | 0.1102 |
| top3_multiscore | 2111 | 0.3113 | 0.3316 | 0.2200 |
| fixed_set_1_0_2_0_2_1 | 2111 | 0.2393 | 0.2477 | 0.1809 |
| goals_range_2_4 | 2111 | 0.6076 | 0.6144 | 0.2362 |

All four markets are well calibrated with a slight conservative
bias (predicted 0.5-2pp below observed); note for phase-5
thresholds (fewer false 'value' signals). Full binning in
reports/backtest/part3_score_calibration.json.

## 3.4 Frozen parameters

Selected on validation (part2_tuning.md): every tuned variant was
statistically tied with the simplest configuration (paired
week-clustered bootstrap), so xi=0.002, w=0.5, prior_sigma=0.5,
no model options. Written to config.yaml; see decisions.md entry
2026-10-25 (phase 4).

## 3.2a Model vs de-vigged AvgC on the same matches (review S1)

On the 2096 test matches with an AvgC closing pair:

| metric | model | de-vigged AvgC | difference [95% CI] |
|---|---|---|---|
| Brier O/U 2.5 | 0.2422 | 0.2376 | +0.0023 .. +0.0067 |
| binary log loss | 0.6771 | 0.6676 | +0.0049 .. +0.0142 |

The market's de-vigged closing probability is BETTER calibrated on
O/U 2.5 than the model, by 0.0046 Brier and 0.0096 log loss, both
CI excluding zero. (Against PRE-MATCH Avg the model is closer -
computed in the phase-4b report once prematch odds are loaded.)

## 3.2b Fair ROI, benchmarks and adverse selection

Mean AvgC margin on these matches: 0.0653; fair ROI -m/(1+m) = **-0.0613**.

| strategy | ROI |
|---|---|
| random side | -0.0570 |
| always over | -0.0397 |
| always under | -0.0921 |

| threshold | bets | model ROI | fair ROI | model - fair | E[p_model] | E[p_market] | actual win |
|---|---|---|---|---|---|---|---|
| 0% | 1193 | -0.0881 | -0.0881 | -0.0268 | 0.518 | 0.451 | 0.441 |
| 2% | 984 | -0.0938 | -0.0938 | -0.0325 | 0.518 | 0.444 | 0.432 |
| 4% | 793 | -0.1111 | -0.1111 | -0.0498 | 0.517 | 0.437 | 0.416 |
| 6% | 636 | -0.1061 | -0.1061 | -0.0448 | 0.516 | 0.430 | 0.410 |
| 8% | 510 | -0.0889 | -0.0889 | -0.0276 | 0.515 | 0.422 | 0.412 |

Adverse selection is visible: at every threshold the model expects
~0.52 win probability on its bets, the de-vigged market says 0.42-
0.44, and reality lands on 0.41-0.44 - the model's excess confidence selects precisely the matches it is wrong about. The
model's ROI is 3.3-5.0 pp BELOW the fair (margin-only) ROI.

### ROI split by side and league (2% threshold, AvgC)

| slice | bets | ROI |
|---|---|---|
| over | 651 | -0.0576 |
| under | 333 | -0.1647 |
| premier_league | 211 | -0.0136 |
| la_liga | 233 | -0.1334 |
| serie_a | 229 | -0.0057 |
| bundesliga | 145 | -0.1810 |
| ligue_1 | 166 | -0.1857 |

## 3.5 Thresholds and execution limits (review S4/S5)

**No O/U 2.5 threshold makes money.** At 2/4/6/8% edges the AvgC ROI
is -9.4/-11.1/-10.6/-8.9% and even against MaxC every CI95 straddles
or stays below zero (best CI upper bound +0.036 at 8%). Decision for
phase 5: the raw model issues NO O/U 2.5 signals; totals probabilities
are reference output only (revisited in phase 4b via a market mixture).

**MaxC is not an executable price.** On the validation seasons 1104 of
3504 matches (31.5%) show a guaranteed-profit fork 1/MaxO + 1/MaxU < 1
- the MaxC book is a best-of composite no single bookmaker offers, so
any ROI against MaxC is an unreachable upper bound, not a scenario.

**Margins differ across periods** (part3_margin.json): mean AvgC margin
0.0479 on validation vs 0.0653 on test - ROI
numbers are NOT comparable between periods; the fair-ROI baseline
(-m/(1+m)) moves with the margin itself.
