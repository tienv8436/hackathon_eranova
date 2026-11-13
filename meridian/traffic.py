"""Traffic on each load's remaining route, from the TomTom Routing API. Owner: agent & evals lead.

Real data, no LLM. Needs TOMTOM_API_KEY in .env (free key at developer.tomtom.com); without one,
traffic is skipped and says so. For every open load:
  1. split the planned trip (meridian.hazards) into driving shifts: the stretches between 10 h breaks
  2. per shift, ask TomTom for a truck route from where the shift starts to where it ends, departing
     when the plan says the shift starts. With a future departure TomTom uses typical traffic for that
     day and hour (rush hours), plus live incidents and planned road works/closures (up to 60 days out).
     Summary fields used:
       noTrafficTravelTimeInSeconds   free-flow driving
       travelTimeInSeconds            TomTom's estimate with traffic (what we use)
       historicTrafficTravelTimeInSeconds / liveTrafficIncidentsTravelTimeInSeconds  shown for context
     Traffic sections (jams, road works, closures) are listed with where and roughly when.
  3. count only what our plan doesn't already allow for:
         extra = travel time − max(our plan's driving time, free-flow time)        (never negative)
     Our plan drives at ASSUMED_MPH (50 mph), slower than free-flow truck speed on most interstates, so
     ordinary traffic is already inside the ETA. Traffic adds time only once it eats through that slack.
     The extra goes to meridian.hazards as a fixed delay, so it also uses up the driver's hours.

Shifts that are already over (the data's "as of" is in the past) are skipped; one in progress is
routed from now. Responses are cached in snapshots/traffic_cache.json (parsed, without the API key).
With DEMO_MODE=1 only that file is read.
"""

from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Callable

import httpx
from pydantic import BaseModel

from . import geo, hazards
from .client import ROOT, SNAPSHOT_DIR, demo_mode
from .geo import UTC
from .hazards import FixedDelay, Hazard, Leg
from .tracking import ASSUMED_MPH

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

TOMTOM_URL = "https://api.tomtom.com/routing/1/calculateRoute"
CACHE_FILE = SNAPSHOT_DIR / "traffic_cache.json"
TTL = timedelta(hours=1)
DEPART_ROUND = timedelta(minutes=15)    # departures rounded to 15 min, so re-runs share cached routes
VEHICLE_KG = 36000                      # ~80,000 lb loaded tractor-trailer: applies weight restrictions
NOTABLE_DELAY_S = 300                   # list traffic sections that cost at least this…
NOTABLE_KINDS = {"ROAD_CLOSURE", "ROAD_WORK"}   # …or are closures / road works
WORKERS = 2
QPS = float(os.environ.get("TOMTOM_QPS") or 2)   # requests per second, shared by all threads (free tier limits QPS)
RETRIES_429 = 3                         # on "rate limit exceeded": wait (Retry-After, else 1, 2, 4 s) and retry
KIND_TEXT = {"JAM": "Traffic jam", "ROAD_WORK": "Road work", "ROAD_CLOSURE": "Road closure", "OTHER": "Traffic incident"}
MAGNITUDE = {1: "minor", 2: "moderate", 3: "major", 4: "indefinite"}


class ShiftTraffic(BaseModel):
    """TomTom's view of one driving shift."""
    depart: datetime                  # when TomTom was asked to depart (UTC)
    start_place: str
    end_place: str
    plan_miles: float
    tomtom_miles: float
    plan_minutes: float               # our plan: plan_miles at ASSUMED_MPH
    free_flow_minutes: float
    typical_minutes: float | None     # historic traffic for that day/hour
    live_minutes: float | None        # live incidents
    predicted_minutes: float          # TomTom's estimate with traffic
    extra_minutes: float              # beyond max(plan, free flow): what we add
    incidents: list[str] = []


# --------------------------------------------------------------------------- TomTom client with a cache

