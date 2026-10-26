"""Страница 3: Мои ставки (учёт и аудит ставок пользователя)."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from dashboard.api_client import ApiClient

MARKET_OPTIONS = [
    "exact_score",
    "totals",
    "1x2",
    "btts",
    "multiscore",
    "goals_range",
    "team_totals",
]


def render_bets_view(api: ApiClient) -> None:
    st.header("📝 Мои ставки")
    st.caption(
        "Регистрация ставок пользователя для последующего автоматического расчёта выигрышей "
        "и предматчевого аудита закрывающей линии (CLV per SPEC v3 §7)."
    )

    # Form to add bet
    with st.expander("➕ Записать новую ставку", expanded=True):
        try:
            m_data = api.get_matches(days=14)
            matches = m_data.get("matches", [])
        except Exception:
            matches = []

        if matches:
            match_opts = {
                f"[{m['date_utc']}] {m['home_team']} — {m['away_team']}": m["id"]
                for m in matches
            }
            c1, c2 = st.columns(2)
            with c1:
                chosen_match = st.selectbox("Матч", list(match_opts.keys()))
                match_id = match_opts[chosen_match]
                market = st.selectbox("Рынок", MARKET_OPTIONS)
                outcome = st.text_input("Исход (например, '2:1', 'over 2.5', '1', 'yes')", "1:0")

            with c2:
                odds = st.number_input(
                    "Коэффициент", min_value=1.01, max_value=200.0, value=2.00, step=0.05
                )
                stake = st.number_input(
                    "Сумма ставки (в единицах)", min_value=0.1, max_value=100.0, value=1.0, step=0.5
                )
                bookmaker = st.text_input("Букмекер", "Fortuna")

            if st.button("Записать ставку", type="primary"):
                try:
                    res = api.record_user_bet(
                        match_id=match_id,
                        market=market,
                        outcome=outcome,
                        odds=odds,
                        stake=stake,
                        bookmaker=bookmaker,
                    )
                    b_id = res.get("id")
                    b_st = res.get("status")
                    st.success(f"Ставка #{b_id} успешно записана со статусом '{b_st}'!")
                except Exception as exc:
                    st.error(f"Не удалось записать ставку: {exc}")
        else:
            st.info("Нет доступных матчей для добавления ставки.")

    # Bets table
    st.markdown("### 📋 История зарегистрированных ставок")
    status_filter = st.selectbox(
        "Фильтр по статусу",
        ["Все", "open", "won", "lost", "push", "void"],
        index=0,
    )
    api_status = None if status_filter == "Все" else status_filter

    try:
        bets_data = api.get_user_bets(status=api_status, limit=100)
        bets = bets_data.get("bets", [])
    except Exception as exc:
        st.error(f"Ошибка загрузки истории ставок: {exc}")
        return

    if not bets:
        st.info("В истории пока нет зарегистрированных ставок.")
        return

    rows = []
    for b in bets:
        pnl_val = b.get("pnl")
        pnl_str = f"{pnl_val:+.2f} ед." if pnl_val is not None else "—"
        clv_val = b.get("clv")
        clv_str = f"{clv_val:+.2f}%" if clv_val is not None else "—"
        odds_val = float(b.get("odds") or b.get("bookmaker_odds") or 0.0)
        stake_val = float(b.get("stake") or b.get("stake_units") or 0.0)

        rows.append(
            {
                "ID": b["id"],
                "Матч": b.get("match_name", f"Match_{b.get('match_id')}"),
                "Рынок": b.get("market", ""),
                "Исход": b.get("outcome", ""),
                "Кэф": f"{odds_val:.2f}",
                "Ставка": f"{stake_val:.2f}",
                "БК": b.get("bookmaker", "Fortuna"),
                "Статус": b.get("status", "open"),
                "P/L": pnl_str,
                "CLV": clv_str,
                "Источник CLV": b.get("clv_source") or "—",
            }
        )

    st.dataframe(pd.DataFrame(rows), use_container_width=True)
