"""Weather on each load's remaining route, from the US National Weather Service. Owner: agent & evals lead.

Real data, no LLM, no API key. For every open load:
  1. lay the rest of the trip out as timed legs (meridian.hazards.legs_for_status)
  2. for each leg's midpoint, ask NWS for the hourly forecast and active alerts there
       GET /points/{lat},{lon}          -> which forecast grid covers the point (cached for good)
       GET {forecastHourly}             -> conditions hour by hour, ~6 days out (cached 1 h)
       GET /alerts/active?point=lat,lon -> warnings/advisories, including ones starting later (cached 15 min)
  3. turn conditions at the hour the truck is there into a speed factor (tables below) and let
     meridian.hazards work out the delay, with hours of service.

Speed factors are our planning assumptions, roughly in line with FHWA road-weather research (freeway
speeds drop a few percent in light rain and up to ~40% in heavy snow). Precipitation counts only when
NWS gives it at least a MIN_POP chance. They are listed next to every delay so a dispatcher can judge.

Responses are cached in snapshots/weather_cache.json. With DEMO_MODE=1 only that file is read (no
network), so the demo shows whatever was last fetched. Refresh it:   python -m meridian.weather
"""

from __future__ import annotations

import json
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Callable

import httpx
from pydantic import BaseModel

from . import geo, hazards, tracking
from .client import SNAPSHOT_DIR, demo_mode
from .geo import UTC
from .hazards import Hazard, RouteDelay
from .models import TrackingStatus

NWS_BASE = "https://api.weather.gov"
USER_AGENT = "broker-desk-copilot/0.1 (hackathon demo)"   # NWS asks every client to send a User-Agent
CACHE_FILE = SNAPSHOT_DIR / "weather_cache.json"
TTL = {"points": None, "forecast": timedelta(hours=1), "alerts": timedelta(minutes=15)}
SNAP_DEG = 0.25           # legs are looked up on a 0.25° grid (~15 mi), so loads on the same corridor share calls
POINT_RADIUS_MI = 20.0    # a looked-up point stands for this radius around it
MIN_POP = 40              # % chance of precipitation below which a forecast doesn't count
HIGH_WIND_MPH = 40
WORKERS = 6

# Hourly forecast text -> (label, speed factor). First match wins, so the worst conditions go first.
FORECAST_FACTORS: list[tuple[str, str, float, bool]] = [
    # regex on NWS shortForecast, label, factor, is precipitation (subject to MIN_POP)
    (r"blizzard", "Blizzard", 0.50, True),
    (r"freezing rain|freezing drizzle|ice|sleet", "Freezing rain / ice", 0.60, True),
    (r"heavy snow", "Heavy snow", 0.60, True),
    (r"light snow|flurries", "Light snow", 0.85, True),
    (r"snow", "Snow", 0.75, True),
    (r"heavy rain", "Heavy rain", 0.85, True),
    (r"thunderstorm|t-storm", "Thunderstorms", 0.85, True),
    (r"dense fog", "Dense fog", 0.85, False),
    (r"rain|showers", "Rain", 0.92, True),
    (r"drizzle", "Drizzle", 0.95, True),
    (r"fog", "Fog", 0.95, False),
]

# NWS alert event -> speed factor. Events not listed (heat, frost, marine, …) don't slow trucks.
ALERT_FACTORS: dict[str, float] = {
    "Blizzard Warning": 0.50, "Ice Storm Warning": 0.55, "Dust Storm Warning": 0.60,
    "Winter Storm Warning": 0.65, "Lake Effect Snow Warning": 0.65, "Snow Squall Warning": 0.60,
    "Freezing Rain Advisory": 0.70, "High Wind Warning": 0.80, "Winter Weather Advisory": 0.85,
    "Dense Fog Advisory": 0.85, "Flash Flood Warning": 0.85, "Flood Warning": 0.90,
}


