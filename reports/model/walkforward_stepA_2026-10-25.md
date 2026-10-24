# Step A evidence (review B1+S1+S7): log-score by history bin

Script: `scripts/model_walkforward.py` — weekly refits over all five
leagues from 2025-08-08 to now, predicting each week's finished matches,
binning the score log-probability by the SHALLOWER team's number of
training matches.

Command (identical for both runs, on a copy of the DB):

```
sqlite3 valuebet.db ".backup /tmp/wf-smoke.db"
DATABASE_URL=sqlite:////tmp/wf-smoke.db uv run python -m scripts.model_walkforward
```

- BEFORE: code at commit `1f9f74a` (phase 3 as merged for review).
- AFTER: step A (Gaussian prior sigma=0.5, result_lag 150 min, season-L
  weakest-third baseline). 315 fits per w value, 2097 predicted matches.

## Mean log-score per history bin

| w | bin | n | BEFORE | AFTER |
|---|---|---|---|---|
| 0.5 | 0 | 13 | -2.917 | -2.844 |
| 0.5 | 1-5 | 63 | **-3.240** | **-3.039** |
| 0.5 | 6-15 | 62 | -2.881 | -2.849 |
| 0.5 | >15 | 1959 | -2.930 | -2.921 |
| 1.0 | 0 | 13 | -2.927 | -2.825 |
| 1.0 | 1-5 | 63 | **-9.839** | **-3.067** |
| 1.0 | 6-15 | 62 | -2.945 | -2.863 |
| 1.0 | >15 | 1959 | -2.964 | -2.939 |

The 1-5-match bin was the failure mode from the review (free
attack/defence after 1-5 matches, lambda ~ 0 at w=1): the prior removes
the blow-up (w=1: -9.839 -> -3.067, in line with the other bins) and
improves the thin-history bin at w=0.5 (-3.240 -> -3.039) without
hurting established teams: the 0-match bin (-2.917 -> -2.844 at w=0.5,
-2.927 -> -2.825 at w=1), the 6-15 bin (-2.881 -> -2.849, -2.945 ->
-2.863) and the >15 bin (-2.930 -> -2.921, -2.964 -> -2.939) are all
slightly BETTER after the change - the ridge toward 0 is mild.

## Model-vs-market slope (upcoming matches with totals 2.5 snapshots)

OLS slope of the de-vigged market P(over 2.5) on the model probability,
n=30 matches, as_of=now (same command, market section):

- BEFORE: slope 0.744, corr 0.887
- AFTER: slope **0.872**, corr 0.907

Closer to 1 as expected: the regularized model is less overconfident on
thin-history teams, so its spread tracks the market's better.
