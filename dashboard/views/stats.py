"""Страница 4: Статистика портфеля и аудит CLV."""

from __future__ import annotations

import plotly.graph_objects as go  # type: ignore[import-untyped]
import streamlit as st

from dashboard.api_client import ApiClient


def render_stats_view(api: ApiClient) -> None:
    st.header("📊 Статистика портфеля и аудит CLV")
    st.caption(
        "Честный финансовый аудит: P/L в единицах банкролла, ROI с бутстреп-интервалами, "
        "сравнение с closing line (CLV) и обязательное предупреждение о дисперсии (SPEC v3 §7)."
    )

    period_labels = {
        "all": "Все время",
        "month": "Последний месяц",
        "season": "Текущий сезон",
    }
    period = st.radio(
        "Период анализа",
        ["all", "month", "season"],
        format_func=lambda x: period_labels[x],
        horizontal=True,
    )

    try:
        perf = api.get_performance(period=period)
    except Exception as exc:
        st.error(f"Ошибка загрузки статистики из API: {exc}")
        return

    # Metrics row 1
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Всего ставок", perf.get("total_bets", 0))
    c2.metric("Рассчитано", perf.get("settled_bets", 0))
    c3.metric("В игре (open)", perf.get("pending_bets", 0))
    turnover = perf.get("turnover", 0.0)
    c4.metric("Оборот (ед.)", f"{turnover:.2f}")

    # Metrics row 2: P/L, ROI, CLV, Hit Rate
    pnl = perf.get("total_profit", 0.0)
    roi = perf.get("roi", 0.0)
    clv = perf.get("avg_clv", 0.0)
    hit_rate = perf.get("hit_rate", 0.0)

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Чистая прибыль (P/L)", f"{pnl:+.2f} ед.", delta=f"{pnl:+.2f}")

    ci_roi = perf.get("bootstrap_ci_roi_95")
    ci_roi_str = f"[{ci_roi['lower']:+.1f}% ... {ci_roi['upper']:+.1f}%]" if ci_roi else "—"
    m2.metric("ROI", f"{roi:+.2f}%", help=f"95% Bootstrap CI: {ci_roi_str}")

    ci_clv = perf.get("bootstrap_ci_clv_95")
    ci_clv_str = f"[{ci_clv['lower']:+.1f}% ... {ci_clv['upper']:+.1f}%]" if ci_clv else "—"
    m3.metric("Средний CLV", f"{clv:+.2f}%", help=f"95% Bootstrap CI: {ci_clv_str}")

    m4.metric("Hit Rate (проходимость)", f"{hit_rate:.1f}%")

    # Mandatory Sample Size Warning for N < 100
    warning = perf.get("sample_size_warning")
    settled_cnt = perf.get("settled_bets", 0)
    if warning or settled_cnt < 100:
        msg = warning or (
            f"⚠️ Внимание: выборка < 100 ставок (рассчитано {settled_cnt} ставок). "
            "Оценка ROI и CLV имеет высокую погрешность из-за случайных колебаний."
        )
        st.warning(msg)

    # Charts: Cumulative P/L and CLV
    st.markdown("### 📈 Динамика P/L и закрывающей линии (CLV)")
    try:
        bets_data = api.get_user_bets(limit=200)
        bets = bets_data.get("bets", [])
    except Exception:
        bets = []

    valid_statuses = ("won", "lost", "push")
    settled = [
        b for b in bets if b.get("status") in valid_statuses and b.get("pnl") is not None
    ]

    if settled:
        # Reverse to chronological order
        settled.sort(key=lambda x: x.get("placed_at_utc", ""))
        cum_pnl = []
        running_pnl = 0.0
        indices = list(range(1, len(settled) + 1))
        clv_vals = []

        for b in settled:
            running_pnl += float(b.get("pnl", 0.0))
            cum_pnl.append(running_pnl)
            clv_vals.append(float(b.get("clv") or 0.0))

        fig = go.Figure()
        fig.add_trace(
            go.Scatter(
                x=indices,
                y=cum_pnl,
                mode="lines+markers",
                name="Кумулятивный P/L (ед.)",
                line={"color": "#1f77b4" if running_pnl >= 0 else "#d62728", "width": 3},
            )
        )
        fig.add_hline(y=0.0, line_dash="dash", line_color="gray")
        fig.update_layout(
            title="Кумулятивная прибыль / убыток (P/L) по ставкам",
            xaxis_title="Номер ставки",
            yaxis_title="Прибыль / Убыток (ед.)",
            template="plotly_white",
        )
        st.plotly_chart(fig, use_container_width=True)

        # CLV distribution chart
        fig_clv = go.Figure()
        fig_clv.add_trace(
            go.Bar(
                x=indices,
                y=clv_vals,
                name="CLV ставки (%)",
                marker_color=["#2ca02c" if v >= 0 else "#d62728" for v in clv_vals],
            )
        )
        fig_clv.add_hline(y=0.0, line_dash="dash", line_color="gray")
        fig_clv.update_layout(
            title="Beat the Closing Line (CLV) по каждой ставке",
            xaxis_title="Номер ставки",
            yaxis_title="CLV (%)",
            template="plotly_white",
        )
        st.plotly_chart(fig_clv, use_container_width=True)
    else:
        st.info("Графики кумулятивного P/L и CLV появятся после расчёта первых ставок.")

    st.markdown("---")
    st.caption(f"ℹ️ {perf.get('disclaimer', '')}")
