"""Tab 3: Manual vs copilot, the productivity evidence on one screen. Owner: UI lead."""

from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from . import state


def render() -> None:
    b = state.load_baseline()
    ss = st.session_state
    triage, quote = b.get("morning_triage", {}), b.get("quote_from_email", {})

    scan = (f"0 clicks: board ranked in {ss.scan_seconds:.1f} s, + review" if ss.scan_seconds is not None
            else "0 clicks + review (open the Dispatch board)")
    build = f"1 paste + 1 approve, {ss.quote_seconds:.0f} s" if ss.quote_seconds is not None else "1 paste + 1 approve (build a quote)"
    st.table({
        "Task": ["Find what needs attention this morning", "Price and create a quote from an email", "Quotes sent below margin"],
        "Manual (measured)": [f"{triage.get('screens', '?')} screens, {triage.get('minutes', '?')} min",
                              f"{quote.get('screens', '?')} screens, {quote.get('minutes', '?')} min", "possible"],
        "Copilot": [scan, build, f"blocked: {ss.scoreboard['quotes_below_margin']}"],
    })
    if b.get("_placeholder"):
        st.caption(":orange[Manual numbers are placeholders until baseline.json is measured.]")
    st.caption(f"Method: time saved = measured manual time − (copilot run time + {state.REVIEW_MIN_PER_ITEM} min review), per item handled.")

    st.subheader("Time per task (minutes)")
    copilot_triage = (ss.scan_seconds or 0) / 60 + state.REVIEW_MIN_PER_ITEM * len(ss.risk_items[:10])
    copilot_quote = (ss.quote_seconds or 0) / 60 + state.REVIEW_MIN_PER_ITEM
    st.bar_chart(pd.DataFrame({"Manual": [triage.get("minutes", 0), quote.get("minutes", 0)],
                               "Copilot": [round(copilot_triage, 1), round(copilot_quote, 1)]},
                              index=["Morning triage", "Quote from email"]), stack=False)

    st.subheader("Eval results")
    if state.EVAL_RESULTS_PATH.exists():
        st.table(json.loads(state.EVAL_RESULTS_PATH.read_text(encoding="utf-8"))["summary"])
    else:
        st.caption("Run `python evals/run_evals.py` to fill this in.")