class TomTom:
    """Cached truck routes. `http_get(url, params) -> dict | None` can be swapped out in tests."""

    def __init__(self, key: str | None = None, offline: bool | None = None, cache_path=CACHE_FILE,
                 http_get: Callable[[str, dict], dict | None] | None = None):
        self.key = os.environ.get("TOMTOM_API_KEY", "") if key is None else key
        self.offline = demo_mode() if offline is None else offline
        self.cache_path = cache_path
        self._lock = threading.Lock()
        self._http_get = http_get or self._fetch
        self._http: httpx.Client | None = None
        try:
            self.cache: dict[str, dict] = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path and cache_path.exists() else {}
        except (OSError, ValueError):
            self.cache = {}
        self.errors: list[str] = []
        self.stop_reason: str | None = None     # set when further calls this run would fail the same way
        self._sleep = time.sleep
        self._next_slot = 0.0

    @property
    def available(self) -> bool:
        return bool(self.key) or (self.offline and bool(self.cache))

    @property
    def denied(self) -> bool:
        return self.stop_reason is not None

    def _throttle(self) -> None:
        """Space requests 1/QPS apart across all threads."""
        with self._lock:
            now = time.monotonic()
            wait = max(0.0, self._next_slot - now)
            self._next_slot = max(now, self._next_slot) + 1 / QPS
        if wait:
            self._sleep(wait)

    def _fetch(self, url: str, params: dict) -> dict | None:
        if self._http is None:
            self._http = httpx.Client(timeout=30)
        for attempt in range(RETRIES_429 + 1):
            if self.stop_reason:
                return None
            self._throttle()
            try:
                r = self._http.get(url, params=params)
            except httpx.HTTPError as e:
                self._error(f"{type(e).__name__} calling TomTom")
                return None
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429 and attempt < RETRIES_429:
                try:
                    wait = float(r.headers.get("Retry-After", ""))
                except ValueError:
                    wait = 2.0 ** attempt
                self._sleep(min(wait, 30))
                continue
            break
        try:
            body = r.json()
            msg = (body.get("detailedError") or {}).get("message") or (body.get("error") or {}).get("description") or ""
        except ValueError:
            msg = ""
        err = f"TomTom HTTP {r.status_code}{': ' + msg if msg else ''}"   # never log the URL: it has the key
        self._error(err)
        if r.status_code in (401, 403):        # key rejected: every other call would fail the same way
            self.stop_reason = f"TomTom rejected the key ({err})"
        elif r.status_code == 429:             # still limited after backing off: likely the daily quota
            self.stop_reason = f"TomTom rate limit still hit after {RETRIES_429} retries ({err})"
        return None

    def _error(self, msg: str) -> None:
        with self._lock:
            self.errors.append(msg)

    def route(self, a: tuple[float, float], b: tuple[float, float], depart: datetime) -> dict | None:
        """Parsed truck route a → b departing at `depart` (see _parse), or None."""
        dep = _round_up(depart)
        locs = f"{a[0]:.4f},{a[1]:.4f}:{b[0]:.4f},{b[1]:.4f}"
        cache_key = f"{locs}@{dep:%Y-%m-%dT%H:%M}Z"
        now = datetime.now(UTC)
        with self._lock:
            hit = self.cache.get(cache_key)
        if hit and (self.offline or now - geo.parse_utc(hit["fetched_at"]) < TTL):
            return hit["data"]
        if self.offline or not self.key or self.denied:
            return (hit or {}).get("data")
        raw = self._http_get(f"{TOMTOM_URL}/{locs}/json", {
            "key": self.key, "departAt": f"{dep:%Y-%m-%dT%H:%M:%S}Z", "traffic": "true",
            "travelMode": "truck", "vehicleCommercial": "true", "vehicleWeight": VEHICLE_KG,
            "computeTravelTimeFor": "all", "sectionType": "traffic", "routeRepresentation": "polyline"})
        data = _parse(raw)
        if data is None:
            return (hit or {}).get("data")
        with self._lock:
            self.cache[cache_key] = {"fetched_at": now.isoformat(), "data": data}
        return data

    def save(self, keep: timedelta = timedelta(days=2)) -> None:
        if self.offline or not self.cache_path or not self.cache:
            return
        cutoff = datetime.now(UTC) - keep
        with self._lock:
            items = {k: v for k, v in self.cache.items() if geo.parse_utc(v["fetched_at"]) >= cutoff}
        self.cache_path.parent.mkdir(exist_ok=True)
        self.cache_path.write_text(json.dumps(items), encoding="utf-8")