class WeatherCheck(BaseModel):
    """Weather on one load's remaining route and what it does to the ETA."""
    shipment_id: str
    checked_at: datetime
    source: str = "NWS (api.weather.gov)"
    miles_total: float = 0
    miles_covered: float = 0          # miles whose time-of-passage the forecast reaches
    delay: RouteDelay = RouteDelay()
    notes: list[str] = []             # one line per hazard hit, ready for the UI / assumptions
    eta: datetime | None = None       # tracking ETA, without weather
    eta_with_weather: datetime | None = None
    minutes_late: int | None = None   # with weather, vs window end
    minutes_late_mabd: int | None = None
    status: str = ""                  # tracking status, without weather
    status_with_weather: str = ""
    errors: list[str] = []


# --------------------------------------------------------------------------- NWS client with a cache

class NWS:
    """Cached GETs against api.weather.gov. `http_get(url) -> dict | None` can be swapped out in tests."""

    def __init__(self, offline: bool | None = None, cache_path=CACHE_FILE,
                 http_get: Callable[[str], dict | None] | None = None):
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

    def _fetch(self, url: str) -> dict | None:
        if self._http is None:
            self._http = httpx.Client(headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json"},
                                      timeout=20, follow_redirects=True)
        for _ in range(2):                    # NWS gridpoint endpoints return a transient 500 now and then
            try:
                r = self._http.get(url)
            except httpx.HTTPError as e:
                err = f"{type(e).__name__}"
                continue
            if r.status_code == 200:
                return r.json()
            err = f"HTTP {r.status_code}"
            if r.status_code == 404:          # outside NWS coverage (e.g. Mexico): don't retry
                break
        with self._lock:
            self.errors.append(f"{err} for {url}")
        return None

    def get(self, url: str, ttl: timedelta | None) -> dict | None:
        now = datetime.now(UTC)
        with self._lock:
            hit = self.cache.get(url)
        if hit and (self.offline or ttl is None or now - geo.parse_utc(hit["fetched_at"]) < ttl):
            return hit["data"]
        if self.offline:
            return None
        data = _slim(self._http_get(url))
        if data is not None:
            with self._lock:
                self.cache[url] = {"fetched_at": now.isoformat(), "data": data}
        return data if data is not None else (hit or {}).get("data")   # stale beats nothing

    def save(self, keep: timedelta = timedelta(days=2)) -> None:
        if self.offline or not self.cache_path:
            return
        cutoff = datetime.now(UTC) - keep
        with self._lock:
            keep_items = {u: v for u, v in self.cache.items()
                          if "/points/" in u or geo.parse_utc(v["fetched_at"]) >= cutoff}
        self.cache_path.parent.mkdir(exist_ok=True)
        self.cache_path.write_text(json.dumps(keep_items), encoding="utf-8")

    # ------------------------------------------------------------------ per point

    def hourly(self, pt: tuple[float, float]) -> list[dict]:
        meta = self.get(f"{NWS_BASE}/points/{pt[0]:.4f},{pt[1]:.4f}", TTL["points"])
        url = ((meta or {}).get("properties") or {}).get("forecastHourly")
        data = self.get(url, TTL["forecast"]) if url else None
        return ((data or {}).get("properties") or {}).get("periods") or []

    def alerts(self, pt: tuple[float, float]) -> list[dict]:
        data = self.get(f"{NWS_BASE}/alerts/active?point={pt[0]:.4f},{pt[1]:.4f}", TTL["alerts"])
        return [f.get("properties") or {} for f in (data or {}).get("features") or []]


PERIOD_FIELDS = ("startTime", "endTime", "shortForecast", "probabilityOfPrecipitation", "windSpeed", "windGust")
ALERT_FIELDS = ("id", "@id", "event", "headline", "onset", "effective", "ends", "expires", "areaDesc")


def _slim(data: dict | None) -> dict | None:
    """Keep only the fields we read (NWS responses carry geometry and much more: ~50 KB per forecast)."""
    if not isinstance(data, dict):
        return data
    props = data.get("properties") or {}
    if "periods" in props:
        return {"properties": {"periods": [{k: p.get(k) for k in PERIOD_FIELDS if k in p} for p in props["periods"]]}}
    if "forecastHourly" in props:
        return {"properties": {"forecastHourly": props["forecastHourly"]}}
    if "features" in data:
        return {"features": [{"properties": {k: (f.get("properties") or {}).get(k) for k in ALERT_FIELDS}}
                             for f in data["features"]]}
    return data


