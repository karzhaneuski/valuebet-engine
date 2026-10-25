# Part 1: baseline config (xi=0.005, w=0.5) on validation (2023/24-2024/25)

- predictions: 3504
- exact-score log loss: 2.9148

## Log loss by history bin (shallower team, 400d)

| bin | n | mean log loss |
|---|---|---|
| 0 | 26 | 2.5986 |
| 1-5 | 136 | 3.0138 |
| 6-15 | 249 | 2.8529 |
| >15 | 3093 | 2.9181 |

## Brier scores

| market | n | Brier | base rate |
|---|---|---|---|
| O/U 1.5 | 3504 | 0.1675 | 0.781 |
| O/U 2.5 | 3504 | 0.2393 | 0.541 |
| O/U 3.5 | 3504 | 0.2098 | 0.326 |
| BTTS | 3504 | 0.2447 | 0.555 |

## Per league

| league | n | log loss |
|---|---|---|
| bundesliga | 612 | 3.0682 |
| la_liga | 760 | 2.8037 |
| ligue_1 | 612 | 2.9492 |
| premier_league | 760 | 3.0114 |
| serie_a | 760 | 2.7781 |

- one full validation walk-forward: 30.0s wall clock, 354 (league, as_of) refit groups, 3504 predicted matches (models cached under .cache/bt_models)
- run: DATABASE_URL=sqlite:////tmp/bt.db uv run python -m scripts.backtest_run --tag baseline --period validation
