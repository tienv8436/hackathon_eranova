"""Tab: Dispatch board. One row per load, most urgent first, with the next thing to do. Owner: UI lead.

The board merges what the load watch and live tracking show separately (meridian.board): tracking
statuses and the weather/traffic outlook come from ui/tracking.get_tracker(); the risk rules run
here on the same data and date, without the model (drafts are written per action, on request).
Clicking a truck on the Fleet map & feed tab selects it here.
"""

from __future__ import annotations

import time

import altair as alt
import pandas as pd
import streamlit as st

from meridian import board, client, geo, llm, risk_playbook, risk_rules, tracking
from meridian.board import LoadView
from meridian.models import RiskItem

from . import sidebar, state
from . import tracking as tracking_ui

URGENCY_DOT = {"ACT_NOW": "🔴", "WATCH": "🟠", "FINE": "🟢"}
URGENCY_TEXT = {"ACT_NOW": "need action now", "WATCH": "to watch", "FINE": "fine"}
URGENCY_LABEL = {"ACT_NOW": "Act now", "WATCH": "Watch", "FINE": "Fine"}
PHASES = {"All": None, "In motion": "IN_MOTION", "Pre-pickup": "PRE_PICKUP", "Admin": "ADMIN"}
WHO = {"carrier": "📞 Carrier", "customer": "✉️ Customer", "receiver": "🏭 Receiver", "shipper": "🏭 Shipper",
       "internal": "📝 Internal"}
ROW_H = 72      # px; over 4rem, so the dataframe wraps long headlines instead of cutting them off


# --------------------------------------------------------------------------- data

@st.cache_data(show_spinner="Checking carriers, appointments and quotes…", ttl=300)
def _risks(key: tuple, _tr: tracking.Tracker) -> tuple[list[RiskItem], list[str]]:
    """The load-watch rules on the tracker's data and date. `key` says when to recompute."""
    warnings: list[str] = []
    today = _tr.as_of.astimezone(geo.tz(tracking_ui.CLOCK_TZ)).date()
    ctx = risk_playbook.gather(today, on_step=lambda m: warnings.append(m) if m.startswith("Warning") else None,
                               tracking=_tr.statuses)
    return risk_playbook.rank(risk_rules.run_all(ctx)), warnings


def get_board() -> tuple[list[LoadView], tracking.Tracker] | None:
    """The board, plus the tracker it was built from. Also feeds the sidebar (data health, issues
    caught) and the Manual vs copilot tab (how long ranking took)."""
    t0 = time.perf_counter()
    tr = tracking_ui.get_tracker()
    if tr is None:
        return None
    ss = st.session_state
    key = (ss.data_source, client.demo_mode(), ss.track_refresh, tr.as_of.isoformat(),
           sum(map(len, ss.get("track_dry_calls", {}).values())))
    try:
        risks, warnings = _risks(key, tr)
    except Exception as e:  # the board still works from tracking alone
        risks, warnings = [], [f"Load-watch rules unavailable: {e}"]
    views = board.build(tr.shipments, tr.statuses, risks, tr.outlooks)
    traffic = sorted({e for o in tr.outlooks.values() for e in o.errors if e.startswith("traffic")})
    sidebar.note_health(*warnings, *tr.outlook_errors, *traffic)
    ss.scan_seconds, ss.risk_items, ss.loads_scanned = time.perf_counter() - t0, risks, len(views)
    ss.scoreboard["issues_caught"] = sum(1 for v in views if v.urgency != "FINE")
    return views, tr


# --------------------------------------------------------------------------- pieces