# --------------------------------------------------------------------------- conditions -> factors

def snap(pos: tuple[float, float]) -> tuple[float, float]:
    return round(round(pos[0] / SNAP_DEG) * SNAP_DEG, 4), round(round(pos[1] / SNAP_DEG) * SNAP_DEG, 4)


def _mph(text: str | None) -> float:
    nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", text or "")]
    return max(nums) if nums else 0.0


def forecast_factor(short: str | None, pop: float | None, wind: str | None = None,
                    gust: str | None = None) -> tuple[str, float] | None:
    """(label, speed factor) for one hourly period, or None if it shouldn't slow a truck."""
    text = (short or "").lower()
    found = None
    for pattern, label, factor, precip in FORECAST_FACTORS:
        if re.search(pattern, text):
            if precip and pop is not None and pop < MIN_POP:
                continue
            found = (label, factor)
            break
    if max(_mph(wind), _mph(gust)) >= HIGH_WIND_MPH and (not found or found[1] > 0.85):
        found = ("High winds", 0.85)
    return found


def _period_hazards(pt: tuple[float, float], periods: list[dict], t0: datetime, t1: datetime) -> list[Hazard]:
    out = []
    for p in periods:
        start, end = geo.parse_utc(p.get("startTime")), geo.parse_utc(p.get("endTime"))
        if not start or not end or end <= t0 or start >= t1:
            continue
        pop = (p.get("probabilityOfPrecipitation") or {}).get("value")
        f = forecast_factor(p.get("shortForecast"), pop, p.get("windSpeed"), p.get("windGust"))
        if not f:
            continue
        evidence = f"{p.get('shortForecast')}" + (f", {pop:.0f}% chance" if pop is not None else "") \
            + (f", wind {p.get('windSpeed')}" if p.get("windSpeed") else "")
        out.append(Hazard(key=f"fc:{pt}:{start.isoformat()}", kind="WEATHER_FORECAST", label=f[0],
                          points=[pt], radius_mi=POINT_RADIUS_MI, starts_at=start, ends_at=end,
                          speed_factor=f[1], source="NWS hourly forecast", evidence=evidence))
    return out


def _alert_hazard(pt: tuple[float, float], a: dict) -> Hazard | None:
    factor = ALERT_FACTORS.get(a.get("event") or "")
    if factor is None:
        return None
    return Hazard(key=f"alert:{a.get('id')}", kind="WEATHER_ALERT", label=a["event"], points=[pt],
                  radius_mi=POINT_RADIUS_MI, speed_factor=factor,
                  starts_at=geo.parse_utc(a.get("onset") or a.get("effective")),
                  ends_at=geo.parse_utc(a.get("ends") or a.get("expires")),
                  source="NWS alert", source_url=a.get("@id") or a.get("id") or "",
                  evidence=a.get("headline") or a["event"])


def _covered(periods: list[dict], at: datetime) -> bool:
    return any((s := geo.parse_utc(p.get("startTime"))) and (e := geo.parse_utc(p.get("endTime"))) and s <= at < e
               for p in periods)


# --------------------------------------------------------------------------- one load / all loads

def route_hazards(legs: list[hazards.Leg], nws: NWS) -> tuple[list[Hazard], float]:
    """(weather hazards along the planned legs, miles whose time of passage the forecast reaches)."""
    if not legs:
        return [], 0.0
    t0, t1 = legs[0].depart, legs[-1].arrive
    found: list[Hazard] = []
    alerts: dict[str, Hazard] = {}
    covered = 0.0
    for pt in dict.fromkeys(snap(leg.mid) for leg in legs):
        periods = nws.hourly(pt)
        found += _period_hazards(pt, periods, t0, t1)
        for a in nws.alerts(pt):
            if h := _alert_hazard(pt, a):
                if h.key in alerts:
                    alerts[h.key].points.append(pt)
                else:
                    alerts[h.key] = h
        covered += sum(leg.miles for leg in legs if snap(leg.mid) == pt and _covered(periods, leg.mid_at))
    return found + list(alerts.values()), round(covered, 1)


