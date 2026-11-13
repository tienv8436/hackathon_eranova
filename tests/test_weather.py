"""Route legs, hazard delay and the NWS weather check, with canned NWS responses (no network)."""

from datetime import datetime, timedelta

import pytest

from meridian import geo, hazards, tracking, weather
from meridian.geo import UTC
from meridian.hazards import Hazard
from tests.test_tracking import DEPARTED, FACILITIES, call, load

CHI, DAL = geo.CITIES[("Chicago", "IL")], geo.CITIES[("Dallas", "TX")]
T0 = datetime(2026, 11, 14, 15, 0, tzinfo=UTC)


def everywhere(factor: float, label: str = "Heavy snow", **kw) -> Hazard:
    return Hazard(key=label, kind="WEATHER_FORECAST", label=label, points=[CHI, DAL], radius_mi=5000,
                  speed_factor=factor, source="test", **kw)


# --------------------------------------------------------------------------- the leg plan

@pytest.mark.parametrize("miles,hos", [(100, 11), (550, 11), (550, 3), (925, 5.5), (1400, None), (300, 0)])
def test_timeline_matches_tracking_drive_time(miles, hos):
    total = sum(h for _, h in hazards.timeline(miles, hos))
    assert total == pytest.approx(tracking.drive_time(miles, hos)[0].total_seconds() / 3600)


def test_legs_cover_the_route_and_end_at_the_eta():
    legs = hazards.route_legs(T0, CHI, DAL, 925, 5.5, step_mi=50)
    assert legs[0].depart == T0
    assert legs[-1].arrive == T0 + tracking.drive_time(925, 5.5)[0]
    assert legs[-1].mile1 == pytest.approx(925)
    assert all(leg.miles <= 50 + 1e-6 for leg in legs)
    assert geo.haversine_mi(legs[-1].mid, DAL) < 30
    # the 10 h break after the first 5.5 h shows up as a gap between legs
    gaps = [b.depart - a.arrive for a, b in zip(legs, legs[1:])]
    assert max(gaps) == timedelta(hours=10)


def test_legs_for_status_reproduce_the_tracking_eta():
    events = [DEPARTED, call("2026-11-14T21:00:00Z", "Springfield", "MO")]
    st = tracking.estimate(load(), FACILITIES, events, datetime(2026, 11, 14, 21, 30, tzinfo=UTC))
    legs, _ = hazards.legs_for_status(st, DAL)
    assert legs and legs[-1].arrive == st.eta
    assert legs[0].depart >= st.position.at - timedelta(minutes=1)


# --------------------------------------------------------------------------- hazards -> minutes

def test_slower_driving_adds_time():
    legs = hazards.route_legs(T0, CHI, DAL, 200, 11)
    d = hazards.route_delay(legs, [everywhere(0.5)], hos_left=11)
    assert d.minutes == 240                      # 200 mi at 50 mph = 4 h; at half speed 8 h
    assert d.extra_breaks == 0


def test_slowdown_that_runs_out_the_driver_adds_a_break():
    legs = hazards.route_legs(T0, CHI, DAL, 500, 11)        # 10 h: fits one shift
    d = hazards.route_delay(legs, [everywhere(0.8)], hos_left=11)
    assert d.extra_breaks == 1
    assert d.minutes > 10 * 60
    assert "out of hours" in hazards.describe(d, "America/Chicago")[-1]


def test_hazards_outside_the_time_window_or_area_dont_count():
    legs = hazards.route_legs(T0, CHI, DAL, 925, 11)
    later = everywhere(0.5, starts_at=legs[-1].arrive + timedelta(hours=1))
    far = Hazard(key="x", kind="WEATHER_ALERT", label="Blizzard Warning", points=[(47.6, -122.3)],
                 radius_mi=30, speed_factor=0.5)
    assert hazards.route_delay(legs, [later, far]).minutes == 0


def test_overlapping_hazards_dont_stack():
    legs = hazards.route_legs(T0, CHI, DAL, 300, 11)
    one = hazards.route_delay(legs, [everywhere(0.6, "Snow")], hos_left=11).minutes
    both = hazards.route_delay(legs, [everywhere(0.6, "Snow"), everywhere(0.85, "Rain")], hos_left=11)
    assert both.minutes == one and [h.hazard.label for h in both.hits] == ["Snow"]


def test_adjusted_status_never_improves():
    events = [DEPARTED, call("2026-11-14T21:00:00Z", "Springfield", "MO")]
    st = tracking.estimate(load(), FACILITIES, events, datetime(2026, 11, 14, 21, 30, tzinfo=UTC))
    eta, late, _, status = hazards.adjusted_status(st, 0)
    assert (eta, status) == (st.eta, st.status)
    eta, late, _, status = hazards.adjusted_status(st, 24 * 60)     # Mon 09:23 CST: still inside the 06–12 window
    assert eta == st.eta + timedelta(hours=24) and status == "ON_TIME"
    eta, late, _, status = hazards.adjusted_status(st, 48 * 60)     # Tue: misses it
    assert late > 0 and status == "LATE"


# --------------------------------------------------------------------------- NWS text -> factors

