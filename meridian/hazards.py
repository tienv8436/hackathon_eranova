"""Hazards on a load's remaining route (weather now, news later) and the delay they add. Owner: agent & evals lead.

The idea: we already know where a truck is (latest check call) and when it will be where (the same
ASSUMED_MPH + hours-of-service plan meridian.tracking uses for the ETA). So we can lay the rest of the
trip out as timed legs and ask, for each leg, "is anything bad happening *there* at *that* time?".
That is what lets us warn a day ahead instead of after the driver calls in with an AO code.

Code computes every number here. Hazard sources (meridian.weather, later meridian.news) only say what,
where and when; they never say how late a load will be.

How a hazard turns into minutes:
  - each leg (≤ step_mi) driven inside a hazard counts as extra "equivalent miles": miles × (1/speed_factor − 1)
    (40% slower = 0.6 factor = each mile takes 1/0.6 as long). Overlapping hazards don't stack: the
    worst one on a leg wins.
  - delay = drive_time(miles + extra) − drive_time(miles), so slower driving also burns the driver's
    hours and can push a 10 h break into the trip, as it would in real life.
  - second-order effects are ignored: a slower truck reaches later legs later, which can move it into
    or out of later weather. Fine at hourly forecast resolution.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from pydantic import BaseModel

from . import geo
from .models import TrackingStatus
from .tracking import (ASSUMED_MPH, AT_RISK_SLACK_MIN, HOS_BREAK_H, HOS_DRIVE_H, HOS_GRACE_H,
                       LATE_TOLERANCE_MIN, SHORT_BREAK_AFTER_H, drive_time)

STEP_MI = 50.0
RANK = {"ON_TIME": 0, "AT_RISK": 1, "LATE": 2}


class Hazard(BaseModel):
    """Something that slows trucks in an area for a time window."""
    key: str                          # stable id, used to group hits (an NWS alert id, a forecast grid+hour, …)
    kind: str                         # WEATHER_FORECAST, WEATHER_ALERT, later ROAD_CLOSURE, STRIKE, …
    label: str                        # "Heavy Snow", "Winter Storm Warning"
    points: list[tuple[float, float]]  # where it applies (each with radius_mi around it)
    radius_mi: float = 30.0
    starts_at: datetime | None = None  # UTC; None = already going
    ends_at: datetime | None = None    # UTC; None = no end given
    speed_factor: float = 1.0         # 0.6 = trucks drive at 60% of normal speed
    source: str = ""                  # "NWS hourly forecast", "NWS alert"
    source_url: str = ""
    evidence: str = ""                # the forecast/alert text that justifies it
    simulated: bool = False

    def applies(self, pos: tuple[float, float], at: datetime) -> bool:
        if self.starts_at and at < self.starts_at:
            return False
        if self.ends_at and at >= self.ends_at:
            return False
        return any(geo.haversine_mi(pos, p) <= self.radius_mi for p in self.points)


@dataclass(frozen=True)
class Leg:
    """A stretch of driving: from mile0 to mile1 of the remaining trip, depart → arrive (UTC)."""
    depart: datetime
    arrive: datetime
    mile0: float
    mile1: float
    mid: tuple[float, float]          # position halfway along the leg
    mid_at: datetime                  # time at the halfway point
    a: tuple[float, float] = (0.0, 0.0)   # position at the start of the leg
    b: tuple[float, float] = (0.0, 0.0)   # position at the end

    @property
    def miles(self) -> float:
        return self.mile1 - self.mile0


class FixedDelay(BaseModel):
    """Driving time a source has already worked out for a stretch (e.g. traffic from a routing API),
    beyond what our plan allows for it. Added to the trip like slower driving, so it also uses up hours."""
    hazard: Hazard                    # what it is, for labelling (speed_factor unused)
    minutes: float
    miles: float
    first_at: datetime
    last_at: datetime
    near: str


class HazardHit(BaseModel):
    hazard: Hazard
    miles: float                      # route miles driven inside it
    first_at: datetime
    last_at: datetime
    near: str                         # "near North Platte, NE"
    added_minutes: int                # this hazard's share of the delay


class RouteDelay(BaseModel):
    minutes: int = 0
    extra_breaks: int = 0             # 10 h breaks the slowdown adds
    hits: list[HazardHit] = []


# --------------------------------------------------------------------------- the plan, as timed legs

def timeline(miles: float, hos_left: float | None) -> list[tuple[str, float]]:
    """The driving plan behind tracking.drive_time, step by step: [("drive"|"rest", hours), …].
    Same rules, so the legs end exactly at the tracking ETA."""
    remaining = miles / ASSUMED_MPH
    cap = HOS_DRIVE_H if hos_left is None else max(hos_left, 0.0)
    out: list[tuple[str, float]] = []
    while remaining > 1e-9:
        if remaining - cap <= HOS_GRACE_H:
            cap = max(cap, remaining)
        if cap <= 0:
            out.append(("rest", HOS_BREAK_H))
            cap = HOS_DRIVE_H
            continue
        drive = min(remaining, cap)
        if drive > SHORT_BREAK_AFTER_H:
            out += [("drive", SHORT_BREAK_AFTER_H), ("rest", 0.5), ("drive", drive - SHORT_BREAK_AFTER_H)]
        else:
            out.append(("drive", drive))
        remaining -= drive
        if remaining > 1e-9:
            out.append(("rest", HOS_BREAK_H))
            cap = HOS_DRIVE_H
    return out


def route_legs(start_at: datetime, start: tuple[float, float], dest: tuple[float, float], miles: float,
               hos_left: float | None, step_mi: float = STEP_MI) -> list[Leg]:
    """Timed driving legs from `start` to `dest` (straight line; `miles` are road miles), each ≤ step_mi."""
    legs: list[Leg] = []
    t, done = start_at, 0.0
    pos = lambda m: geo.interpolate(start, dest, m / miles) if miles else dest  # noqa: E731
    for kind, hours in timeline(miles, hos_left):
        if kind == "rest":
            t += timedelta(hours=hours)
            continue
        seg_end = min(done + hours * ASSUMED_MPH, miles)
        while seg_end - done > 1e-6:
            m1 = min(done + step_mi, seg_end)
            dur = timedelta(hours=(m1 - done) / ASSUMED_MPH)
            legs.append(Leg(depart=t, arrive=t + dur, mile0=done, mile1=m1,
                            mid=pos((done + m1) / 2), mid_at=t + dur / 2, a=pos(done), b=pos(m1)))
            t, done = t + dur, m1
    return legs


def shifts(legs: list[Leg]) -> list[list[Leg]]:
    """Legs grouped into driving shifts (split at the 10 h breaks; a 30-min break stays inside a shift)."""
    out: list[list[Leg]] = []
    for leg in legs:
        if out and leg.depart - out[-1][-1].arrive < timedelta(hours=1):
            out[-1].append(leg)
        else:
            out.append([leg])
    return out


def legs_for_status(st: TrackingStatus, dest: tuple[float, float],
                    step_mi: float = STEP_MI) -> tuple[list[Leg], float]:
    """(legs, driver's hours left at the start) for a load, from its TrackingStatus alone.

    The status gives the position, miles left and ETA but not the two inputs behind the ETA (when
    driving resumes, and the driver's hours left), so they are recovered: the hours left whose plan
    starts closest to when the truck should be rolling (its last report for a truck on the road,
    loading done for one at the shipper; a full 11 h for a dispatched truck). Off by an hour or so at
    worst, which is within hourly forecast resolution. Replace with the exact values once
    tracking.estimate() passes them in (route_legs takes them directly).
    """
    if not (st.position and st.position.lat is not None and st.eta and st.miles_remaining):
        return [], HOS_DRIVE_H
    miles = st.miles_remaining
    if st.stage == "DISPATCHED":
        hos = HOS_DRIVE_H
    else:
        ref = st.position.at if st.stage in ("PICKED_UP", "IN_TRANSIT") else st.as_of + timedelta(hours=1)
        candidates = [HOS_DRIVE_H - i * 0.25 for i in range(int(HOS_DRIVE_H / 0.25) + 1)]
        hos = min(candidates, key=lambda h: (abs((st.eta - drive_time(miles, h)[0] - ref).total_seconds()), -h))
    start_at = st.eta - drive_time(miles, hos)[0]
    return route_legs(start_at, (st.position.lat, st.position.lon), dest, miles, hos, step_mi), hos


def hos_of(legs: list[Leg]) -> float:
    """Driving hours in the first shift of a leg plan (what route_legs was given, as far as it matters)."""
    hours = 0.0
    for a, b in zip(legs, legs[1:] + [None]):
        hours += a.miles / ASSUMED_MPH
        if b is None or (b.depart - a.arrive) >= timedelta(hours=HOS_BREAK_H - 1e-6):
            break
    return hours


# --------------------------------------------------------------------------- hazards -> minutes

def route_delay(legs: list[Leg], hazards: list[Hazard], hos_left: float | None = None,
                fixed: list[FixedDelay] | None = None) -> RouteDelay:
    """Delay the hazards (area/time slowdowns, e.g. weather) and fixed delays (e.g. traffic) add to the
    trip planned as `legs` (see the module docstring for the method). Different sources stack; overlapping
    area hazards don't (the worst one on a leg wins)."""
    fixed = [f for f in fixed or [] if f.minutes > 0]
    if not legs or not (hazards or fixed):
        return RouteDelay()
    miles = legs[-1].mile1
    extra_by: dict[str, float] = {}          # equivalent extra miles, per group
    hit_by: dict[str, HazardHit] = {}
    hit_legs: dict[str, list[Leg]] = {}
    for leg in legs:
        live = [h for h in hazards if h.speed_factor < 1 and h.applies(leg.mid, leg.mid_at)]
        if not live:
            continue
        worst = min(live, key=lambda h: h.speed_factor)
        extra_by[worst.label] = extra_by.get(worst.label, 0.0) + leg.miles * (1 / worst.speed_factor - 1)
        hit_legs.setdefault(worst.label, []).append(leg)
        hit_by.setdefault(worst.label, HazardHit(hazard=worst, miles=0, first_at=leg.depart, last_at=leg.arrive,
                                                 near="", added_minutes=0))
    for key, ls in hit_legs.items():
        hit_by[key] = hit_by[key].model_copy(update=dict(
            miles=round(sum(leg.miles for leg in ls), 1), first_at=ls[0].depart, last_at=ls[-1].arrive,
            near=geo.near(ls[len(ls) // 2].mid)))
    for f in fixed:
        key = f"{f.hazard.label}|{f.first_at.isoformat()}"
        extra_by[key] = extra_by.get(key, 0.0) + f.minutes / 60 * ASSUMED_MPH
        hit_by[key] = HazardHit(hazard=f.hazard, miles=round(f.miles, 1), first_at=f.first_at,
                                last_at=f.last_at, near=f.near, added_minutes=0)
    if not extra_by:
        return RouteDelay()
    hos = hos_of(legs) if hos_left is None else hos_left
    base, base_breaks = drive_time(miles, hos)
    slow, slow_breaks = drive_time(miles + sum(extra_by.values()), hos)
    minutes = round((slow - base).total_seconds() / 60)
    total_extra = sum(extra_by.values())
    hits = [hit_by[key].model_copy(update={"added_minutes": round(minutes * extra / total_extra)})
            for key, extra in sorted(extra_by.items(), key=lambda kv: -kv[1])]
    return RouteDelay(minutes=minutes, extra_breaks=slow_breaks - base_breaks, hits=hits)


def describe(delay: RouteDelay, tz_name: str | None) -> list[str]:
    """One plain-English line per hazard, for TrackingStatus.assumptions / the UI."""
    lines = []
    for h in delay.hits:
        when = f"{geo.fmt_local(h.first_at, tz_name)}–{geo.fmt_local(h.last_at, tz_name, with_day=False)}"
        if h.hazard.speed_factor < 1:
            slower = round((1 - h.hazard.speed_factor) * 100)
            lines.append(f"{h.hazard.label} {h.near}, {when}: assumed {slower}% slower over {h.miles:.0f} mi "
                         f"(+{_dur(h.added_minutes)}) — {h.hazard.source}")
        else:
            lines.append(f"{h.hazard.label} {h.near}, {when}: +{_dur(h.added_minutes)} beyond our "
                         f"{ASSUMED_MPH:g} mph plan ({h.hazard.evidence}) — {h.hazard.source}")
    if delay.extra_breaks:
        lines.append(f"the slowdown runs the driver out of hours: +{delay.extra_breaks} × 10 h break")
    return lines


def adjusted_status(st: TrackingStatus, delay_minutes: int) -> tuple[datetime | None, int | None, int | None, str]:
    """(eta, minutes late vs window, minutes late vs MABD, status) once the hazard delay is added.
    Same thresholds as tracking.estimate; never better than the status without hazards."""
    if not st.eta or not delay_minutes:
        return st.eta, st.minutes_late, st.minutes_late_mabd, st.status
    eta = max(st.eta + timedelta(minutes=delay_minutes), st.as_of)
    late = round((eta - st.window_end).total_seconds() / 60) if st.window_end else None
    late_mabd = round((eta - st.mabd_end).total_seconds() / 60) if st.mabd_end else None
    if (late is not None and late > LATE_TOLERANCE_MIN) or (late_mabd is not None and late_mabd > 0):
        status = "LATE"
    elif late is not None and late > -AT_RISK_SLACK_MIN:
        status = "AT_RISK"
    else:
        status = "ON_TIME"
    if RANK.get(st.status, 0) > RANK[status]:
        status = st.status
    return eta, late, late_mabd, status


def _dur(minutes: float) -> str:
    m = abs(minutes)
    return f"{m / 60:.1f} h" if m >= 90 else f"{m:.0f} min"