def prefetch(all_legs: list[list[hazards.Leg]], nws: NWS) -> None:
    """Fetch every route point's forecast and alerts in parallel, so the per-load checks hit the cache."""
    points = list(dict.fromkeys(snap(leg.mid) for legs in all_legs for leg in legs))
    if points and not nws.offline:
        with ThreadPoolExecutor(WORKERS) as pool:
            list(pool.map(lambda p: (nws.hourly(p), nws.alerts(p)), points))


def check_load(st: TrackingStatus, shipment: dict, facilities: dict[str, dict], nws: NWS,
               step_mi: float = hazards.STEP_MI) -> WeatherCheck | None:
    """Weather on the rest of this load's route. None for loads that aren't on their way (arrived, no ETA)."""
    if st.stage == "AT_DELIVERY" or st.status in ("ARRIVED", "NOT_TRACKED") or not st.eta:
        return None
    dest = tracking.lane(shipment, facilities)["dest"]
    legs, hos = hazards.legs_for_status(st, dest, step_mi) if dest else ([], 0.0)
    out = WeatherCheck(shipment_id=st.shipment_id, checked_at=datetime.now(UTC), eta=st.eta,
                       eta_with_weather=st.eta, minutes_late=st.minutes_late,
                       minutes_late_mabd=st.minutes_late_mabd, status=st.status, status_with_weather=st.status)
    if not legs:
        out.errors.append("no position or destination to plan a route from")
        return out
    out.miles_total = round(legs[-1].mile1, 1)
    found, out.miles_covered = route_hazards(legs, nws)
    out.delay = hazards.route_delay(legs, found, hos_left=hos)
    out.notes = hazards.describe(out.delay, st.delivery_tz)
    (out.eta_with_weather, out.minutes_late, out.minutes_late_mabd,
     out.status_with_weather) = hazards.adjusted_status(st, out.delay.minutes)
    return out


def plans_for(tracker: tracking.Tracker) -> dict[str, list[hazards.Leg]]:
    """Planned legs for every load still on its way."""
    plans = {}
    for sid, st in tracker.statuses.items():
        dest = tracking.lane(tracker.shipments[sid], tracker.facilities)["dest"]
        if dest and st.eta and st.stage != "AT_DELIVERY" and st.status not in ("ARRIVED", "NOT_TRACKED"):
            plans[sid] = hazards.legs_for_status(st, dest)[0]
    return plans


def check_all(tracker: tracking.Tracker, nws: NWS | None = None) -> dict[str, WeatherCheck]:
    """Weather for every load the tracker follows. Fetches all route points in parallel first."""
    nws = nws or NWS()
    prefetch(list(plans_for(tracker).values()), nws)
    out = {}
    for sid, st in tracker.statuses.items():
        if (res := check_load(st, tracker.shipments[sid], tracker.facilities, nws)) is not None:
            out[sid] = res
    nws.save()
    return out


# --------------------------------------------------------------------------- CLI

def _tracker_from_snapshots() -> tracking.Tracker:
    read = lambda name: json.loads((SNAPSHOT_DIR / f"{name}.json").read_text(encoding="utf-8"))  # noqa: E731
    return tracking.Tracker(read("open_shipments"), {f["facility_id"]: f for f in read("facilities")},
                            read("shipment_events"))


if __name__ == "__main__":
    tr = _tracker_from_snapshots()
    nws = NWS(offline=False if sys.argv[1:] == ["--live"] else None)
    print(f"As of {tr.as_of:%Y-%m-%d %H:%M} UTC · {'cache only (DEMO_MODE=1)' if nws.offline else 'live NWS'}")
    results = check_all(tr, nws)
    for sid, w in sorted(results.items(), key=lambda kv: -kv[1].delay.minutes):
        change = f"{w.status} → {w.status_with_weather}" if w.status != w.status_with_weather else w.status
        print(f"\n{sid}  +{w.delay.minutes} min  {change}  "
              f"(forecast reaches {w.miles_covered:.0f}/{w.miles_total:.0f} mi)")
        for line in w.notes + w.errors:
            print(f"    {line}")
    if nws.errors:
        print(f"\n{len(nws.errors)} NWS request(s) failed, e.g. {nws.errors[0]}")
    print(f"\nCache: {CACHE_FILE}")