@pytest.mark.parametrize("short,pop,expected", [
    ("Heavy Snow", 90, ("Heavy snow", 0.60)),
    ("Light Snow Likely", 60, ("Light snow", 0.85)),
    ("Chance Rain Showers", 30, None),                     # below MIN_POP
    ("Rain And Snow", 80, ("Snow", 0.75)),                  # worst wins
    ("Freezing Rain", 70, ("Freezing rain / ice", 0.60)),
    ("Patchy Fog", 0, ("Fog", 0.95)),                       # fog isn't gated on precip chance
    ("Sunny", 0, None),
])
def test_forecast_factor(short, pop, expected):
    assert weather.forecast_factor(short, pop) == expected


def test_high_wind_counts():
    assert weather.forecast_factor("Sunny", 0, "25 to 45 mph") == ("High winds", 0.85)
    assert weather.forecast_factor("Heavy Snow", 90, "45 mph") == ("Heavy snow", 0.60)


# --------------------------------------------------------------------------- a whole load, canned NWS

def fake_nws(tmp_path, snow_from: datetime, snow_hours: int, alert: dict | None = None):
    """Every point: heavy snow for `snow_hours` from `snow_from`, sunny otherwise; one alert everywhere if given."""
    calls = []

    def http_get(url: str):
        calls.append(url)
        if "/points/" in url:
            return {"properties": {"forecastHourly": f"{weather.NWS_BASE}/gridpoints/X/1,1/forecast/hourly"}}
        if "/forecast/hourly" in url:
            periods = []
            for h in range(0, 120):
                s = T0 - timedelta(hours=6) + timedelta(hours=h)
                snow = snow_from <= s < snow_from + timedelta(hours=snow_hours)
                periods.append({"startTime": s.isoformat(), "endTime": (s + timedelta(hours=1)).isoformat(),
                                "shortForecast": "Heavy Snow" if snow else "Sunny",
                                "probabilityOfPrecipitation": {"value": 90 if snow else 0}, "windSpeed": "10 mph"})
            return {"properties": {"periods": periods}}
        if "/alerts/active" in url:
            return {"features": [{"properties": alert}] if alert else []}
        return None

    return weather.NWS(offline=False, cache_path=tmp_path / "cache.json", http_get=http_get), calls


def in_transit_status():
    events = [DEPARTED, call("2026-11-14T21:00:00Z", "Springfield", "MO")]
    return tracking.estimate(load(), FACILITIES, events, datetime(2026, 11, 14, 21, 30, tzinfo=UTC))


def test_check_load_finds_snow_on_the_route(tmp_path):
    st = in_transit_status()
    nws, _ = fake_nws(tmp_path, snow_from=datetime(2026, 11, 14, 22, 0, tzinfo=UTC), snow_hours=3)
    w = weather.check_load(st, load(), FACILITIES, nws)
    assert w.delay.minutes > 0
    assert w.eta_with_weather == st.eta + timedelta(minutes=w.delay.minutes)
    assert w.notes and w.notes[0].startswith("Heavy snow") and "NWS hourly forecast" in w.notes[0]
    assert w.miles_covered == pytest.approx(w.miles_total)


def test_check_load_uses_alerts(tmp_path):
    st = in_transit_status()
    alert = {"id": "urn:a1", "event": "Winter Storm Warning", "headline": "Winter Storm Warning until Sun 6 AM",
             "onset": "2026-11-14T12:00:00Z", "ends": "2026-11-16T12:00:00Z"}
    nws, _ = fake_nws(tmp_path, snow_from=T0 + timedelta(days=30), snow_hours=0, alert=alert)
    w = weather.check_load(st, load(), FACILITIES, nws)
    assert [h.hazard.label for h in w.delay.hits] == ["Winter Storm Warning"]
    assert w.delay.hits[0].hazard.speed_factor == 0.65


def test_clear_skies_change_nothing(tmp_path):
    st = in_transit_status()
    nws, _ = fake_nws(tmp_path, snow_from=T0 + timedelta(days=30), snow_hours=0)
    w = weather.check_load(st, load(), FACILITIES, nws)
    assert w.delay.minutes == 0 and w.notes == [] and w.status_with_weather == st.status


def test_cache_is_reused_and_offline_mode_reads_it(tmp_path):
    st = in_transit_status()
    nws, calls = fake_nws(tmp_path, snow_from=datetime(2026, 11, 14, 22, 0, tzinfo=UTC), snow_hours=3)
    first = weather.check_load(st, load(), FACILITIES, nws)
    n = len(calls)
    weather.check_load(st, load(), FACILITIES, nws)
    assert len(calls) == n                                   # second run: all from cache
    nws.save()
    offline = weather.NWS(offline=True, cache_path=tmp_path / "cache.json",
                          http_get=lambda url: pytest.fail("offline mode made a request"))
    assert weather.check_load(st, load(), FACILITIES, offline).delay.minutes == first.delay.minutes


def test_offline_with_no_cache_degrades_quietly(tmp_path):
    nws = weather.NWS(offline=True, cache_path=tmp_path / "missing.json")
    w = weather.check_load(in_transit_status(), load(), FACILITIES, nws)
    assert w.delay.minutes == 0 and w.miles_covered == 0


def test_arrived_loads_are_skipped(tmp_path):
    st = tracking.estimate(load(status="AT_DELIVERY"), FACILITIES, [], datetime(2026, 11, 16, 13, 0, tzinfo=UTC))
    assert weather.check_load(st, load(status="AT_DELIVERY"), FACILITIES, weather.NWS(offline=True, cache_path=None)) is None
