# Part 3: FROZEN config (xi=0.002, w=0.5) on TEST (2025/26-2026/27)

- predictions: 2111
- exact-score log loss: 2.9157

## Log loss by history bin (shallower team, 400d)

| bin | n | mean log loss |
|---|---|---|
| 0 | 28 | 2.7220 |
| 1-5 | 131 | 3.1294 |
| 6-15 | 158 | 2.8576 |
| >15 | 1794 | 2.9083 |

## Brier scores

| market | n | Brier | base rate |
|---|---|---|---|
| O/U 1.5 | 2111 | 0.1741 | 0.770 |
| O/U 2.5 | 2111 | 0.2419 | 0.542 |
| O/U 3.5 | 2111 | 0.2079 | 0.312 |
| BTTS | 2111 | 0.2455 | 0.546 |

## Per league

| league | n | log loss |
|---|---|---|
| bundesliga | 366 | 3.1053 |
| la_liga | 470 | 2.8188 |
| ligue_1 | 370 | 3.0323 |
| premier_league | 452 | 2.9011 |
| serie_a | 453 | 2.7825 |
