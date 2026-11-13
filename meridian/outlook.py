"""Delay outlook: weather (NWS) + traffic (TomTom) on each load's remaining route, combined into one
predicted ETA. Owner: agent & evals lead.

Both sources work on the same timed plan (meridian.hazards.legs_for_status) and feed one
hazards.route_delay call, so their effects add up and are run through hours of service together
(e.g. a snowy rush hour that pushes the driver into a 10 h break). Each line in `notes` says what was
assumed and where the numbers came from.

    python -m meridian.outlook            # from snapshots; live APIs unless DEMO_MODE=1
"""

from __future__ import annotations

import json
import sys
from datetime import datetime

from pydantic import BaseModel

from . import geo, hazards, tracking, traffic, weather
from .client import SNAPSHOT_DIR
from .geo import UTC
from .hazards import RouteDelay
from .models import TrackingStatus
from .traffic import ShiftTraffic


class DelayOutlook(BaseModel):
    shipment_id: str
    checked_at: datetime
    eta: datetime | None = None               # tracking ETA (no weather/traffic)
    predicted_eta: datetime | None = None     # with weather + traffic
    minutes_late: int | None = None           # predicted, vs window end
    minutes_late_mabd: int | None = None
    status: str = ""                          # tracking status
    predicted_status: str = ""
    delay: RouteDelay = RouteDelay()
    weather_minutes: int = 0
    traffic_minutes: int = 0
    notes: list[str] = []                     # what adds time, one line each
    incidents: list[str] = []                 # notable traffic incidents (already inside TomTom's estimate)
    traffic_shifts: list[ShiftTraffic] = []
    miles_total: float = 0
    weather_miles_covered: float = 0
    traffic_miles_covered: float = 0
    errors: list[str] = []


def outlook(st: TrackingStatus, shipment: dict, facilities: dict[str, dict],
            nws: weather.NWS, tt: traffic.TomTom, now: datetime | None = None) -> DelayOutlook | None:
    if st.stage == "AT_DELIVERY" or st.status in ("ARRIVED", "NOT_TRACKED") or not st.eta:
        return None
    out = DelayOutlook(shipment_id=st.shipment_id, checked_at=datetime.now(UTC), eta=st.eta,
                       predicted_eta=st.eta, minutes_late=st.minutes_late, minutes_late_mabd=st.minutes_late_mabd,
                       status=st.status, predicted_status=st.status)
    dest = tracking.lane(shipment, facilities)["dest"]
    legs, hos = hazards.legs_for_status(st, dest) if dest else ([], 0.0)
    if not legs:
        out.errors.append("no position or destination to plan a route from")
        return out
    out.miles_total = round(legs[-1].mile1, 1)
    wx, out.weather_miles_covered = weather.route_hazards(legs, nws)
    fixed, out.traffic_shifts, out.traffic_miles_covered = traffic.route_traffic(legs, tt, st.delivery_tz, now) \
        if tt.available else ([], [], 0.0)
    if not tt.available:
        out.errors.append("traffic not checked: set TOMTOM_API_KEY in .env")
    elif tt.stop_reason and not out.traffic_shifts:
        out.errors.append(f"traffic not checked: {tt.stop_reason}")
    out.delay = hazards.route_delay(legs, wx, hos_left=hos, fixed=fixed)
    out.weather_minutes = sum(h.added_minutes for h in out.delay.hits if h.hazard.kind.startswith("WEATHER"))
    out.traffic_minutes = sum(h.added_minutes for h in out.delay.hits if h.hazard.kind == "TRAFFIC")
    out.notes = hazards.describe(out.delay, st.delivery_tz)
    out.incidents = [i for s in out.traffic_shifts for i in s.incidents]
    (out.predicted_eta, out.minutes_late, out.minutes_late_mabd,
     out.predicted_status) = hazards.adjusted_status(st, out.delay.minutes)
    return out


