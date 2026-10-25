# Part 2: parameter tuning on validation (2023/24-2024/25)

All numbers: exact-score log loss on the validation walk-forward
(3504 matches; records cached under .cache/bt_records).

## Stage a: xi x w grid (prior_sigma=0.5)

| config | log loss | bin 1-5 | Brier O/U 2.5 |
|---|---|---|---|
| xi=0.002 w=0.5 | 2.9112 | 3.0133 | 0.2380 |
| xi=0.002 w=0.3 | 2.9114 | 3.0059 | 0.2382 |
| xi=0.0035 w=0.5 | 2.9121 | 3.0124 | 0.2386 |
| xi=0.0035 w=0.3 | 2.9123 | 3.0023 | 0.2388 |
| xi=0.002 w=0.7 | 2.9135 | 3.0246 | 0.2383 |
| xi=0.005 w=0.3 | 2.9147 | 3.0021 | 0.2393 |
| xi=0.005 w=0.5 | 2.9148 | 3.0138 | 0.2393 |
| xi=0.0035 w=0.7 | 2.9149 | 3.0273 | 0.2391 |
| xi=0.005 w=0.7 | 2.9183 | 3.0312 | 0.2398 |
| xi=0.0075 w=0.3 | 2.9196 | 3.0043 | 0.2400 |
| xi=0.0075 w=0.5 | 2.9203 | 3.0171 | 0.2401 |
| xi=0.002 w=1.0 | 2.9218 | 3.0529 | 0.2397 |
| xi=0.0075 w=0.7 | 2.9249 | 3.0368 | 0.2409 |
| xi=0.0035 w=1.0 | 2.9250 | 3.0623 | 0.2408 |
| xi=0.005 w=1.0 | 2.9304 | 3.0717 | 0.2419 |
| xi=0.0075 w=1.0 | 2.9399 | 3.0825 | 0.2435 |

## Stage b: prior_sigma on the top-3 xi/w points

| config | log loss | bin 1-5 |
|---|---|---|
| sigma=0.75 xi=0.002 w=0.3 | 2.9113 | 3.0072 |
| sigma=0.75 xi=0.002 w=0.5 | 2.9114 | 3.0162 |
| sigma=0.35 xi=0.002 w=0.5 | 2.9120 | 3.0152 |
| sigma=0.75 xi=0.0035 w=0.5 | 2.9125 | 3.0164 |
| sigma=0.35 xi=0.002 w=0.3 | 2.9126 | 3.0095 |
| sigma=0.35 xi=0.0035 w=0.5 | 2.9135 | 3.0140 |
| sigma=0.25 xi=0.002 w=0.5 | 2.9152 | 3.0205 |
| sigma=0.25 xi=0.002 w=0.3 | 2.9163 | 3.0165 |
| sigma=0.25 xi=0.0035 w=0.5 | 2.9188 | 3.0203 |

## Stage c: model options on xi=0.002, w=0.5

| config | log loss | bin 1-5 |
|---|---|---|
| opt_decay0.25 | 2.9107 | 3.0154 |
| opt_decay0.5 | 2.9109 | 3.0148 |
| opt_commonrho | 2.9110 | 3.0088 |
| opt_base_xi0.002_w0.5 | 2.9112 | 3.0133 |
| opt_xgscale | 2.9115 | 3.0161 |
| opt_levelgoals | 2.9157 | 3.0387 |

## Selection (paired week-clustered bootstrap, 2000 resamples)

- opt_decay0.25 vs opt_decay0.5: dLL -0.00014, SE 0.00012, CI95 [-0.00038, +0.00009] -> TIE
- opt_decay0.25 vs opt_commonrho: dLL -0.00023, SE 0.00076, CI95 [-0.00172, +0.00126] -> TIE
- opt_decay0.25 vs opt_base_xi0.002_w0.5: dLL -0.00049, SE 0.00037, CI95 [-0.00122, +0.00024] -> TIE
- opt_decay0.25 vs sig0.75_xi0.002_w0.3: dLL -0.00061, SE 0.00101, CI95 [-0.00258, +0.00136] -> TIE

**Chosen: `opt_base_xi0.002_w0.5`** (xi=0.002, w=0.5, prior_sigma=0.5, no options) - statistically tied with every tuned variant; the agreed rule prefers the simpler configuration. The best challenger (decay 0.25) improves the overall loss by only 0.0005 and the 1-5 bin by 0.0002 - far inside the bootstrap noise.

Effect of the choice on the 1-5 bin: chosen 3.0133 vs best variant 3.0088 (common rho) and 3.0154 (decay 0.25) - all within noise; the bin is dominated by prior quality, not by xi/w/decay.

Frozen: `reports/backtest/FROZEN_CONFIG.json` (config_hash `5090b95a35ff...`, commit `4575d5ee`). Test period unlocked from this point; parameters do not change anymore.
