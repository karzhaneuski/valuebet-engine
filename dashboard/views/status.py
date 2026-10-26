"""Страница 5: Состояние данных и квоты источников."""

from __future__ import annotations

import streamlit as st

from dashboard.api_client import ApiClient


def render_status_view(api: ApiClient) -> None:
    st.header("🖥️ Состояние данных и квоты")
    st.caption(
        "Мониторинг свежести рыночных данных, лимитов внешних API "
        "и лестницы деградации опроса (SPEC v3 §8)."
    )

    try:
        health = api.get_health()
    except Exception as exc:
        st.error(f"Сервис API недоступен по адресу {api.base_url}: {exc}")
        return

    # Status indicators
    c1, c2, c3 = st.columns(3)
    c1.metric("Статус сервиса", health.get("status", "unknown").upper())
    c2.metric("Версия системы", health.get("version", "0.3.0"))
    db_conn = health.get("database_connected", False)
    c3.markdown(f"**База данных:** {'🟢 Подключена' if db_conn else '🔴 Ошибка'}")

    st.markdown("### 📡 Свежесть рыночных котировок")
    last_snap = health.get("last_odds_snapshot_utc")
    if last_snap:
        st.success(f"Последний снимок линий зафиксирован: **{last_snap}** UTC")
    else:
        st.warning("В базе данных ещё нет зафиксированных снимков live-котировок TOA.")

    # Quota section
    st.markdown("### 💳 Расход квот провайдеров")
    toa_rem = health.get("toa_credits_remaining")
    if toa_rem is not None:
        toa_used = 500 - toa_rem
        st.markdown("**The Odds API (лимит 500 кредитов/месяц):**")
        progress = max(0.0, min(1.0, toa_used / 500.0))
        st.progress(progress)
        st.caption(f"Использовано {toa_used} из 500 кредитов (остаток: **{toa_rem}**).")

        # Ladder status sourced directly from backend /health
        p_stage = health.get("pacing_stage", 0)
        p_degraded = health.get("pacing_degraded", False)
        p_actions = health.get("pacing_actions", [])

        if p_stage >= 3 or (toa_used >= 450):
            st.error(
                "🚨 **Режим жесткой блокировки:** лимит квоты исчерпан (стадия 3). "
                "Платные запросы заблокированы."
            )
        elif p_degraded:
            actions_text = (
                ", ".join(p_actions) if p_actions else "активны ограничения лестницы деградации"
            )
            st.warning(f"⚠️ **Режим деградации (стадия {p_stage}):** {actions_text}.")
        else:
            st.info("🟢 **Штатный режим:** опрос выполняется по расписанию тиров без ограничений.")

    # Search quotas
    search_q = health.get("search_quota", {})
    if search_q:
        st.markdown("### 🔎 Квоты поисковых систем новостей")
        sq1, sq2 = st.columns(2)
        sq1.metric("Tavily API (запросов в мес.)", search_q.get("tavily_credits_used", 0))
        sq2.metric("Brave Search (запросов в мес.)", search_q.get("brave_credits_used", 0))
        st.caption(f"Учётный период: {search_q.get('period', '')}")

    st.markdown("---")
    st.markdown("### ⚙️ Конфигурация клиента")
    tok_status = "Задан (Bearer)" if api.token else "Отсутствует"
    st.code(f"API Base URL: {api.base_url}\nAPI Token: {tok_status}", language="yaml")