def apply(st: TrackingStatus, o: DelayOutlook | None) -> TrackingStatus:
    """The tracking status with the outlook folded in: ETA, lateness and status become the predicted
    ones, and the assumptions say what was added and why. When the outlook alone makes the status
    worse, that reason goes first in `exceptions` (the load watch's LATE_ETA rule titles at-risk
    items with it). Unchanged when the outlook adds nothing."""
    if not o or not o.delay.minutes or not o.predicted_eta:
        return st
    tz_name = st.delivery_tz
    head = (f"weather & traffic forecast adds {_dur(o.delay.minutes)} to the legal-hours ETA "
            f"({geo.fmt_local(o.eta, tz_name)} → {geo.fmt_local(o.predicted_eta, tz_name)})")
    worse = hazards.RANK.get(o.predicted_status, 0) > hazards.RANK.get(st.status, 0)
    exceptions = ([f"{head[0].upper()}{head[1:]}: {o.notes[0]}"] if worse and o.notes else []) + st.exceptions
    gap = round((o.predicted_eta - st.driver_eta).total_seconds() / 60) if st.driver_eta else st.eta_gap_minutes
    return st.model_copy(update=dict(
        eta=o.predicted_eta, minutes_late=o.minutes_late, minutes_late_mabd=o.minutes_late_mabd,
        status=o.predicted_status, eta_gap_minutes=gap, exceptions=exceptions,
        assumptions=st.assumptions + [head] + o.notes))


def _dur(minutes: float) -> str:
    return f"{minutes / 60:.1f} h" if abs(minutes) >= 90 else f"{minutes:.0f} min"


def outlook_all(tracker: tracking.Tracker, nws: weather.NWS | None = None,
                tt: traffic.TomTom | None = None) -> dict[str, DelayOutlook]:
    nws, tt = nws or weather.NWS(), tt or traffic.TomTom()
    plans = list(weather.plans_for(tracker).values())
    weather.prefetch(plans, nws)
    traffic.prefetch(plans, tt)
    out = {}
    for sid, st in tracker.statuses.items():
        if (res := outlook(st, tracker.shipments[sid], tracker.facilities, nws, tt)) is not None:
            out[sid] = res
    nws.save()
    tt.save()
    return out


if __name__ == "__main__":
    read = lambda name: json.loads((SNAPSHOT_DIR / f"{name}.json").read_text(encoding="utf-8"))  # noqa: E731
    tr = tracking.Tracker(read("open_shipments"), {f["facility_id"]: f for f in read("facilities")},
                          read("shipment_events"))
    live = sys.argv[1:] == ["--live"]
    nws = weather.NWS(offline=False if live else None)
    tt = traffic.TomTom(offline=False if live else None)
    print(f"As of {tr.as_of:%Y-%m-%d %H:%M} UTC · weather: {'cache' if nws.offline else 'live NWS'} · "
          f"traffic: {'cache' if tt.offline else 'live TomTom' if tt.key else 'OFF (no TOMTOM_API_KEY)'}")
    results = outlook_all(tr, nws, tt)
    for sid, o in sorted(results.items(), key=lambda kv: -kv[1].delay.minutes):
        change = f"{o.status} → {o.predicted_status}" if o.status != o.predicted_status else o.status
        print(f"\n{sid}  +{o.delay.minutes} min (weather {o.weather_minutes}, traffic {o.traffic_minutes})  {change}")
        for s in o.traffic_shifts:
            print(f"    · TomTom {s.start_place} → {s.end_place}: free-flow {s.free_flow_minutes} min, "
                  f"with traffic {s.predicted_minutes} min, our plan {s.plan_minutes} min → +{s.extra_minutes:.0f}")
        for line in o.notes + o.incidents:
            print(f"    {line}")
    for name, errs in (("NWS", nws.errors), ("TomTom", tt.errors)):
        if errs:
            print(f"\n{len(errs)} {name} request(s) failed, e.g. {errs[0]}")
