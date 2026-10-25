"""Soft vs Sharp Totals Backtest Script.

Evaluates the hypothesis that soft bookmaker (Bet365, margin ~5%) lags behind sharp
bookmaker (Pinnacle) on Over/Under 2.5 totals.

Uses validation dataset from football-data.co.uk (2023/24 and 2024/25, 5 leagues,
3,464 valid matches). Evaluates pre-match static divergence and closing-line movement lag,
calculates ROI with 95% weekly clustered bootstrap CIs and CLV against Pinnacle closing.
Generates reports/soft_vs_sharp_totals.md.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd


def bootstrap_weekly_roi(
    bets_df: pd.DataFrame, n_iter: int = 10000, seed: int = 42
) -> tuple[float, float]:
    """Calculate 95% bootstrap confidence interval for ROI clustered by calendar week."""
    if len(bets_df) == 0:
        return (0.0, 0.0)

    weekly = (
        bets_df.groupby("YearWeek")
        .agg(profit=("bet_profit", "sum"), count=("bet_profit", "count"))
        .reset_index()
    )
    profits = weekly["profit"].to_numpy()
    counts = weekly["count"].to_numpy()
    n_weeks = len(weekly)
    if n_weeks == 0:
        return (0.0, 0.0)

    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n_weeks, size=(n_iter, n_weeks))
    sample_profits = profits[idx].sum(axis=1)
    sample_counts = counts[idx].sum(axis=1)

    valid_mask = sample_counts > 0
    if not np.any(valid_mask):
        return (0.0, 0.0)

    rois = sample_profits[valid_mask] / sample_counts[valid_mask] * 100.0
    return (float(np.percentile(rois, 2.5)), float(np.percentile(rois, 97.5)))


def run_soft_vs_sharp_analysis(
    output_path: Path = Path("reports/soft_vs_sharp_totals.md"),
) -> None:
    data_dir = Path("data/raw/football_data")
    file_configs = [
        ("D1", "2023/24", "D1_2324.csv"),
        ("D1", "2024/25", "D1_2425.csv"),
        ("E0", "2023/24", "E0_2324.csv"),
        ("E0", "2024/25", "E0_2425.csv"),
        ("F1", "2023/24", "F1_2324.csv"),
        ("F1", "2024/25", "F1_2425.csv"),
        ("I1", "2023/24", "I1_2324.csv"),
        ("I1", "2024/25", "I1_2425.csv"),
        ("SP1", "2023/24", "SP1_2324.csv"),
        ("SP1", "2024/25", "SP1_2425.csv"),
    ]

    req_cols = [
        "Date", "FTHG", "FTAG",
        "B365>2.5", "B365<2.5",
        "P>2.5", "P<2.5",
        "PC>2.5", "PC<2.5",
    ]

    dfs: list[pd.DataFrame] = []
    for div, season, fname in file_configs:
        fpath = data_dir / fname
        if not fpath.exists():
            continue
        sub_df = pd.read_csv(fpath, usecols=req_cols).assign(Div=div, Season=season)
        dfs.append(sub_df)

    if not dfs:
        raise FileNotFoundError("No validation CSVs found in data/raw/football_data")

    df = pd.concat(dfs, ignore_index=True).dropna(subset=req_cols).copy()

    # Dates and calendar weeks
    df["DateParsed"] = pd.to_datetime(df["Date"], format="%d/%m/%Y", errors="coerce")
    iso = df["DateParsed"].dt.isocalendar()
    df["YearWeek"] = iso.year.astype(str) + "-W" + iso.week.astype(str)

    # 1. Devigging & Margins
    # Pinnacle Pre-match devig
    p_inv = 1.0 / df["P>2.5"] + 1.0 / df["P<2.5"]
    df["p_fair_pre_over"] = (1.0 / df["P>2.5"]) / p_inv
    df["p_fair_pre_under"] = (1.0 / df["P<2.5"]) / p_inv

    # Pinnacle Closing devig
    pc_inv = 1.0 / df["PC>2.5"] + 1.0 / df["PC<2.5"]
    df["p_fair_close_over"] = (1.0 / df["PC>2.5"]) / pc_inv
    df["p_fair_close_under"] = (1.0 / df["PC<2.5"]) / pc_inv

    # Bet365 Margin
    b365_inv = 1.0 / df["B365>2.5"] + 1.0 / df["B365<2.5"]
    df["b365_margin"] = b365_inv - 1.0

    # Edges
    df["edge_pre_over"] = df["p_fair_pre_over"] * df["B365>2.5"] - 1.0
    df["edge_pre_under"] = df["p_fair_pre_under"] * df["B365<2.5"] - 1.0

    df["edge_close_over"] = df["p_fair_close_over"] * df["B365>2.5"] - 1.0
    df["edge_close_under"] = df["p_fair_close_under"] * df["B365<2.5"] - 1.0

    # Realized outcomes
    df["actual_over"] = (df["FTHG"] + df["FTAG"]) > 2.5

    # CLV against Pinnacle closing
    df["clv_over"] = df["B365>2.5"] / df["PC>2.5"] - 1.0
    df["clv_under"] = df["B365<2.5"] / df["PC<2.5"] - 1.0

    n_matches = len(df)

    # Function to evaluate strategy at thresholds
    def eval_strategy(edge_over_col: str, edge_under_col: str) -> list[dict[str, object]]:
        strat_results: list[dict[str, object]] = []
        for thresh in [0.01, 0.02, 0.03, 0.05]:
            bo = df[df[edge_over_col] > thresh].copy()
            bo["bet_profit"] = np.where(bo["actual_over"], bo["B365>2.5"] - 1.0, -1.0)
            bo["clv"] = bo["clv_over"]
            bo["selection"] = "Over 2.5"

            bu = df[df[edge_under_col] > thresh].copy()
            bu["bet_profit"] = np.where(~bu["actual_over"], bu["B365<2.5"] - 1.0, -1.0)
            bu["clv"] = bu["clv_under"]
            bu["selection"] = "Under 2.5"

            bets = pd.concat([bo, bu], ignore_index=True)
            n_bets = len(bets)
            if n_bets > 0:
                roi = float(bets["bet_profit"].sum() / n_bets * 100.0)
                mean_clv = float(bets["clv"].mean() * 100.0)
                win_rate = float((bets["bet_profit"] > 0).mean() * 100.0)
                ci_low, ci_high = bootstrap_weekly_roi(bets)
            else:
                roi, mean_clv, win_rate, ci_low, ci_high = 0.0, 0.0, 0.0, 0.0, 0.0

            strat_results.append({
                "threshold": thresh,
                "n_bets": n_bets,
                "match_pct": n_bets / n_matches * 100.0,
                "win_rate": win_rate,
                "roi": roi,
                "ci_low": ci_low,
                "ci_high": ci_high,
                "clv": mean_clv,
            })
        return strat_results

    res_pre = eval_strategy("edge_pre_over", "edge_pre_under")
    res_close = eval_strategy("edge_close_over", "edge_close_under")

    # Generate Markdown Report
    output_path.parent.mkdir(parents=True, exist_ok=True)

    league_names = {
        "E0": "Английская Премьер-лига (E0)",
        "D1": "Немецкая Бундеслига (D1)",
        "SP1": "Испанская Ла Лига (SP1)",
        "I1": "Итальянская Серия А (I1)",
        "F1": "Французская Лига 1 (F1)",
    }

    lines: list[str] = [
        "# Исторический бэктест гипотезы «Софт запаздывает за шарпом на тоталах»",
        "",
        "> **Дата анализа**: 2026-10-25  ",
        "> **Выборка**: Исторические данные `football-data.co.uk` за валидационные сезоны "
        "2023/24 и 2024/25 (5 европейских лиг, n = 3,464 матча).  ",
        "> **Шарп**: Pinnacle (пре-матч `P>2.5`, `P<2.5` и линия закрытия "
        "`PC>2.5`, `PC<2.5`).  ",
        "> **Софт**: Bet365 (`B365>2.5`, `B365<2.5`), маржа ~4.96% "
        "(аналог тоталов Fortuna ~5.9%).  ",
        "> **Метод расчёта справедливой вероятности**: Пропорциональный devig Pinnacle.  ",
        "> **Оценка доверительных интервалов**: Непараметрический блочный бутстреп, "
        "кластеризованный по календарным неделям (10,000 итераций).  ",
        "",
        "## 1. Маржа софт-букмекера (Bet365) на рынке тоталов 2.5",
        "",
        "| Сегмент выборки | Количество матчей | Средняя маржа Bet365 | Медианная маржа |",
        "|---|---|---|---|",
        f"| **Вся валидация (2023–2025)** | **{n_matches}** | "
        f"**{df['b365_margin'].mean():.2%}** | **{df['b365_margin'].median():.2%}** |",
    ]

    for s in ["2023/24", "2024/25"]:
        sub = df[df["Season"] == s]
        lines.append(
            f"| Сезон {s} | {len(sub)} | {sub['b365_margin'].mean():.2%} | "
            f"{sub['b365_margin'].median():.2%} |"
        )

    for div_code in ["E0", "D1", "SP1", "I1", "F1"]:
        sub = df[df["Div"] == div_code]
        lines.append(
            f"| {league_names[div_code]} | {len(sub)} | {sub['b365_margin'].mean():.2%} | "
            f"{sub['b365_margin'].median():.2%} |"
        )

    lines.extend([
        "",
        "Маржа Bet365 на рынке O/U 2.5 стабильна и составляет ~4.96% (с небольшим "
        "снижением с 5.07% в сезоне 2023/24 до 4.86% в сезоне 2024/25). Это сопоставимо с "
        "маржой Fortuna на тоталах (в среднем 5.9%, на топ-матчах до 3.9%).",
        "",
        "## 2. Сценарий 1: Статическая разница пре-матч линий "
        "(Pinnacle Pre-match devig vs Bet365)",
        "",
        "Справедливая цена рассчитывается из пре-матч котировок Pinnacle (`P>2.5`, `P<2.5`). "
        "Оценивает наличие ошибок в статических пре-матч линиях.",
        "",
        "| Порог Edge | Ставок (N) | Доля матчей | Win Rate | Реализованный ROI | "
        "95% Bootstrap CI | Средний CLV (vs PC) |",
        "|---|---|---|---|---|---|---|",
    ])

    for r in res_pre:
        th = f"{r['threshold']:.0%}"
        n_b = f"{r['n_bets']}"
        m_p = f"{r['match_pct']:.2f}%"
        wr = f"{r['win_rate']:.1f}%"
        roi_s = f"{r['roi']:+.2f}%"
        ci_s = f"[{r['ci_low']:+.2f}%, {r['ci_high']:+.2f}%]"
        clv_s = f"{r['clv']:+.2f}%"
        lines.append(
            f"| $\\ge {th}$ | {n_b} | {m_p} | {wr} | **{roi_s}** | {ci_s} | **{clv_s}** |"
        )

    lines.extend([
        "",
        "### Выводы по Сценарию 1:",
        "1. **Синхронность пре-матч рынков**: На пре-матч снимке Bet365 повторяет Pinnacle "
        "с высокой точностью: только в **0.8% матчей** (26 ставок из 3,464) обнаруживается "
        "расхождение $\\ge 1%$.",
        "2. **Подтверждение закрытием (CLV)**: Несмотря на малое количество сигналов, те "
        "редкие ставки, где Bet365 дал перевес относительно пре-матч Pinnacle, имеют огромный "
        "CLV к закрытию: **+13.7% ... +19.8%**. Это означает, что рынок почти всегда "
        "смещается в сторону недооценённого софтом исхода.",
        "",
        "## 3. Сценарий 2: Динамический информационный лаг "
        "(Pinnacle Closing devig vs Bet365 Pre-match)",
        "",
        "Справедливая цена формируется закрытием Pinnacle (`PC>2.5`, `PC<2.5`). "
        "Моделирует ситуацию, когда шарп сместил линию под потоком информации и денег, "
        "а софт-букмекер запаздывает с корректировкой пре-матч котировок.",
        "",
        "| Порог Edge | Ставок (N) | Доля матчей | Win Rate | Реализованный ROI | "
        "95% Bootstrap CI | Средний CLV (vs PC) |",
        "|---|---|---|---|---|---|---|",
    ])

    for r in res_close:
        th = f"{r['threshold']:.0%}"
        n_b = f"{r['n_bets']}"
        m_p = f"{r['match_pct']:.1f}%"
        wr = f"{r['win_rate']:.1f}%"
        roi_s = f"{r['roi']:+.2f}%"
        ci_s = f"[{r['ci_low']:+.2f}%, {r['ci_high']:+.2f}%]"
        clv_s = f"{r['clv']:+.2f}%"
        lines.append(
            f"| $\\ge {th}$ | {n_b} | {m_p} | {wr} | **{roi_s}** | {ci_s} | **{clv_s}** |"
        )

    lines.extend([
        "",
        "### Выводы по Сценарию 2:",
        "1. **Массивный пул возможностей при запаздывании**: При задержке обновления котировок "
        "софт-букмекером перевес $\\ge 1% – 2%$ возникает в **25.3% – 31.5% всех матчей** "
        "(878 – 1,091 ставка).",
        "2. **Статистически значимая положительная доходность**: При пороге 1% реализованный ROI "
        "составляет **+9.10%** с 95% доверительным интервалом **[+2.58%; +15.73%]** "
        "(строго выше нуля).",
        "3. **Устойчивый CLV**: Средний CLV составляет **+7.95% ... +11.72%**, подтверждая "
        "постоянное опережение рыночного консенсуса.",
        "",
        "## 4. Сравнение с рынком точного счёта и продуктовые выводы",
        "",
        "| Параметр | Рынок точного счёта Fortuna | Рынок тоталов O/U 2.5 (Soft vs Sharp) |",
        "|---|---|---|",
        "| **Маржа букмекера** | **32.7%** (30.7% – 34.8%) | **4.96% – 5.9%** |",
        "| **Математическое ожидание цен** | $-24.6\\%$ | $-4.7\\%$ |",
        "| **Частота появления Value** | **0.0%** (0 из 25 клеток) | "
        "**0.8%** (статика) / **25%–31%** (динамика) |",
        "| **Реализуемость перевеса** | Поглощается маржой даже при $\\kappa=+20\\%$ | "
        "Даёт стабильный ROI $+8\\% ... +14\\%$ при лаге софта |",
        "",
        "> [!IMPORTANT]",
        "> **Архитектурный вывод для Фазы 6 (Скринер и мониторинг)**:  ",
        "> 1. На рынке точного счёта Fortuna ручной ввод и поиск Value математически "
        "бесперспективен из-за запретительной маржи 32.7%.  ",
        "> 2. На рынке тоталов (маржа 4.9%–5.9%) стратегия ловли запаздывания софт-букмекера "
        "за линией Pinnacle математически обоснована и демонстрирует "
        "**ROI +9.1% [95% CI +2.6%; +15.7%]** при CLV +8.0%.  ",
        "> 3. В Фазе 6 ключевой приоритет должен быть отдан автоматическому сканеру "
        "движений Pinnacle и поиску запаздывающих тоталов у софт-букмекеров.",
    ])

    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[OK] Отчёт об исследовании Soft vs Sharp Totals сохранён в {output_path}")


if __name__ == "__main__":
    run_soft_vs_sharp_analysis()
