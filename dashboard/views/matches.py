"""Страница 1: Матчи и справедливые цены."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from dashboard.api_client import ApiClient

LEAGUES = {
    "Все лиги": None,
    "Англия — Премьер-лига (EPL)": "epl",
    "Испания — Ла Лига": "la_liga",
    "Италия — Серия А": "serie_a",
    "Германия — Бундеслига": "bundesliga",
    "Франция — Лига 1": "ligue_1",
    "Лига чемпионов": "ucl",
}


def render_matches_view(api: ApiClient) -> None:
    st.header("⚽ Матчи и справедливые цены")
    st.caption(
        "Расчёт справедливых рыночных котировок из девигованной закрывающей линии "
        "острых букмекеров (SPEC v3 §4)."
    )

    # Filters
    col1, col2, col3 = st.columns([2, 1, 2])
    with col1:
        league_choice = st.selectbox("Лига", list(LEAGUES.keys()), index=0)
        league_filter = LEAGUES[league_choice]
    with col2:
        days = st.slider("Горизонт (дней)", min_value=1, max_value=30, value=7)
    with col3:
        search_query = st.text_input("Поиск по названию команды", "")

    try:
        data = api.get_matches(days=days, league=league_filter, search=search_query or None)
        matches = data.get("matches", [])
    except Exception as exc:
        st.error(f"Ошибка загрузки матчей из API: {exc}")
        return

    if not matches:
        st.info("Матчей по заданным фильтрам не найдено.")
        return

    # Match selector
    options = {
        f"[{m['date_utc']}] {m['home_team']} — {m['away_team']} ({m['league'].upper()})": m["id"]
        for m in matches
    }
    selected_label = st.selectbox("Выберите матч для анализа", list(options.keys()))
    selected_id = options[selected_label]

    # Selected match card
    match_info = next(m for m in matches if m["id"] == selected_id)
    st.subheader(f"{match_info['home_team']} — {match_info['away_team']}")

    c_meta1, c_meta2, c_meta3 = st.columns(3)
    c_meta1.metric("Дата матча", str(match_info["date_utc"]))
    c_meta2.metric("Лига", match_info["league"].upper())
    has_lines = match_info.get("has_market_lines", False)
    c_meta3.markdown(
        f"**Линии БК:** {'🟢 Доступны' if has_lines else '🟡 Ожидание линий'}"
    )

    if not has_lines:
        st.warning(
            "Для данного матча котировки острых букмекеров ещё не получены. "
            "Справедливая матрица строится только при наличии предматчевых или closing odds."
        )
        return

    # Load fair prices
    try:
        fair_data = api.get_fair_prices(selected_id)
    except Exception as exc:
        st.error(f"Не удалось рассчитать справедливые цены: {exc}")
        return

    # 1. 1X2 & BTTS
    col_1x2, col_btts = st.columns(2)
    with col_1x2:
        st.markdown("### 🏆 1X2 (Исход матча)")
        h2h = fair_data.get("h2h", {})
        p_1 = f"{h2h.get('home_prob', 0) * 100:.1f}%"
        p_x = f"{h2h.get('draw_prob', 0) * 100:.1f}%"
        p_2 = f"{h2h.get('away_prob', 0) * 100:.1f}%"
        h_df = pd.DataFrame(
            [
                {"Исход": "1 (П1)", "Кэф": h2h.get("home_fair_odds"), "P": p_1},
                {"Исход": "X (Ничья)", "Кэф": h2h.get("draw_fair_odds"), "P": p_x},
                {"Исход": "2 (П2)", "Кэф": h2h.get("away_fair_odds"), "P": p_2},
            ]
        )
        st.dataframe(h_df, hide_index=True, use_container_width=True)

    with col_btts:
        st.markdown("### 🥅 Обе забьют (BTTS)")
        btts = fair_data.get("btts", {})
        p_by = f"{btts.get('yes_prob', 0) * 100:.1f}%"
        p_bn = f"{btts.get('no_prob', 0) * 100:.1f}%"
        b_df = pd.DataFrame(
            [
                {"Исход": "Да (GG)", "Кэф": btts.get("yes_fair_odds"), "P": p_by},
                {"Исход": "Нет (NG)", "Кэф": btts.get("no_fair_odds"), "P": p_bn},
            ]
        )
        st.dataframe(b_df, hide_index=True, use_container_width=True)

    # 2. Totals (half and whole lines)
    st.markdown("### 📈 Тоталы голов (включая возврат ставки)")
    totals = fair_data.get("totals", [])
    if totals:
        t_rows = []
        for t in totals:
            t_rows.append(
                {
                    "Линия": f"{t['line']:g}",
                    "Больше (Кэф)": t["over_fair_odds"],
                    "P(Больше)": f"{t['over_prob'] * 100:.1f}%",
                    "P(Возврат)": f"{t['push_prob'] * 100:.1f}%" if t["push_prob"] > 0 else "—",
                    "Меньше (Кэф)": t["under_fair_odds"],
                    "P(Меньше)": f"{t['under_prob'] * 100:.1f}%",
                }
            )
        st.dataframe(pd.DataFrame(t_rows), hide_index=True, use_container_width=True)

    # 3. Exact score grid
    st.markdown("### 🎯 Точный счёт и исход 'inny'")
    col_scores, col_inny = st.columns([3, 1])
    with col_scores:
        exact_scores = fair_data.get("exact_score", [])
        if exact_scores:
            score_rows = [
                {
                    "Счёт": s["score"],
                    "Справедливый коэфф.": s["fair_odds"],
                    "Вероятность": f"{s['prob'] * 100:.2f}%",
                }
                for s in exact_scores
            ]
            st.dataframe(pd.DataFrame(score_rows), height=250, use_container_width=True)

    with col_inny:
        inny = fair_data.get("inny", {})
        st.info(
            f"**Исход 'inny' (>4 голов):**\n\n"
            f"• Справедливый кэф: **{inny.get('fair_odds', '—')}**\n\n"
            f"• Вероятность: **{inny.get('prob', 0) * 100:.2f}%**\n\n"
            f"Рассчитан как остаток вероятности сетки 5x5."
        )

    # 4. Methodological caveats
    st.markdown("### 📋 Методологические оговорки")
    caveats = fair_data.get("methodological_caveats", [])
    for c in caveats:
        st.markdown(f"- *{c}*")
