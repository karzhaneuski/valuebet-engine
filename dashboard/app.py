"""ValueBet Engine Streamlit Dashboard.

SPEC v3:
- Fully analytical dashboard interacting EXCLUSIVELY via REST API (zero direct SQLite access).
- Russian language UI per SPEC §10.
- Five pages:
  1. Матчи и справедливые цены (Matches & Fair Prices)
  2. Оценка цен Fortuna (Fortuna Odds Evaluation)
  3. Мои ставки (User Bets Registration & History)
  4. Статистика и CLV (Transparent Portfolio Audit, Bootstrap CI, Variance Warning)
  5. Состояние данных (Data Freshness, TOA/Search Quotas, Degradation Ladder)
"""

from __future__ import annotations

import os

import streamlit as st

from dashboard.api_client import ApiClient
from dashboard.views.bets import render_bets_view
from dashboard.views.fortuna import render_fortuna_view
from dashboard.views.matches import render_matches_view
from dashboard.views.stats import render_stats_view
from dashboard.views.status import render_status_view

st.set_page_config(
    page_title="ValueBet Engine",
    page_icon="⚽",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Sidebar setup
st.sidebar.title("⚽ ValueBet Engine")
st.sidebar.caption("Аналитический движок справедливых футбольных цен (SPEC v3)")

# API configuration in session state
api_base = os.getenv("API_BASE_URL", "http://localhost:8000")
api_token = os.getenv("API_SECRET_TOKEN") or os.getenv("API_TOKEN", "")

with st.sidebar.expander("⚙️ Настройки API", expanded=False):
    configured_token = st.text_input("API Secret Token", value=api_token, type="password")
    api_url = st.text_input("API Base URL", value=api_base)

client = ApiClient(base_url=api_url, token=configured_token)

# Navigation
PAGE_MATCHES = "Матчи и справедливые цены"
PAGE_FORTUNA = "Оценка цен Fortuna"
PAGE_BETS = "Мои ставки"
PAGE_STATS = "Статистика и CLV"
PAGE_STATUS = "Состояние данных"

NAV_OPTIONS = [
    PAGE_MATCHES,
    PAGE_FORTUNA,
    PAGE_BETS,
    PAGE_STATS,
    PAGE_STATUS,
]

selected_page = st.sidebar.radio("Разделы", NAV_OPTIONS, index=0)

# Global Responsible Gaming Banner
st.sidebar.markdown("---")
st.sidebar.markdown(
    "⚠️ **Ответственная игра:**\n\n"
    "Сервис предоставляет исключительно справочную аналитику. "
    "Ставки сопряжены с риском потери средств. "
    "Полное отсутствие рекомендаций."
)

# Route to page view
if selected_page == PAGE_MATCHES:
    render_matches_view(client)
elif selected_page == PAGE_FORTUNA:
    render_fortuna_view(client)
elif selected_page == PAGE_BETS:
    render_bets_view(client)
elif selected_page == PAGE_STATS:
    render_stats_view(client)
elif selected_page == PAGE_STATUS:
    render_status_view(client)
