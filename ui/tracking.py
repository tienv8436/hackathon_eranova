"""Tab: Live tracking from TMS check calls and status changes. Owner: UI lead.

Everything is read from the TMS (live API or snapshot) as of one time: where each truck last
reported, our ETA at legal driving hours next to the driver's ETA, and what needs attention.
Dispatchers can log a check call here; in dry-run it is shown but not saved.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time as dtime, timedelta

import altair as alt
import pandas as pd
import pydeck as pdk
import streamlit as st

from meridian import client, geo, outlook, tracking, traffic, weather
from meridian.models import FeedEvent, TrackingStatus
from meridian.outlook import DelayOutlook

from . import state

CLOCK_TZ = "America/Chicago"
STATUS_COLOR = {"ON_TIME": [46, 160, 67], "AT_RISK": [235, 140, 0], "LATE": [214, 39, 40],
                "ARRIVED": [120, 120, 120], "NOT_TRACKED": [140, 140, 140]}
STATUS_LABEL = {"ON_TIME": ":green[On time]", "AT_RISK": ":orange[At risk]", "LATE": ":red[Late]",
                "ARRIVED": ":gray[Arrived]", "NOT_TRACKED": ":gray[Not tracked]"}
STATUS_ORDER = {"LATE": 0, "AT_RISK": 1, "ON_TIME": 2, "ARRIVED": 3, "NOT_TRACKED": 4}
SOURCE_ICON = {"check_call": "📞", "status": "🚚", "appointment": "📅", "copilot": "⚠️"}
LEVEL_DOT = {"alert": "🔴", "warning": "🟠", "info": "⚪"}
REASON_CODES = {"": "none", "AI": "AI · mechanical breakdown", "AO": "AO · weather", "B1": "B1 · consignee closed",
                "T3": "T3 · tractor/trailer not available", "D1": "D1 · carrier dispatch error"}


# --------------------------------------------------------------------------- data

def _load(source: str) -> tuple[list[dict], dict[str, dict], dict[str, list[dict]]]:
    if source == "Fake data":   # fake mode has no loads to track: use the committed snapshot
        snap = state.ROOT / "snapshots"
        read = lambda n: json.loads((snap / f"{n}.json").read_text(encoding="utf-8"))  # noqa: E731
        return read("open_shipments"), {f["facility_id"]: f for f in read("facilities")}, read("shipment_events")
    ships, facs = client.list_shipments(), client.list_facilities()
    if not ships["ok"] or not facs["ok"]:
        raise RuntimeError(ships.get("detail") or facs.get("detail") or "could not read shipments/facilities")
    tracked = [s["shipment_id"] for s in ships["data"] if s.get("status") in tracking.TRACKED_STAGES]
    events = client.events_for(tracked)
    return ships["data"], {f["facility_id"]: f for f in facs["data"]}, events["data"]


@st.cache_data(show_spinner="Reading loads and check calls…", ttl=300)
def _data(source: str, demo_mode: bool, refresh: int):
    return _load(source)


def get_tracker() -> tracking.Tracker | None:
    ss = st.session_state
    ss.setdefault("track_refresh", 0)
    ss.setdefault("track_dry_calls", {})          # shipment_id -> [event dicts] logged in dry-run
    ss.setdefault("track_as_of_mode", "Latest TMS activity")
    try:
        ships, facs, events = _data(ss.data_source, client.demo_mode(), ss.track_refresh)
    except Exception as e:  # keep the app alive if the API is down
        st.error(f"Tracking unavailable: {e}")
        return None
    merged = {sid: list(evs) + ss.track_dry_calls.get(sid, []) for sid, evs in events.items()}
    for sid, extra in ss.track_dry_calls.items():
        merged.setdefault(sid, list(extra))
    as_of = None if ss.track_as_of_mode == "Latest TMS activity" else datetime.now(geo.UTC)
    tr = tracking.Tracker(ships, facs, merged, as_of=as_of)
    # Fold the weather & traffic outlook into each status, so this tab and the load watch both use the
    # predicted ETA. The outlooks keep the legal-hours ETA (DelayOutlook.eta) for display.
    ss.setdefault("track_outlook", True)
    tr.outlooks, tr.outlook_errors = get_outlooks(tr)
    tr.statuses = {sid: outlook.apply(s, tr.outlooks.get(sid)) for sid, s in tr.statuses.items()}
    return tr


@st.cache_data(show_spinner="Checking weather (NWS) and traffic (TomTom) along every route…", ttl=900)
def _outlooks(key: tuple, _tr: tracking.Tracker) -> tuple[dict[str, DelayOutlook], list[str]]:
    """Weather + traffic outlook per load. `key` says when to recompute (the tracker itself isn't hashed)."""
    nws, tt = weather.NWS(), traffic.TomTom()
    outs = outlook.outlook_all(_tr, nws, tt)
    errors = [f"{len(e)} {name} request(s) failed, e.g. {e[0]}" for name, e in (("NWS", nws.errors), ("TomTom", tt.errors)) if e]
    return outs, errors


def get_outlooks(tr: tracking.Tracker) -> tuple[dict[str, DelayOutlook], list[str]]:
    ss = st.session_state
    if not ss.get("track_outlook", True):
        return {}, []
    key = (ss.data_source, client.demo_mode(), ss.track_refresh, tr.as_of.isoformat(),
           sum(map(len, ss.get("track_dry_calls", {}).values())))
    try:
        return _outlooks(key, tr)
    except Exception as e:  # weather/traffic are extras: never take the tab down
        return {}, [f"Weather/traffic outlook unavailable: {e}"]


def _dur(minutes: float) -> str:
    return f"{minutes / 60:.1f} h" if abs(minutes) >= 90 else f"{minutes:.0f} min"


def _outlook_short(o: DelayOutlook | None) -> str:
    """'+29 min (weather)' / '' when nothing slows the load."""
    if not o or not o.delay.minutes:
        return ""
    parts = [n for n, m in (("weather", o.weather_minutes), ("traffic", o.traffic_minutes)) if m]
    return f"+{_dur(o.delay.minutes)}" + (f" ({' + '.join(parts)})" if parts else "")


def _outlook_events(tr: tracking.Tracker, outs: dict[str, DelayOutlook]) -> list[FeedEvent]:
    """Copilot feed lines for loads the outlook slows by 10+ min or moves to a worse status."""
    out = []
    for sid, o in outs.items():
        worse = o.predicted_status != o.status
        if not o.notes or o.delay.minutes < 10 or worse:   # worse: already in the feed as the load's first exception
            continue
        change = (f" · {o.status.replace('_', ' ').lower()} → {o.predicted_status.replace('_', ' ').lower()}"
                  if worse else "")
        out.append(FeedEvent(at=tr.as_of, shipment_id=sid, source="copilot", level="info",
                             text=f"🌦️ Outlook {_outlook_short(o)}{change}: {o.notes[0]}"))
    return out


def current_statuses() -> tuple[dict[str, TrackingStatus], datetime] | None:
    """For the load watch: tracking statuses and the time they describe."""
    tr = get_tracker()
    return (tr.statuses, tr.as_of) if tr else None


# --------------------------------------------------------------------------- pieces

def _header(tr: tracking.Tracker) -> None:
    c1, c2, c3 = st.columns([2.2, 1.6, 1])
    c1.markdown(f"**As of {geo.fmt_local(tr.as_of, CLOCK_TZ)}**")
    c1.caption("Positions come from check calls and status changes in the TMS. "
               "ETAs are ours, at legal driving hours, shown next to what the driver reported."
               + (" With *Weather & traffic* on, statuses and lateness use the predicted ETA "
                  "(NWS forecast + TomTom traffic along the rest of the route)."
                  if st.session_state.get("track_outlook", True) else ""))
    modes = ["Latest TMS activity"] + (["Now"] if st.session_state.data_source == "Live API" else [])
    c2.radio("As of", modes, key="track_as_of_mode", horizontal=True,
             help="Latest TMS activity: the moment the data describes. Now: the current clock (live API only).")
    if c3.button("↻ Refresh", help="Re-read loads and check calls from the TMS."):
        st.session_state.track_refresh += 1
        st.rerun()
    c3.toggle("Weather & traffic", key="track_outlook",
              help="Check the NWS forecast and TomTom traffic along each load's remaining route, at the hour "
                   "the truck should be there, and add what they cost to a predicted ETA.")


def _outlook_summary(outs: dict[str, DelayOutlook], errors: list[str]) -> None:
    if not st.session_state.get("track_outlook", True):
        return
    slowed = [o for o in outs.values() if o.delay.minutes]
    worse = [o for o in outs.values() if o.predicted_status != o.status]
    total = sum(o.delay.minutes for o in slowed)
    text = (f"🌦️ **Weather & traffic outlook:** {len(slowed)} of {len(outs)} loads slowed"
            + (f", {_dur(total)} in total" if total else "")
            + (f"; **{len(worse)} would change status** ({', '.join(o.shipment_id for o in worse)})" if worse else ""))
    st.markdown(text)
    notes = errors + sorted({e for o in outs.values() for e in o.errors if e.startswith("traffic")})
    if notes:
        st.caption(" · ".join(notes))


def _metrics(statuses: dict[str, TrackingStatus]) -> None:
    n = lambda k: sum(1 for s in statuses.values() if s.status == k)  # noqa: E731
    c = st.columns(5)
    c[0].metric("Trucks tracked", len(statuses))
    c[1].metric("On time", n("ON_TIME"))
    c[2].metric("At risk", n("AT_RISK"))
    c[3].metric("Late", n("LATE"))
    c[4].metric("Arrived", n("ARRIVED"))


URGENCY_COLOR = {"ACT_NOW": [214, 39, 40], "WATCH": [235, 140, 0], "FINE": [46, 160, 67]}


def _map(tr: tracking.Tracker, selected: str | None, outs: dict[str, DelayOutlook],
         urgency: dict[str, str] | None = None) -> None:
    """urgency: the board's urgency per load; when given, dots use the board's colours (red act now,
    orange watch, green fine), so the map and the board agree. Arrived loads stay grey."""
    trucks, trails, ahead = [], [], []
    for sid, s in tr.statuses.items():
        pos = s.position
        if not pos or pos.lat is None:
            continue
        ship = tr.shipments[sid]
        dest = tracking.lane(ship, tr.facilities)["dest"]
        color = (URGENCY_COLOR[urgency[sid]] if urgency and sid in urgency and s.status != "ARRIVED"
                 else STATUS_COLOR[s.status])
        trucks.append({
            "id": sid, "lon": pos.lon, "lat": pos.lat, "color": color, "radius": 30 if sid == selected else 12,
            "status": s.status.replace("_", " ").title(), "stage": s.stage.replace("_", " ").lower(),
            "where": f"{pos.city}, {pos.state}" + (" (approx.)" if pos.approximate else ""),
            "when": f"{geo.fmt_local(pos.at, s.delivery_tz)} ({s.hours_since_report:.0f} h ago)",
            "eta": geo.fmt_local(s.eta, s.delivery_tz), "late": tracking.describe_lateness(s),
            "lane": f"{ship.get('shipper_city')} → {ship.get('consignee_city')}",
            "outlook": f"<br/>🌦️ Outlook {_outlook_short(o)}" if (o := outs.get(sid)) and o.delay.minutes else "",
        })
        pts = [p for p in s.trail if p.lat is not None]
        for a, b in zip(pts, pts[1:]):
            trails.append({"from": [a.lon, a.lat], "to": [b.lon, b.lat],
                           "color": [*color, 230] if sid == selected else [110, 110, 110, 150]})
        if dest and s.status != "ARRIVED" and s.stage != "AT_DELIVERY":
            ahead.append({"from": [pos.lon, pos.lat], "to": [dest[1], dest[0]],
                          "color": [*color, 200 if sid == selected else 70], "width": 2.5 if sid == selected else 1})
    layers = [
        pdk.Layer("LineLayer", ahead, get_source_position="from", get_target_position="to", get_color="color", get_width="width"),
        pdk.Layer("LineLayer", trails, get_source_position="from", get_target_position="to", get_color="color", get_width=2),
        pdk.Layer("ScatterplotLayer", trucks, id="trucks", get_position=["lon", "lat"], get_fill_color="color",
                  get_radius="radius", radius_scale=2500, radius_min_pixels=5, radius_max_pixels=11, stroked=True,
                  get_line_color=[255, 255, 255], line_width_min_pixels=1.5, pickable=True),
    ]
    tooltip = {"html": "<b>{id}</b> · {status} · {stage}<br/>{lane}<br/>Last report: {where}<br/>{when}"
                       "<br/>ETA {eta}<br/>{late}{outlook}", "style": {"fontSize": "12px"}}
    deck = pdk.Deck(layers=layers, initial_view_state=pdk.ViewState(latitude=38.6, longitude=-97.5, zoom=3.3),
                    map_provider="carto", map_style="light", tooltip=tooltip)
    st.pydeck_chart(deck, width="stretch", height=430, on_select=_map_pick, selection_mode="single-object",
                    key="fleet_map")
    st.caption("Dot = last reported position, coloured as on the Dispatch board (red act now, orange watch, "
               "green fine, grey arrived). Solid line = check-call trail, thin line = still to go "
               "(straight line, not a road route). Hover for details; **click a truck to open it on the "
               "Dispatch board**.")


def _map_pick() -> None:
    """A truck clicked on the map: open it on the Dispatch board, with filters that show it."""
    ss = st.session_state
    objs = (ss.fleet_map["selection"].get("objects") or {}).get("trucks") or []
    if not objs:
        return
    sid = objs[0]["id"]
    ss.board_selected = sid
    ss.board_phase = "All"
    if sid in ss.get("fleet_fine_ids", ()):
        ss.board_show_fine = True
    ss.main_tab = "Dispatch board"


def _feed(tr: tracking.Tracker, by_id: dict) -> None:
    """The activity feed, grouped by load: the board's one-line headline, then the latest TMS events
    (check calls, status changes, appointments). Loads with the newest activity first."""
    attention = st.toggle("Only loads needing attention", key="fleet_attention_only")
    groups: dict[str, list[FeedEvent]] = {}
    for e in tr.events(limit=1000):
        if e.source != "copilot":          # the headline already sums up the copilot's exceptions
            groups.setdefault(e.shipment_id, []).append(e)
    ids = [sid for sid in tr.statuses if not attention or (sid in by_id and by_id[sid].urgency != "FINE")]
    never = datetime.min.replace(tzinfo=geo.UTC)
    ids.sort(key=lambda sid: groups[sid][0].at if groups.get(sid) else never, reverse=True)
    with st.container(height=455):
        if not ids:
            st.caption("Nothing needs attention.")
        for sid in ids:
            v, evs = by_id.get(sid), groups.get(sid, [])
            dot = {"ACT_NOW": "🔴", "WATCH": "🟠", "FINE": "🟢"}.get(v.urgency, "⚪") if v else "⚪"
            st.markdown(f"{dot} **{sid}** · {v.headline if v else ''}")
            for e in evs[:2]:
                st.caption(f"{SOURCE_ICON[e.source]} `{geo.fmt_local(e.at, CLOCK_TZ)}` · {e.text}")
            if len(evs) > 2:
                st.caption(f"… {len(evs) - 2} earlier event(s) — see the load on the Dispatch board")


def _timeline(tr: tracking.Tracker, sid: str, o: DelayOutlook | None) -> None:
    s, ship = tr.statuses[sid], tr.shipments[sid]
    tz_name = s.delivery_tz
    adjusted = bool(o and o.delay.minutes)       # s already carries the predicted ETA (outlook.apply)
    legal_eta = o.eta if adjusted else s.eta
    base_assumptions = s.assumptions[: len(s.assumptions) - len(o.notes) - 1] if adjusted else s.assumptions
    p_stop, d_stop = ship["stops"][0], ship["stops"][-1]
    pw, dw = p_stop.get("scheduled_window") or {}, d_stop.get("scheduled_window") or {}

    def loc(dt: datetime | None) -> str | None:   # receiver-local wall time, so the axis reads in local hours
        return dt.astimezone(geo.tz(tz_name)).replace(tzinfo=None).isoformat() if dt else None

    bars = [
        {"row": "Pickup window", "start": loc(geo.local_to_utc(pw.get("earliest_local"), pw.get("timezone"))),
         "end": loc(geo.local_to_utc(pw.get("latest_local"), pw.get("timezone"))), "kind": "Window"},
        {"row": "Delivery window", "start": loc(geo.local_to_utc(dw.get("earliest_local"), tz_name)),
         "end": loc(s.window_end), "kind": "Window"},
    ]
    if d_stop.get("mabd_date"):
        bars.append({"row": "Must arrive by", "start": loc(geo.local_to_utc(f"{d_stop['mabd_date']}T00:00:00", tz_name)),
                     "end": loc(s.mabd_end), "kind": "MABD"})
    pts = []
    if s.departed_at:
        pts.append({"row": "Truck", "at": loc(s.departed_at), "label": "Departed", "detail": "loaded, left the shipper"})
    for c in s.trail:
        if c.source == "check_call":
            pts.append({"row": "Truck", "at": loc(c.at), "label": "Check call", "detail": f"{c.city}, {c.state}"})
    if s.arrived_at:
        pts.append({"row": "Truck", "at": loc(s.arrived_at), "label": "Arrived", "detail": "at the receiver"})
    else:
        if legal_eta:
            pts.append({"row": "Truck", "at": loc(legal_eta), "label": "Our ETA (legal hours)", "detail": "; ".join(base_assumptions)})
        if s.driver_eta:
            pts.append({"row": "Truck", "at": loc(s.driver_eta), "label": "Driver's ETA", "detail": "as reported on the last call"})
        if o and o.delay.minutes and o.predicted_eta:
            pts.append({"row": "Truck", "at": loc(o.predicted_eta), "label": "Predicted (weather + traffic)",
                        "detail": "; ".join(o.notes)})

    rows = [r for r in ("Pickup window", "Truck", "Delivery window", "Must arrive by")
            if r == "Truck" or any(b["row"] == r for b in bars)]
    y = alt.Y("row:N", scale=alt.Scale(domain=rows), sort=None, title=None, axis=alt.Axis(labelLimit=200))
    labels = ["Departed", "Check call", "Arrived", "Our ETA (legal hours)", "Driver's ETA", "Predicted (weather + traffic)"]
    bar = alt.Chart(pd.DataFrame(bars)).mark_bar(height=16, opacity=0.75).encode(
        x=alt.X("start:T", title=f"Local time at the receiver ({tz_name})"), x2="end:T", y=y,
        color=alt.Color("kind:N", scale=alt.Scale(domain=["Window", "MABD"], range=["#4C78A8", "#9D755D"]), legend=None),
        tooltip=["row", alt.Tooltip("start:T", format="%a %m/%d %H:%M"), alt.Tooltip("end:T", format="%a %m/%d %H:%M")])
    pt = alt.Chart(pd.DataFrame(pts)).mark_point(filled=True, size=150).encode(
        x="at:T", y=y,
        shape=alt.Shape("label:N", scale=alt.Scale(domain=labels, range=["circle", "diamond", "square", "triangle-up",
                                                                         "triangle-down", "triangle-right"]),
                        legend=alt.Legend(title=None, orient="bottom")),
        color=alt.Color("label:N", scale=alt.Scale(domain=labels, range=["#4C78A8", "#9E9E9E", "#2E9F43", "#D62728",
                                                                         "#F2A93B", "#7B4FD6"]),
                        legend=alt.Legend(title=None, orient="bottom")),
        tooltip=["label", alt.Tooltip("at:T", format="%a %m/%d %H:%M"), "detail"]) if pts else None
    rule = alt.Chart(pd.DataFrame([{"at": loc(tr.as_of)}])).mark_rule(strokeDash=[4, 3], color="#9E9E9E").encode(
        x="at:T", tooltip=[alt.Tooltip("at:T", title="as of", format="%a %m/%d %H:%M")])
    chart = bar + rule + pt if pt is not None else bar + rule
    st.altair_chart(chart.resolve_scale(color="independent", shape="independent").properties(height=60 + 42 * len(rows)),
                    width="stretch")
    st.caption("Dashed line = as-of time. ▲ red = our ETA at legal driving hours, ▼ yellow = the driver's reported ETA"
               + (", ▶ purple = predicted with weather and traffic." if o and o.delay.minutes else "."))

    left, right = st.columns([3, 2])
    with left:
        st.markdown(f"**{STATUS_LABEL[s.status]}** · {tracking.describe_lateness(s) or '—'}"
                    + (" · *with weather & traffic*" if adjusted else ""))
        pos = s.position
        gap = s.eta_gap_minutes
        rows_tbl = {
            "Stage": s.stage.replace("_", " ").lower(),
            "Last position": (f"{pos.city}, {pos.state}" + (" (approx.)" if pos.approximate else "")
                              + f" · {geo.fmt_local(pos.at, tz_name)} · {s.hours_since_report:.0f} h ago") if pos else "—",
            "Miles to go": "—" if s.miles_remaining is None else f"{s.miles_remaining:.0f}",
            "Arrived" if s.arrived_at else "Our ETA (legal hours)": geo.fmt_local(s.arrived_at or legal_eta, tz_name),
            **({"Predicted (weather + traffic)": f"{geo.fmt_local(o.predicted_eta, tz_name)} · {_outlook_short(o)} · "
                                                 f"{o.predicted_status.replace('_', ' ').lower()}"}
               if o and o.delay.minutes else {}),
            "Driver's ETA": geo.fmt_local(s.driver_eta, tz_name) if s.driver_eta else "—",
            "Driver vs ours": "—" if gap is None else (f"driver {abs(gap) / 60:.1f} h earlier" if gap > 0 else
                                                      f"driver {abs(gap) / 60:.1f} h later"),
            "Window ends": geo.fmt_local(s.window_end, tz_name),
            "Must arrive by": geo.fmt_local(s.mabd_end, tz_name) if s.mabd_end else "—",
            "Carrier": ship.get("carrier_id") or "—",
        }
        st.table(pd.DataFrame({"": list(rows_tbl), "value": list(rows_tbl.values())}).set_index(""))
        for x in s.exceptions:
            (st.error if s.status == "LATE" else st.warning)(x, icon="⚠️")
        if base_assumptions:
            st.caption("Our ETA assumes: " + "; ".join(base_assumptions) + ".")
        if o:
            _outlook_detail(o, tz_name)
    with right:
        st.markdown("**This load's activity**")
        with st.container(height=330):
            for e in tr.events(sid, limit=40):
                st.markdown(f"{LEVEL_DOT[e.level]} {SOURCE_ICON[e.source]} `{geo.fmt_local(e.at, tz_name)}` · {e.text}")
    check_call_form(tr, sid)


def _outlook_detail(o: DelayOutlook, tz_name: str | None) -> None:
    st.markdown("**🌦️ Weather & traffic on the route**")
    if o.notes:
        for line in o.notes:
            st.markdown(f"- {line}")
    else:
        st.markdown("- Nothing on the remaining route is expected to slow this load.")
    for line in o.incidents:
        st.markdown(f"- 🚧 {line}")
    if o.traffic_shifts:
        st.dataframe(pd.DataFrame([{
            "Shift from": geo.fmt_local(t.depart, tz_name), "Stretch": f"{t.start_place} → {t.end_place}",
            "Free-flow": _dur(t.free_flow_minutes), "With traffic (TomTom)": _dur(t.predicted_minutes),
            "Our plan": _dur(t.plan_minutes), "Added": f"+{_dur(t.extra_minutes)}" if t.extra_minutes else "—",
        } for t in o.traffic_shifts]), hide_index=True, width="stretch")
    cover = (f"Forecast reaches {o.weather_miles_covered:.0f} of {o.miles_total:.0f} mi"
             + (f" · traffic checked for {o.traffic_miles_covered:.0f} mi" if o.traffic_miles_covered else ""))
    st.caption(cover + ". Each stretch is checked at the hour our plan puts the truck there; stretches already "
               "in the past can't be forecast. Speed assumptions: NWS conditions → a fixed slowdown table; "
               "traffic counts only beyond our 50 mph plan." + (" " + " ".join(o.errors) if o.errors else ""))


def check_call_form(tr: tracking.Tracker, sid: str, key_prefix: str = "cc", expanded: bool = False,
                    on_logged=None) -> None:
    """Log a check call (dry-run: shown, not saved). key_prefix keeps widget keys apart when the same
    load's form is on two tabs; on_logged() runs after a call is recorded, before the rerun."""
    s = tr.statuses[sid]
    tz_name = s.delivery_tz
    ss = st.session_state
    with st.expander(f"📞 Log a check call for {sid}", expanded=expanded):
        with st.form(f"{key_prefix}-{sid}", clear_on_submit=True):
            c1, c2, c3 = st.columns([2, 1, 2])
            city = c1.text_input("City")
            st_code = c2.text_input("State", max_chars=2)
            reason = c3.selectbox("Reason code", list(REASON_CODES), format_func=REASON_CODES.get)
            local_now = tr.as_of.astimezone(geo.tz(tz_name))
            c4, c5, c6 = st.columns(3)
            eta_day = c4.date_input("Driver's ETA (date)", value=None, help=f"Receiver local time ({tz_name})")
            eta_time = c5.time_input("Driver's ETA (time)", value=None, step=900)
            hours = c6.number_input("Driving hours left (if the driver says)", min_value=0.0, max_value=11.0,
                                    value=None, step=0.5)
            note = st.text_input("Note", placeholder="e.g. Running behind after a tire change")
            c7, c8 = st.columns(2)
            call_day = c7.date_input("Call date", value=local_now.date())
            call_time = c8.time_input("Call time", value=local_now.time().replace(second=0, microsecond=0), step=300)
            submitted = st.form_submit_button("Log check call", type="primary")
        if submitted:
            if not city or not st_code:
                st.error("City and state are required.")
                return
            at = datetime.combine(call_day, call_time, tzinfo=geo.tz(tz_name)).astimezone(geo.UTC)
            eta = (datetime.combine(eta_day, eta_time, tzinfo=geo.tz(tz_name)).astimezone(geo.UTC)
                   if eta_day and eta_time else None)
            text = (note or "Check call") + (f" ({hours:g} h left)" if hours is not None else "")
            iso = lambda d: d.isoformat().replace("+00:00", "Z")  # noqa: E731
            fields = {"city": city.strip().title(), "state": st_code.strip().upper(), "event_at": iso(at)}
            if eta:
                fields["eta"] = iso(eta)
            if reason:
                fields["reason_code"] = reason
            res = client.record_check_call(sid, text, **fields)
            if not res["ok"]:
                st.error(f"Check call not recorded: {res.get('detail') or res.get('error')}")
                return
            if res.get("dry_run"):
                ss.track_dry_calls.setdefault(sid, []).append({"event_type": "CHECK_CALL", "note": text, "_dry_run": True, **fields})
                st.toast(f"Check call for {sid} shown in dry-run (not saved to the TMS)")
            else:
                ss.track_refresh += 1
                st.toast(f"Check call saved on {sid}")
            if on_logged:
                on_logged()
            st.rerun()
    if ss.track_dry_calls.get(sid):
        if st.button(f"Clear {len(ss.track_dry_calls[sid])} dry-run call(s) on {sid}", key=f"{key_prefix}-clear-{sid}"):
            ss.track_dry_calls.pop(sid)
            st.rerun()


# --------------------------------------------------------------------------- tab

def render() -> None:
    """Fleet map & feed: every tracked truck on a map, and what the TMS heard from each, by load.
    The per-load detail lives on the Dispatch board (ui/board.py); clicking a truck opens it there."""
    from . import board as board_ui   # the board builds on this module, so import it here

    got = board_ui.get_board()
    if got is None:
        return
    views, tr = got
    by_id = {v.record_id: v for v in views}
    ss = st.session_state
    ss.setdefault("fleet_attention_only", True)
    ss.fleet_fine_ids = [v.record_id for v in views if v.urgency == "FINE"]
    st.caption(f"As of {geo.fmt_local(tr.as_of, CLOCK_TZ)} · positions come from check calls and status "
               "changes in the TMS.")
    _metrics(tr.statuses)
    _outlook_summary(tr.outlooks, [])   # failed requests go to Data health in the sidebar
    left, right = st.columns([3, 2])
    with left:
        _map(tr, ss.get("board_selected"), tr.outlooks, {sid: v.urgency for sid, v in by_id.items()})
    with right:
        _feed(tr, by_id)


def _old_timeline_view(tr: tracking.Tracker, outs: dict[str, DelayOutlook], ordered: list[str]) -> None:
    """The previous per-load view (selectbox + timeline + check-call form). Not shown any more: the
    Dispatch board's load panel replaced it. Kept until the team decides it can go."""
    ss = st.session_state
    st.subheader("Load timeline")
    st.selectbox("Load", ordered, key="track_selected",
                 format_func=lambda k: f"{k} · {tr.statuses[k].status.replace('_', ' ').title()} · "
                                       f"{tr.shipments[k].get('shipper_city')} → {tr.shipments[k].get('consignee_city')}"
                                       f" · {tracking.describe_lateness(tr.statuses[k]) or tr.statuses[k].stage.lower()}"
                                       + (f" · 🌦️ {s}" if (s := _outlook_short(outs.get(k))) else ""))
    if ss.track_selected:
        _timeline(tr, ss.track_selected, outs.get(ss.track_selected))