def _header(tr: tracking.Tracker, views: list[LoadView]) -> None:
    n = {u: sum(1 for v in views if v.urgency == u) for u in URGENCY_DOT}
    c1, c2 = st.columns([5, 1])
    c1.markdown(" · ".join(f"{URGENCY_DOT[u]} **{n[u]}** {URGENCY_TEXT[u]}" for u in URGENCY_DOT)
                + f" &nbsp;&nbsp; :gray[as of {geo.fmt_local(tr.as_of, tracking_ui.CLOCK_TZ)}]")
    with c2.popover("How this works", width="stretch"):
        st.markdown(
            "Each row is one load. It merges tracking (check calls, our ETA at legal "
            "driving hours" + (", the weather & traffic forecast" if st.session_state.get("track_outlook", True) else "")
            + ") with the load-watch rules (coverage, compliance, appointments, paperwork).\n\n"
            "- 🔴 **Need action now**: late, a truck that has gone quiet, an ETA that can't be right, or a high-severity rule.\n"
            "- 🟠 **To watch**: tight slack, slowed by weather/traffic, or a medium/low rule.\n"
            "- 🟢 **Fine**: tracked and on time.\n\n"
            "Ranked by urgency, then how late, then missing a must-arrive-by date. **Next step** is the first "
            "action: get a solid ETA from the carrier before moving the appointment or telling the customer.\n\n"
            "The **Fleet map & feed** tab shows the same loads on a map; click a truck there to open it here.")


def _filters(views: list[LoadView]) -> list[LoadView]:
    ss = st.session_state
    c1, c2 = st.columns([4, 1])
    show_fine = c2.toggle("Show on-time loads", key="board_show_fine")
    pool = [v for v in views if show_fine or v.urgency != "FINE"]
    counts = {label: sum(1 for v in pool if phase is None or v.phase == phase) for label, phase in PHASES.items()}
    ss.setdefault("board_phase", "All")
    c1.pills("Phase", list(PHASES), key="board_phase", required=True, label_visibility="collapsed",
             format_func=lambda p: f"{p} · {counts[p]}")
    phase = PHASES[ss.board_phase or "All"]
    return [v for v in pool if phase is None or v.phase == phase]


def _queue(rows: list[LoadView]) -> str | None:
    """The ranked table; returns the selected record id."""
    ss = st.session_state
    if not rows:
        st.info("Nothing here needs attention.")
        return None
    # Plain text in the table: the grid draws on a canvas, where colour emoji can break.
    df = pd.DataFrame([{
        "Urgency": URGENCY_LABEL[v.urgency],
        "Load": v.record_id + (" (quote)" if v.record_type == "quote" else ""),
        "What's happening": v.headline,
        "Next step": _next_step(v),
        "Who": v.next_action.who.capitalize() if v.next_action else "",
    } for v in rows])
    event = st.dataframe(
        df, hide_index=True, on_select="rerun", selection_mode="single-row", row_height=ROW_H,
        height=min(len(rows), 8) * ROW_H + 38, width="stretch",
        key=f"board_queue-{ss.get('board_phase')}-{ss.get('board_show_fine')}",   # new filter: fresh selection
        column_config={
            "Urgency": st.column_config.TextColumn(width=75),
            "Load": st.column_config.TextColumn(width=75),
            "What's happening": st.column_config.TextColumn(width="medium"),
            "Next step": st.column_config.TextColumn(width="medium"),
            "Who": st.column_config.TextColumn(width=75),
        })
    ids = [v.record_id for v in rows]
    if event.selection.rows:
        picked = ids[event.selection.rows[0]]
        if picked != ss.get("board_queue_pick"):   # a new click in the table (the map can select too)
            ss.board_queue_pick = ss.board_selected = picked
    if ss.get("board_selected") not in ids:
        ss.board_selected = ids[0]
    return ss.board_selected


def _done(rid: str) -> set[str]:
    return set(st.session_state.setdefault("board_done", {}).get(rid, []))


def _set_done(rid: str, key: str, value: bool = True) -> None:
    """Which actions the dispatcher has ticked off, per load (this session only)."""
    done = st.session_state.setdefault("board_done", {})
    cur = set(done.get(rid, []))
    (cur.add if value else cur.discard)(key)
    done[rid] = sorted(cur)


def _next_step(v: LoadView) -> str:
    if not v.actions:
        return "—"
    done = _done(v.record_id)
    left = [a for a in v.actions if a.key not in done]
    if not left:
        return "All steps done"
    n_done = len(v.actions) - len(left)
    return left[0].label + (f" ({n_done}/{len(v.actions)} done)" if n_done else "")