def _round_up(t: datetime) -> datetime:
    t = t.astimezone(UTC).replace(second=0, microsecond=0)
    step = int(DEPART_ROUND.total_seconds() // 60)
    return t + timedelta(minutes=(-t.minute) % step)


def _parse(raw: dict | None) -> dict | None:
    """The bits of a TomTom calculateRoute response we use (the full one carries every route point)."""
    try:
        route = raw["routes"][0]
        s = route["summary"]
    except (TypeError, KeyError, IndexError):
        return None
    points = [(p["latitude"], p["longitude"]) for leg in route.get("legs") or [] for p in leg.get("points") or []]
    incidents = []
    for sec in route.get("sections") or []:
        if sec.get("sectionType") != "TRAFFIC":
            continue
        i0, i1 = sec.get("startPointIndex", 0), sec.get("endPointIndex", 0)
        mid = (i0 + i1) // 2
        incidents.append({
            "kind": sec.get("simpleCategory") or "OTHER", "magnitude": sec.get("magnitudeOfDelay"),
            "delay_s": sec.get("delayInSeconds") or 0, "speed_kmh": sec.get("effectiveSpeedInKmh"),
            "frac": mid / max(len(points) - 1, 1), "at_point": points[mid] if 0 <= mid < len(points) else None})
    return {"length_m": s.get("lengthInMeters", 0), "travel_s": s.get("travelTimeInSeconds", 0),
            "no_traffic_s": s.get("noTrafficTravelTimeInSeconds", s.get("travelTimeInSeconds", 0)),
            "historic_s": s.get("historicTrafficTravelTimeInSeconds"),
            "live_s": s.get("liveTrafficIncidentsTravelTimeInSeconds"),
            "traffic_delay_s": s.get("trafficDelayInSeconds", 0), "incidents": incidents}


# --------------------------------------------------------------------------- the plan's shifts -> fixed delays

def _city(pos: tuple[float, float]) -> str:
    (city, state), _ = min(geo.CITIES.items(), key=lambda kv: geo.haversine_mi(pos, kv[1]))
    return f"{city}, {state}"


def _shift_request(shift: list[Leg], now: datetime) -> tuple[tuple[float, float], tuple[float, float], datetime, float] | None:
    """(from, to, depart, plan miles) to ask TomTom for this shift: from the first leg that hasn't started
    yet (≤ 50 mi in), so the request, and its cache key, don't change with the clock; None if nothing
    is left to drive. Departures are always in the future."""
    todo = [leg for leg in shift if leg.depart >= now]
    if not todo:
        return None
    return todo[0].a, shift[-1].b, todo[0].depart, sum(leg.miles for leg in todo)


def prefetch(all_legs: list[list[Leg]], tt: TomTom, now: datetime | None = None) -> None:
    now = now or datetime.now(UTC)
    reqs = [r for legs in all_legs for sh in hazards.shifts(legs) if (r := _shift_request(sh, now))]
    if reqs and tt.key and not tt.offline:
        with ThreadPoolExecutor(WORKERS) as pool:
            list(pool.map(lambda r: tt.route(r[0], r[1], r[2]), reqs))


def route_traffic(legs: list[Leg], tt: TomTom, tz_name: str | None = None, now: datetime | None = None
                  ) -> tuple[list[FixedDelay], list[ShiftTraffic], float]:
    """(fixed delays for meridian.hazards.route_delay, per-shift detail, miles TomTom covered).
    `now` (default: the clock) decides which shifts are already over."""
    now = now or datetime.now(UTC)
    fixed: list[FixedDelay] = []
    detail: list[ShiftTraffic] = []
    covered = 0.0
    for shift in hazards.shifts(legs):
        req = _shift_request(shift, now)
        if not req:
            continue
        a, b, depart, plan_mi = req
        r = tt.route(a, b, depart)
        if not r:
            continue
        covered += plan_mi
        plan_min = plan_mi / ASSUMED_MPH * 60
        free, pred = r["no_traffic_s"] / 60, r["travel_s"] / 60
        extra = max(0.0, pred - max(plan_min, free))
        incidents = []
        for inc in r["incidents"]:
            if inc["delay_s"] < NOTABLE_DELAY_S and inc["kind"] not in NOTABLE_KINDS:
                continue
            at = depart + timedelta(seconds=r["travel_s"] * inc["frac"])
            where = geo.near(tuple(inc["at_point"])) if inc["at_point"] else f"between {_city(a)} and {_city(b)}"
            mag = MAGNITUDE.get(inc["magnitude"] or 0)
            incidents.append(f"{KIND_TEXT.get(inc['kind'], 'Traffic incident')}{f' ({mag})' if mag else ''} {where}, "
                             f"~{geo.fmt_local(at, tz_name)}: "
                             + (f"+{inc['delay_s'] / 60:.0f} min in TomTom's estimate" if inc["delay_s"] >= 30
                                else "no delay expected by TomTom"))
        st = ShiftTraffic(depart=depart, start_place=_city(a), end_place=_city(b), plan_miles=round(plan_mi, 1),
                          tomtom_miles=round(r["length_m"] / 1609.344, 1), plan_minutes=round(plan_min),
                          free_flow_minutes=round(free), predicted_minutes=round(pred), extra_minutes=round(extra),
                          typical_minutes=round(r["historic_s"] / 60) if r.get("historic_s") else None,
                          live_minutes=round(r["live_s"] / 60) if r.get("live_s") else None, incidents=incidents)
        detail.append(st)
        if extra >= 1:
            evidence = (f"TomTom {_h(pred)} with traffic vs {_h(free)} free-flow; our plan allows {_h(plan_min)}")
            hz = Hazard(key=f"traffic:{depart.isoformat()}", kind="TRAFFIC", label="Traffic", points=[a, b],
                        source="TomTom", evidence=evidence)
            fixed.append(FixedDelay(hazard=hz, minutes=extra, miles=plan_mi, first_at=depart,
                                    last_at=depart + timedelta(minutes=pred),
                                    near=(f"around {st.start_place}" if st.start_place == st.end_place
                                          else f"between {st.start_place} and {st.end_place}")))
    return fixed, detail, round(covered, 1)


def _h(minutes: float) -> str:
    return f"{minutes / 60:.1f} h" if minutes >= 90 else f"{minutes:.0f} min"
