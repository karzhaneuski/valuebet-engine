"""Verify Empirical Bias of 'inny' (Other Score) on Validation Matches.

Calculates the predicted probability of scores outside the 0..4 x 0..4 Fortuna grid
p_fair(inny) = 1 - sum(M[0..4, 0..4]) under FairMatrix (Pinnacle devigged market rates)
versus the actual empirical occurrence (max(FTHG, FTAG) >= 5) across all 3,462 valid
matches in the validation dataset (seasons 2023/24 and 2024/25, 5 leagues).

Reproduces:
- Mean predicted p_fair(inny): 4.74%
- Actual occurrence rate: 3.93% (136 matches)
- Binomial z-score: -2.25 (p = 0.024)
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from core.value.fair_matrix import (
    MarketInputs,
    MarketOdds1X2,
    MarketTotalsLine,
    build_fair_matrix,
)


def verify_inny_bias() -> dict[str, float]:
    data_dir = Path("data/raw/football_data")
    file_configs = [
        "D1_2324.csv", "D1_2425.csv",
        "E0_2324.csv", "E0_2425.csv",
        "F1_2324.csv", "F1_2425.csv",
        "I1_2324.csv", "I1_2425.csv",
        "SP1_2324.csv", "SP1_2425.csv",
    ]

    req_cols = ["FTHG", "FTAG", "P>2.5", "P<2.5", "PSH", "PSD", "PSA"]
    dfs: list[pd.DataFrame] = []
    for fname in file_configs:
        fpath = data_dir / fname
        if not fpath.exists():
            continue
        sub_df = pd.read_csv(fpath, usecols=req_cols)
        dfs.append(sub_df)

    if not dfs:
        raise FileNotFoundError("No validation CSVs found in data/raw/football_data")

    raw_df = pd.concat(dfs, ignore_index=True)
    for c in req_cols:
        raw_df[c] = pd.to_numeric(raw_df[c], errors="coerce")

    # Clean positive odds
    cond = (
        raw_df["FTHG"].notnull() & raw_df["FTAG"].notnull() &
        (raw_df["P>2.5"] > 1.0) & (raw_df["P<2.5"] > 1.0) &
        (raw_df["PSH"] > 1.0) & (raw_df["PSD"] > 1.0) & (raw_df["PSA"] > 1.0)
    )
    df = raw_df[cond].copy()
    n_matches = len(df)

    p_inny_list: list[float] = []
    actual_inny_list: list[int] = []

    for _, row in df.iterrows():
        inputs = MarketInputs(
            source="pinnacle",
            snapshot_time=dt.datetime(2024, 1, 1, tzinfo=dt.UTC),
            totals_by_book={
                "pinnacle": [
                    MarketTotalsLine(
                        line=2.5, over=float(row["P>2.5"]), under=float(row["P<2.5"])
                    )
                ]
            },
            h2h_by_book={
                "pinnacle": MarketOdds1X2(
                    home=float(row["PSH"]), draw=float(row["PSD"]), away=float(row["PSA"])
                )
            },
        )
        fm = build_fair_matrix(inputs)
        # inny = 1 - sum(M[0..4, 0..4])
        p_inny = 1.0 - float(fm.matrix[:5, :5].sum())
        p_inny_list.append(p_inny)

        # Actual: either team scored 5 or more goals
        is_inny = 1 if (row["FTHG"] >= 5 or row["FTAG"] >= 5) else 0
        actual_inny_list.append(is_inny)

    p_inny_arr = np.array(p_inny_list)
    act_arr = np.array(actual_inny_list)

    mean_p = float(np.mean(p_inny_arr))
    mean_act = float(np.mean(act_arr))
    diff = mean_act - mean_p
    # Exact standard error for Poisson binomial distribution with unequal probabilities p_i:
    # Var(sum) = sum(p_i * (1 - p_i)) -> SE(mean) = sqrt(sum(p_i * (1 - p_i))) / N
    se_poisson = float(np.sqrt(np.sum(p_inny_arr * (1.0 - p_inny_arr))) / n_matches)
    z_score = diff / se_poisson

    se_sample = float(np.std(act_arr - p_inny_arr) / np.sqrt(n_matches))
    z_sample = diff / se_sample

    overestimate_pct = -diff * 100.0

    print("=" * 70)
    print("Эмпирическая проверка смещения 'inny' (выборка валидации 2023–2025)")
    print("=" * 70)
    print(f"Количество валидационных матчей (N): {n_matches}")
    print(f"Фактическое число матчей с голами >= 5 у команды: {int(np.sum(act_arr))}")
    print(f"Фактическая доля (Actual):          {mean_act:.4f} ({mean_act * 100:.2f}%)")
    print(f"Средняя прогнозная вероятность p_fair: {mean_p:.4f} ({mean_p * 100:.2f}%)")
    print(f"Разница (Actual - Predicted):        {diff:+.4f} ({diff * 100:+.2f}%)")
    print(f"Стандартная ошибка SE (Poisson binom): {se_poisson:.5f}")
    print(f"z-score (SE = sqrt(sum p_i(1-p_i))/N): {z_score:.2f}")
    print(f"z-score (выборочный SE):             {z_sample:.2f}")
    print("=" * 70)
    print("Вывод: Рыночная пуассоновская матрица систематически переоценивает")
    print(
        f"вероятность счетов >= 5 на +{overestimate_pct:.2f} процентных пункта "
        f"({mean_p * 100:.2f}% vs {mean_act * 100:.2f}%, z = {z_score:.2f})."
    )
    print("Исход 'inny' строго справочный и не рекомендуется к ставкам.")
    print("=" * 70)

    return {
        "n_matches": float(n_matches),
        "actual": mean_act,
        "predicted": mean_p,
        "z_score": z_score,
        "z_sample": z_sample,
    }


if __name__ == "__main__":
    verify_inny_bias()