def _facts(v: LoadView) -> None:
    s = v.tracking
    tz_name = s.delivery_tz
    late = s.minutes_late
    pos = s.position
    short = lambda dt: dt.astimezone(geo.tz(tz_name)).strftime("%a %H:%M") if dt else "—"  # noqa: E731
    c = st.columns(4)
    basis = ("Our ETA at legal driving hours"
             + (", with the weather & traffic forecast" if v.outlook and v.outlook.delay.minutes else "") + ". ")
    if s.arrived_at:
        c[0].metric("Arrived", short(s.arrived_at), help=f"{geo.fmt_local(s.arrived_at, tz_name)}")
    elif s.eta and s.eta < s.as_of:
        c[0].metric("Projected arrival", "Overdue",
                    help=basis + f"It was {geo.fmt_local(s.eta, tz_name)}; no arrival is recorded, so lateness "
                                 "counts from now.")
    else:
        c[0].metric("Projected arrival", short(s.eta), help=basis + f"{geo.fmt_local(s.eta, tz_name)}")
    c[1].metric("Window ends", short(s.window_end), help=geo.fmt_local(s.window_end, tz_name))
    if late is None:
        c[2].metric("Slack", "—")
    elif late > 0:
        c[2].metric("Late by", tracking._dur(late),
                    delta="misses the MABD" if (s.minutes_late_mabd or 0) > 0 else None, delta_color="inverse")
    else:
        c[2].metric("Slack", tracking._dur(-late))
    if pos and not s.arrived_at:
        c[3].metric("Last contact", f"{s.hours_since_report:.0f} h ago" if s.hours_since_report is not None else "—",
                    help=f"{pos.city}, {pos.state} · {geo.fmt_local(pos.at, tz_name)}")
        c[3].caption(f"{pos.city}, {pos.state}")


def _timeline(v: LoadView, tr: tracking.Tracker) -> None:
    """Planned (the delivery window, the MABD) against projected (our ETA, the driver's), on one axis."""
    s = v.tracking
    tz_name = s.delivery_tz
    ln = tracking.lane(tr.shipments[v.record_id], tr.facilities)

    def loc(dt):   # receiver-local wall time, so the axis reads in local hours
        return dt.astimezone(geo.tz(tz_name)).replace(tzinfo=None).isoformat() if dt else None

    late_color = "#D62728" if s.status == "LATE" else "#F58518" if s.status == "AT_RISK" else "#2E9F43"
    pts = [("Now (as of)", tr.as_of, "#9E9E9E")]
    if s.position and not s.arrived_at:
        pts.append(("Last contact", s.position.at, "#4C78A8"))
    if s.arrived_at:
        pts.append(("Arrived", s.arrived_at, late_color))
    else:
        if s.driver_eta:
            pts.append(("Driver's ETA", s.driver_eta, "#B279A2"))
        if s.eta:
            pts.append(("Our projected arrival", max(s.eta, tr.as_of), late_color))
    df_pts = pd.DataFrame([{"label": label, "at": loc(t), "color": c} for label, t, c in pts if t])
    layers = []
    if ln["window_start"] and s.window_end:
        layers.append(alt.Chart(pd.DataFrame([{"start": loc(ln["window_start"]), "end": loc(s.window_end),
                                               "what": "Delivery window"}]))
                      .mark_rect(opacity=0.25, color="#4C78A8")
                      .encode(x="start:T", x2="end:T",
                              tooltip=["what", alt.Tooltip("start:T", format="%a %m/%d %H:%M"),
                                       alt.Tooltip("end:T", format="%a %m/%d %H:%M")]))
    if s.mabd_end:
        layers.append(alt.Chart(pd.DataFrame([{"at": loc(s.mabd_end), "what": "Must arrive by (end of day)"}]))
                      .mark_rule(color="#9D755D", strokeWidth=2)
                      .encode(x="at:T", tooltip=["what", alt.Tooltip("at:T", format="%a %m/%d %H:%M")]))
    layers.append(alt.Chart(df_pts).mark_point(filled=True, size=160, opacity=1).encode(
        x=alt.X("at:T", title=f"Local time at the receiver ({tz_name})", axis=alt.Axis(format="%a %H:%M")),
        color=alt.Color("label:N", scale=alt.Scale(domain=list(df_pts["label"]), range=list(df_pts["color"])),
                        legend=alt.Legend(title=None, orient="bottom", columns=2, labelLimit=180)),
        tooltip=["label", alt.Tooltip("at:T", format="%a %m/%d %H:%M")]))
    # Streamlit fits the whole chart (axis and legend included) into this height
    st.altair_chart(alt.layer(*layers).properties(height=200), width="stretch")
    st.caption("Shaded: the delivery window" + (". Brown line: end of the must-arrive-by day" if s.mabd_end else "") + ".")


