"""Streamlit dashboard. Phase 1: stub page only.

Real pages (Value сейчас, Ручной ввод, Статистика, Эксперимент, Данные —
all in Russian per SPEC §10) arrive in phase 9; the dashboard talks only
to the REST API, never to the database directly.
"""

import streamlit as st

st.set_page_config(page_title="ValueBet Engine", page_icon="⚽")

st.title("ValueBet Engine")
st.caption("Статус: каркас (фаза 1). Страницы дашборда появятся в фазе 9.")

if st.button("Проверить API"):
    st.info("Здесь будет запрос к GET /health сервиса api (фаза 9).")
