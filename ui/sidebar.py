"""Sidebar: scoreboard, data source, tracking controls, data health, dry-run and model. Owner: UI lead.

Rendered in two parts: the settings first (the tabs need them), and the scoreboard and data health
after the tabs, so they show what this run just did. Positions in the sidebar are fixed by the
containers below, not by call order.
"""

from __future__ import annotations

import streamlit as st

from meridian import client, llm

from . import state


_score_slot = None   # set each run by render_data_source()
_health_slot = None


def render_data_source() -> None:
    global _score_slot, _health_slot
    ss = st.session_state
    with st.sidebar:
        _score_slot, settings_slot, _health_slot = st.container(), st.container(), st.container()
    with settings_slot:
        st.divider()
        st.radio("Data source", state.DATA_SOURCES, key="data_source",
                 help="Fake data: snapshot loads with templated text (no network, no model calls). "
                      "Snapshots: demo mode, snapshot loads with real model text. Live API: the real TMS.")
        state.apply_data_source()

        # Tracking controls: the board and the fleet map both read the same tracker.
        ss.setdefault("track_refresh", 0)
        ss.setdefault("track_outlook", True)
        modes = ["Latest TMS activity"] + (["Now"] if ss.data_source == "Live API" else [])
        if ss.get("track_as_of_mode") not in modes:
            ss.track_as_of_mode = modes[0]
        st.radio("As of", modes, key="track_as_of_mode", horizontal=True,
                 help="Latest TMS activity: the moment the data describes. Now: the current clock (live API only).")
        c1, c2 = st.columns([1, 2])
        if c1.button("↻", help="Re-read loads and check calls from the TMS."):
            ss.track_refresh += 1
        c2.toggle("Weather & traffic", key="track_outlook",
                  help="Check the NWS forecast and TomTom traffic along each load's remaining route, at the hour "
                       "the truck should be there, and add what they cost to a predicted ETA.")
        st.divider()
        st.markdown(f"**Dry-run:** {':green[ON]' if client.dry_run() else ':red[OFF — writes are real]'}")
        st.markdown(f"**Model:** `{llm.MODEL}`")


def note_health(*notes: str) -> None:
    """Data problems found during this run (failed API calls, missing keys), shown in the sidebar."""
    health = st.session_state.setdefault("data_health", [])
    health.extend(n for n in notes if n and n not in health)


def render_scoreboard() -> None:
    with _score_slot:
        st.subheader("Today's scoreboard")
        sb = st.session_state.scoreboard
        # Time saved and quotes below margin are no longer shown: the manual baseline isn't measured and
        # email-to-quote is out of scope. state.scoreboard still tracks them for the unused tabs.
        c1, c2 = st.columns(2)
        c1.metric("Items done", sb["items_done"])
        c2.metric("Issues caught", sb["issues_caught"])
    health = st.session_state.pop("data_health", [])
    with _health_slot:
        if health:
            with st.expander(f"⚠️ Data health ({len(health)})"):
                for n in health:
                    st.caption(n)
        else:
            st.caption("✓ No data problems this run.")