def _cost(v: LoadView, ship: dict) -> None:
    st.markdown("**② What it costs**")
    s = v.tracking
    if s and s.minutes_late is not None and s.minutes_late > 0:
        text = f"**Time:** {tracking._dur(s.minutes_late)} past the delivery window"
        if (s.minutes_late_mabd or 0) > 0:
            text += f", {tracking._dur(s.minutes_late_mabd)} past the must-arrive-by day"
        st.markdown(text + ".")
    elif s:
        st.markdown("**Time:** no delay expected"
                    + (f" ({tracking._dur(-s.minutes_late)} to spare)." if s.minutes_late is not None else "."))
    terms = next((r.evidence.get("mabd_program_note") for r in v.risks if r.evidence.get("mabd_program_note")), None)
    if terms:
        st.markdown(f"**Receiver's terms:** {terms}")
    if ship.get("sell_total"):
        st.markdown(f"**At stake on this load:** ${ship['sell_total']:,.0f} billed"
                    + (f", ${ship['gp_amount']:,.0f} margin" if ship.get("gp_amount") is not None else "") + ".")
    if v.delay_cost:
        c = v.delay_cost
        st.markdown(" · ".join(f"{who}: ${amt:,.0f}" for who, amt in (
            ("Customer", c.customer_usd), ("Carrier", c.carrier_usd), ("Our margin", c.broker_margin_usd))
            if amt is not None))
    else:
        st.caption("Dollar cost of the delay, split by who pays (customer / carrier / our margin): not estimated yet.")


def _draft(v: LoadView, a: board.Action, item: RiskItem | None) -> None:
    """The drafted message for this action, editable, with Approve & log note."""
    ss = st.session_state
    drafts = ss.setdefault("board_drafts", {})
    dkey = f"{v.record_id}|{a.key}"
    audience, text = drafts.get(dkey) or (a.draft_audience, a.draft)
    if not text:
        if item and st.button("✍️ Draft a message", key=f"bd-draft-{dkey}"):
            with st.spinner("Drafting…"):
                it = llm.explain_and_draft([item.model_copy()], templated=state.use_fake())[0]
            drafts[dkey] = (it.draft_audience, it.draft_message)
            st.rerun()
        return
    edited = st.text_area(f"Message to {audience or '…'}", text, key=f"bd-text-{dkey}", height=90)
    if st.button("✅ Approve & log note", key=f"bd-ok-{dkey}", type="primary"):
        note = f"[Copilot] {a.label}\n\nDraft ({audience}): {edited}"
        res = risk_playbook.act(item, "note", text=note) if item else client.add_shipment_note(v.record_id, note)
        if res["ok"]:
            _set_done(v.record_id, a.key)
            state.record_done("risk")
            st.toast(f"Logged note on {v.record_id}" + (" (dry-run)" if res.get("dry_run") else ""))
            st.rerun()
        else:
            st.error(f"{v.record_id}: {res.get('detail') or res.get('error')}")


