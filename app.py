"""Broker Desk Copilot: Streamlit entry point. Owner: UI lead.

Run:  streamlit run app.py
"""

import streamlit as st

from ui import board, sidebar, state, tracking

st.set_page_config(page_title="Broker Desk Copilot", page_icon="🚚", layout="wide")
state.init()
sidebar.render_data_source()

st.title("Broker Desk Copilot")

# The Dispatch board replaces the separate Load watch and Live tracking tabs: one row per load,
# merging both. ui/load_watch.py, ui/manual_vs_copilot.py and email-to-quote (ui/email_quote.py) stay
# in the repo, unused.
# Tabs track state (key) so a click on the board's queue or the fleet map can open Load details, and
# only the open tab runs.
TABS = ["Dispatch board", "Load details", "Fleet map & feed"]
tab_board, tab_load, tab_fleet = st.tabs(TABS, key="main_tab", on_change="rerun")
if tab_board.open:
    with tab_board:
        board.render()
if tab_load.open:
    with tab_load:
        board.render_details()
if tab_fleet.open:
    with tab_fleet:
        tracking.render()

# Last, so the scoreboard reflects what the tabs just did in this run.
sidebar.render_scoreboard()
