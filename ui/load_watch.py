"""Tab 1: At-risk load watch. Owner: UI lead."""

from __future__ import annotations

import time

import streamlit as st

from meridian import client, geo, risk_playbook
from meridian.models import RiskItem

from . import state
from . import tracking as tracking_ui

SEVERITY_STYLE = {"HIGH": ":red-background[**HIGH**]", "MEDIUM": ":orange-background[**MEDIUM**]", "LOW": ":gray-background[**LOW**]"}


def _tiles() -> list[dict]:
    res = client.dashboard_counts()   # fake data reads the snapshot (state.apply_data_source)
    return res["data"]["tiles"] if res["ok"] else []


def _scan() -> None:
    t0 = time.perf_counter()
    with st.status("Scanning your loads…", expanded=True) as status:
        # LATE_ETA uses tracking (check calls + legal driving hours) as of the tracking tab's time;
        # every rule is judged on that same date. Fake data runs the same rules on the snapshot,
        # with templated text instead of model calls.
        tracked = tracking_ui.current_statuses()
        statuses, at = tracked if tracked else ({}, None)
        today = at.astimezone(geo.tz(tracking_ui.CLOCK_TZ)).date() if at else None
        items = risk_playbook.scan(on_step=status.write, tracking=statuses, today=today, templated=state.use_fake())
        loads = len({it.record_id for it in items})
        st.session_state.scan_as_of = at
        secs = time.perf_counter() - t0
        status.update(label=f"Copilot found {len(items)} issues across {loads} loads in {secs:.1f} s", state="complete", expanded=False)
    ss = st.session_state
    ss.risk_items, ss.scan_seconds, ss.loads_scanned = items, secs, loads
    ss.done_ids, ss.investigations = {}, {}
    ss.scoreboard["issues_caught"] = len(items)


def _approve(item: RiskItem, draft: str) -> None:
    if state.use_fake():
        res = {"ok": True, "dry_run": True}
    else:
        res = risk_playbook.act(item, "note", text=f"[Copilot] {item.explanation}\n\nDraft ({item.draft_audience}): {draft}")
    if res["ok"]:
        st.session_state.done_ids[state.item_id(item)] = "approved"
        state.record_done("risk", (st.session_state.scan_seconds or 0) / max(len(st.session_state.risk_items), 1))
        st.toast(f"Logged note on {item.record_id}" + (" (dry-run)" if res.get("dry_run") else ""))
    else:
        st.error(f"{item.record_id}: {res.get('detail') or res.get('error')}")


def _investigate(item: RiskItem) -> None:
    key = state.item_id(item)
    if state.use_fake():
        st.session_state.investigations[key] = f"(fake) The agent would look up {item.record_type} {item.record_id}, its carrier and facilities here."
        return
    try:
        from agent import investigate
        with st.spinner(f"Investigating {item.record_id}…"):
            st.session_state.investigations[key] = investigate(item.record_id, item.title)
    except Exception as e:  # keep the demo alive if the agent fails
        st.session_state.investigations[key] = f"Investigate failed: {e}"


def _card(item: RiskItem) -> None:
    key = state.item_id(item)
    with st.container(border=True):
        st.markdown(f"{SEVERITY_STYLE[item.severity]} &nbsp; **{item.title}** · {item.record_type} `{item.record_id}`")
        if item.explanation:
            st.markdown(f"**Why:** {item.explanation}")
        with st.expander("Evidence"):
            st.table({"field": list(item.evidence), "value": [str(v) for v in item.evidence.values()]})
        st.markdown(f"**Suggested action:** {item.suggested_action}")
        draft = st.text_area(f"Draft message to {item.draft_audience or '…'}", item.draft_message, key=f"draft-{key}", height=90)
        b1, b2, b3 = st.columns(3)
        if b1.button("✅ Approve & log note", key=f"ok-{key}", type="primary"):
            _approve(item, draft)
            st.rerun()
        if b2.button("🔍 Investigate", key=f"inv-{key}"):
            _investigate(item)
        if b3.button("⏭ Dismiss", key=f"skip-{key}"):
            st.session_state.done_ids[key] = "dismissed"
            st.rerun()
        if answer := st.session_state.investigations.get(key):
            st.info(answer)


def render() -> None:
    tiles = _tiles()
    if tiles:
        cols = st.columns(len(tiles))
        for col, t in zip(cols, tiles):
            col.metric(t["label"], t["count"])

    ss = st.session_state
    left, right = st.columns([1, 3])
    if left.button("Scan my loads", type="primary", use_container_width=True):
        _scan()
    if ss.scan_seconds is not None:
        right.markdown(f"**Copilot found {len(ss.risk_items)} issues across {ss.loads_scanned} loads in {ss.scan_seconds:.1f} s.**")
        if ss.get("scan_as_of"):
            right.caption(f"As of {geo.fmt_local(ss.scan_as_of, tracking_ui.CLOCK_TZ)}. Late-ETA items come from "
                          "check calls and our ETA at legal driving hours"
                          + (", plus the weather & traffic forecast along each route (Live tracking toggle)."
                             if ss.get("track_outlook", True) else "."))

    if not ss.risk_items:
        st.caption("Click **Scan my loads** to find what needs attention.")
        return

    f1, f2 = st.columns(2)
    sev = f1.pills("Severity", ["HIGH", "MEDIUM", "LOW"], selection_mode="multi", default=["HIGH", "MEDIUM", "LOW"])
    kinds = sorted({it.kind for it in ss.risk_items})
    kind = f2.multiselect("Kind", kinds, default=kinds)

    open_items = [it for it in ss.risk_items if state.item_id(it) not in ss.done_ids and it.severity in sev and it.kind in kind]
    for item in open_items:
        _card(item)

    done = [it for it in ss.risk_items if state.item_id(it) in ss.done_ids]
    if done:
        with st.expander(f"Done ({len(done)})"):
            for it in done:
                st.markdown(f"- {ss.done_ids[state.item_id(it)]}: **{it.title}** · `{it.record_id}`")
