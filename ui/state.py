"""Session state, data source and scoreboard. Owner: UI lead.

Streamlit reruns the whole script on every click, so everything that must survive lives in st.session_state.
"""

from __future__ import annotations

import json
import os
import pathlib

import streamlit as st

ROOT = pathlib.Path(__file__).resolve().parent.parent
BASELINE_PATH = ROOT / "baseline.json"
EVAL_RESULTS_PATH = ROOT / "evals" / "results" / "latest.json"

DATA_SOURCES = ["Fake data", "Snapshots (demo mode)", "Live API"]
REVIEW_MIN_PER_ITEM = 0.5   # assumed broker review time per approved item; shown in the method note

DEFAULTS = {
    "data_source": "Fake data",
    "risk_items": [],          # list[RiskItem], ranked
    "done_ids": {},            # item id -> "approved" | "dismissed"
    "investigations": {},      # item id -> agent answer
    "scan_seconds": None,
    "loads_scanned": 0,
    "proposal": None,          # QuoteProposal
    "quote_seconds": None,
    "created_quote": None,     # result of create_quote
    "scoreboard": {"items_done": 0, "minutes_saved": 0.0, "issues_caught": 0, "quotes_below_margin": 0},
}


def init() -> None:
    for k, v in DEFAULTS.items():
        if k not in st.session_state:
            st.session_state[k] = json.loads(json.dumps(v)) if isinstance(v, (dict, list)) else v


def use_fake() -> bool:
    return st.session_state.data_source == "Fake data"


def apply_data_source() -> None:
    """meridian.client reads DEMO_MODE on every call, so switching takes effect immediately.
    Fake data reads the snapshots too, so the load watch and tracking show the same loads; what makes
    it "fake" is the templated text in place of model calls."""
    os.environ["DEMO_MODE"] = "0" if st.session_state.data_source == "Live API" else "1"


@st.cache_data
def load_baseline() -> dict:
    if BASELINE_PATH.exists():
        return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    return {}


def item_id(item) -> str:
    return f"{item.kind}-{item.record_type}-{item.record_id}"


def record_done(kind: str, run_seconds: float = 0.0) -> None:
    """kind: 'risk' or 'quote'. Time saved = baseline − (copilot run time + review time)."""
    b = load_baseline()
    manual = b.get("per_risk_item_minutes", 0) if kind == "risk" else b.get("quote_from_email", {}).get("minutes", 0)
    sb = st.session_state.scoreboard
    sb["items_done"] += 1
    sb["minutes_saved"] += max(0.0, manual - (run_seconds / 60 + REVIEW_MIN_PER_ITEM))
