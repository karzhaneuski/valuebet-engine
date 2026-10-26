"""Страница 2: Оценка котировок Fortuna."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from dashboard.api_client import ApiClient

EXAMPLE_FORTUNA_TEXT = """1:0 8.50
2:0 11.00
2:1 9.20
1:1 6.80
inny 15.00
+2.5 1.95
-2.5 1.85
1 2.10
X 3.40
2 3.60
"""


def render_fortuna_view(api: ApiClient) -> None:
    st.header("🔍 Оценка цен Fortuna")
    st.caption(
        "Сопоставление котировок польского букмекера Fortuna со справедливой рыночной "
        "матрицей (SPEC v3 §5). Отметка исходов с ценой выше справедливой."
    )

    # Match list for quick selection
    try:
        data = api.get_matches(days=14)
        matches = data.get("matches", [])
    except Exception:
        matches = []

    match_options = {"— Ввести команды вручную —": None}
    for m in matches:
        match_options[f"[{m['date_utc']}] {m['home_team']} — {m['away_team']}"] = m["id"]

    col_match, col_text = st.columns([1, 2])
    with col_match:
        chosen_match_label = st.selectbox("Матч", list(match_options.keys()))
        chosen_match_id = match_options[chosen_match_label]
        query_text = ""
        if chosen_match_id is None:
            query_text = st.text_input("Название команд (например, 'Arsenal - Chelsea')", "")

    with col_text:
        _col_btn1, col_btn2 = st.columns([3, 1])
        with col_btn2:
            if st.button("Вставить пример"):
                st.session_state["fortuna_input"] = EXAMPLE_FORTUNA_TEXT

        raw_text = st.text_area(
            "Котировки Fortuna (вставьте текст со страницы события)",
            value=st.session_state.get("fortuna_input", ""),
            height=200,
            placeholder="1:0 8.50\n2:0 11.00\n+2.5 1.95\n...",
        )

    if st.button("Оценить котировки", type="primary"):
        if not raw_text.strip():
            st.warning("Пожалуйста, введите текст котировок Fortuna.")
            return

        if chosen_match_id is None and not query_text.strip():
            st.warning("Пожалуйста, выберите матч или укажите названия команд.")
            return

        try:
            with st.spinner("Анализ маржи и сравнение со справедливой матрицей..."):
                eval_res = api.evaluate_fortuna(
                    raw_text=raw_text,
                    match_id=chosen_match_id,
                    query=query_text or None,
                )
        except Exception as exc:
            st.error(f"Ошибка при анализе котировок: {exc}")
            return

        # Results header
        st.success(f"Анализ завершён для матча: **{eval_res.get('match_name')}**")

        # Section margins
        margins = eval_res.get("section_margins", {})
        if margins:
            st.markdown("### 📊 Маржа букмекера по секциям")
            m_cols = st.columns(len(margins))
            for i, (sec, val) in enumerate(margins.items()):
                m_cols[i].metric(sec, f"{val * 100:.1f}%")

            if margins.get("exact_score", 0) > 0.25:
                st.info(
                    "💡 **Обратите внимание:** маржа на рынке точного счёта составляет "
                    f"**{margins.get('exact_score', 0) * 100:.1f}%**. "
                    "При такой марже математический перевес встречается крайне редко."
                )

        # Evaluations
        evaluations = eval_res.get("evaluations", [])
        if evaluations:
            st.markdown("### 📋 Результаты сопоставления")
            rows = []
            for ev in evaluations:
                status_icon = "🟢 ВЫШЕ" if ev["price_above_fair"] else "🔴 Ниже"
                rows.append(
                    {
                        "Рынок": ev["market"],
                        "Исход": ev["selection"],
                        "Кэф Fortuna": f"{ev['bookmaker_odds']:.2f}",
                        "Справедливый кэф": f"{ev['fair_odds']:.2f}",
                        "Справедливая P": f"{ev['fair_prob'] * 100:.1f}%",
                        "EV (перевес)": f"{ev['edge_pct']:+.1f}%",
                        "Статус": f"{status_icon} справедливой",
                    }
                )
            ev_df = pd.DataFrame(rows)
            st.dataframe(ev_df, use_container_width=True)

            above_count = eval_res.get("prices_above_fair_count", 0)
            if above_count > 0:
                st.success(f"Найдено котировок выше справедливой цены: **{above_count}**")
            else:
                st.warning("Котировок выше справедливой цены не обнаружено.")

        # Disclaimer
        st.markdown("---")
        st.caption(f"ℹ️ {eval_res.get('disclaimer', '')}")
