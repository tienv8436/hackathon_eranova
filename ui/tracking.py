"""Tracking from TMS check calls and status changes, and the Fleet map & feed tab. Owner: UI lead.

Everything is read from the TMS (live API or snapshot) as of one time: where each truck last
reported and our ETA at legal driving hours, with the weather & traffic outlook folded in.
get_tracker() feeds the Dispatch board (ui/board.py); this tab shows the same loads on a map and as
a feed. check_call_form() is used by the board's load panel (dry-run: shown, not saved).
"""

from __future__ import annotations

import json
from datetime import datetime

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


def current_statuses() -> tuple[dict[str, TrackingStatus], datetime] | None:
    """For the load watch: tracking statuses and the time they describe."""
    tr = get_tracker()
    return (tr.statuses, tr.as_of) if tr else None


# --------------------------------------------------------------------------- pieces

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
    c1, c2 = st.columns([3, 1])
    c1.caption("**Click a truck to open its Load details.** Hover for details.")
    with c2.popover("Map key", width="stretch"):
        st.markdown("- **Dot**: last reported position, coloured as on the Dispatch board: red act now, "
                    "orange watch, green fine, grey arrived.\n- **Solid line**: the check-call trail.\n"
                    "- **Thin line**: still to go (straight line, not a road route).")


def _map_pick() -> None:
    """A truck clicked on the map: open it on the Load details tab."""
    ss = st.session_state
    objs = (ss.fleet_map["selection"].get("objects") or {}).get("trucks") or []
    if objs:
        ss.board_selected = objs[0]["id"]
        ss.main_tab = "Load details"


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
                st.caption(f"… {len(evs) - 2} earlier event(s) — see Load details")


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
    st.caption(f"As of {geo.fmt_local(tr.as_of, CLOCK_TZ)} · positions come from check calls and status "
               "changes in the TMS.")
    _metrics(tr.statuses)
    _outlook_summary(tr.outlooks, [])   # failed requests go to Data health in the sidebar
    left, right = st.columns([3, 2])
    with left:
        _map(tr, ss.get("board_selected"), tr.outlooks, {sid: v.urgency for sid, v in by_id.items()})
    with right:
        _feed(tr, by_id)


