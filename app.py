"""Broker Desk Copilot: Streamlit entry point. Owner: UI lead.

Run:  streamlit run app.py
"""

import streamlit as st

from ui import board, manual_vs_copilot, sidebar, state, tracking

st.set_page_config(page_title="Broker Desk Copilot", page_icon="🚚", layout="wide")
state.init()
sidebar.render_data_source()

st.title("Broker Desk Copilot")

# The Dispatch board replaces the separate Load watch and Live tracking tabs: one row per load,
# merging both. ui/load_watch.py and email-to-quote (ui/email_quote.py) stay in the repo, unused.
# Tabs track state (key) so a click on the fleet map can switch to the board, and only the open tab runs.
TABS = ["Dispatch board", "Fleet map & feed", "Manual vs copilot"]
tab_board, tab_fleet, tab_numbers = st.tabs(TABS, key="main_tab", on_change="rerun")
if tab_board.open:
    with tab_board:
        board.render()
if tab_fleet.open:
    with tab_fleet:
        tracking.render()
if tab_numbers.open:
    with tab_numbers:
        manual_vs_copilot.render()

# Last, so the scoreboard reflects what the tabs just did in this run.
sidebar.render_scoreboard()