def _actions(v: LoadView, tr: tracking.Tracker) -> None:
    st.markdown("**③ What to do**")
    if not v.actions:
        st.caption("Nothing to do: " + ("on time." if v.urgency == "FINE" else "keep an eye on it."))
        return
    rid, done = v.record_id, _done(v.record_id)
    items = {r.kind: r for r in v.risks}
    for i, a in enumerate(v.actions, 1):
        is_done = a.key in done
        with st.container(border=True):
            # the key carries the state, so ticking it elsewhere (a logged call or note) redraws it
            st.checkbox(f"~~{i}. {a.label}~~" if is_done else f"**{i}. {a.label}**",
                        value=is_done, key=f"bd-done-{rid}-{a.key}-{int(is_done)}",
                        on_change=_set_done, args=(rid, a.key, not is_done))
            st.caption(f"{WHO.get(a.who, a.who)} · Because: " + "; ".join(a.why)
                       + f". Done when: {a.done_when[0].lower()}{a.done_when[1:]}.")
            if is_done:
                continue
            if a.key in ("call_carrier", "confirm_eta") and rid in tr.statuses:
                tracking_ui.check_call_form(tr, rid, key_prefix="board-cc",
                                            on_logged=lambda rid=rid, k=a.key: _set_done(rid, k))
            _draft(v, a, items.get(a.risk_kind) if a.risk_kind else None)


def _evidence(v: LoadView, tr: tracking.Tracker) -> None:
    s = v.tracking
    tz_name = s.delivery_tz if s else None
    with st.expander("④ Evidence: ETA math, weather & traffic, activity, rule details"):
        tabs = st.tabs(["ETA math", "Weather & traffic", "Activity", "Rules"])
        with tabs[0]:
            if not s:
                st.caption("Not tracked: no ETA for this record.")
            else:
                for x in s.exceptions:
                    st.markdown(f"- ⚠️ {x}")
                if s.assumptions:
                    st.markdown("Our ETA assumes: " + "; ".join(s.assumptions) + ".")
        with tabs[1]:
            if v.outlook:
                tracking_ui._outlook_detail(v.outlook, tz_name)
            else:
                st.caption("No weather & traffic outlook for this load (not on the road, or the toggle is off).")
        with tabs[2]:
            events = tr.events(v.record_id, limit=20) if v.record_id in tr.statuses else []
            if not events:
                st.caption("No TMS activity for this record.")
            for e in events:
                st.markdown(f"{tracking_ui.LEVEL_DOT[e.level]} {tracking_ui.SOURCE_ICON[e.source]} "
                            f"`{geo.fmt_local(e.at, tz_name)}` · {e.text}")
        with tabs[3]:
            if not v.risks:
                st.caption("No load-watch rules flagged this record.")
            for r in v.risks:
                st.markdown(f"**{r.title}** · {r.kind} · {r.severity}")
                if r.explanation:
                    st.caption(r.explanation)
                st.table(pd.DataFrame({"field": list(r.evidence), "value": [str(x) for x in r.evidence.values()]})
                         .set_index("field"))


def _selected(v: LoadView, tr: tracking.Tracker) -> None:
    """The load panel: ① what's happening, ② what it costs, ③ what to do, ④ evidence."""
    st.divider()
    ship = tr.shipments.get(v.record_id, {}) if v.record_type == "shipment" else {}
    lane = f" · {v.lane}" if v.lane else ""
    st.markdown(f"#### {URGENCY_DOT[v.urgency]} {v.record_type.capitalize()} {v.record_id}{lane}")
    meta = [f"carrier {v.carrier_id}" if v.carrier_id else "", f"customer {v.customer_id}" if v.customer_id else "",
            v.stage.replace("_", " ").lower() if v.stage else ""]
    if any(meta):
        st.caption(" · ".join(x for x in meta if x))

    st.markdown("**① What's happening**")
    st.markdown(f"##### {v.headline}")
    if v.tracking:
        _facts(v)
    left, right = st.columns([3, 2], gap="large")
    with left:
        _actions(v, tr)
    with right:
        if v.tracking and v.record_id in tr.shipments:
            _timeline(v, tr)
        _cost(v, ship)
    _evidence(v, tr)


# --------------------------------------------------------------------------- tab

def render() -> None:
    got = get_board()
    if got is None:
        return
    views, tr = got
    ss = st.session_state
    ss.setdefault("board_show_fine", False)
    _header(tr, views)
    rows = _filters(views)
    sid = _queue(rows)
    if sid is None:
        return
    _selected(next(v for v in rows if v.record_id == sid), tr)
