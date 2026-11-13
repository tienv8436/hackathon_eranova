"""Traffic (TomTom) and the combined delay outlook, with canned responses (no network, no key needed)."""

from datetime import datetime, timedelta

import httpx
import pytest

from meridian import geo, hazards, outlook, traffic, weather
from meridian.geo import UTC
from tests.test_tracking import FACILITIES, load
from tests.test_weather import fake_nws, in_transit_status

CHI, DAL = geo.CITIES[("Chicago", "IL")], geo.CITIES[("Dallas", "TX")]
CRUISE = geo.haversine_mi(CHI, DAL)          # plan in straight-line miles, so a shift's plan = its crow distance
T0 = datetime(2026, 11, 14, 15, 0, tzinfo=UTC)
BEFORE = T0 - timedelta(hours=1)


def fake_tomtom(tmp_path, extra_min: float = 30, free_mph: float = 58, sections=None, key="test-key"):
    """TomTom stand-in: free-flow at `free_mph`; with traffic = the slower of (our 50 mph plan, free flow)
    + `extra_min`, never below free flow."""
    calls = []

    def http_get(url, params):
        calls.append((url, params))
        a, b = url.rsplit("/", 2)[-2].split(":")
        a, b = tuple(map(float, a.split(","))), tuple(map(float, b.split(",")))
        miles = geo.haversine_mi(a, b)
        free_s = miles / free_mph * 3600
        with_traffic_s = max(max(miles / 50 * 3600, free_s) + extra_min * 60, free_s)
        pts = [{"latitude": p[0], "longitude": p[1]} for p in (geo.interpolate(a, b, i / 10) for i in range(11))]
        return {"routes": [{
            "summary": {"lengthInMeters": miles * 1609.344, "travelTimeInSeconds": with_traffic_s,
                        "noTrafficTravelTimeInSeconds": free_s,
                        "historicTrafficTravelTimeInSeconds": with_traffic_s,
                        "liveTrafficIncidentsTravelTimeInSeconds": free_s,
                        "trafficDelayInSeconds": 0},
            "legs": [{"points": pts}], "sections": sections or []}]}

    return traffic.TomTom(key=key, offline=False, cache_path=tmp_path / "traffic.json", http_get=http_get), calls


# --------------------------------------------------------------------------- shifts

def test_plan_splits_into_shifts_at_10h_breaks():
    legs = hazards.route_legs(T0, CHI, DAL, 925, 5.5)          # 5.5 h | 10 h break | 11 h (+30 min) | 10 h | 2 h
    shifts = hazards.shifts(legs)
    assert [round(sum(leg.miles for leg in s)) for s in shifts] == [275, 550, 100]
    assert shifts[1][0].a == pytest.approx(shifts[0][-1].b)


# --------------------------------------------------------------------------- what counts as traffic delay

def test_only_traffic_beyond_our_plan_counts(tmp_path):
    legs = hazards.route_legs(T0, CHI, DAL, CRUISE, 11)
    tt, calls = fake_tomtom(tmp_path, extra_min=30)
    fixed, detail, covered = traffic.route_traffic(legs, tt, now=BEFORE)
    assert len(calls) == len(hazards.shifts(legs)) == 2
    assert len(fixed) == 2 and covered == pytest.approx(CRUISE, abs=0.5)
    for f, s in zip(fixed, detail):
        assert s.free_flow_minutes < s.plan_minutes < s.predicted_minutes
        assert f.minutes == pytest.approx(s.predicted_minutes - s.plan_minutes, abs=1)
    assert sum(f.minutes for f in fixed) == pytest.approx(60, abs=15)   # ~30 per shift (lat/lon vs great-circle)


def legs_for(miles: float, hos: float = 11):
    """A plan of `miles` straight-line miles from Chicago toward Dallas."""
    return hazards.route_legs(T0, CHI, geo.interpolate(CHI, DAL, miles / CRUISE), miles, hos)


def test_traffic_inside_the_plans_slack_adds_nothing(tmp_path):
    tt, _ = fake_tomtom(tmp_path, extra_min=-20)                # rush hour, but still faster than 50 mph
    fixed, detail, _ = traffic.route_traffic(legs_for(300), tt, now=BEFORE)
    assert fixed == [] and detail[0].extra_minutes == 0


def test_slow_free_flow_road_counts_only_the_traffic_part(tmp_path):
    legs = legs_for(300)
    tt, _ = fake_tomtom(tmp_path, extra_min=30, free_mph=45)    # free flow already slower than our plan
    fixed, detail, _ = traffic.route_traffic(legs, tt, now=BEFORE)
    s = detail[0]
    assert s.extra_minutes == pytest.approx(s.predicted_minutes - s.free_flow_minutes, abs=1)


def test_finished_shifts_are_skipped_and_the_current_one_starts_at_the_next_leg(tmp_path):
    legs = hazards.route_legs(T0, CHI, DAL, CRUISE, 11)
    tt, calls = fake_tomtom(tmp_path)
    shift1 = hazards.shifts(legs)[0]
    halfway = shift1[0].depart + (shift1[-1].arrive - shift1[0].depart) / 2
    _, detail, covered = traffic.route_traffic(legs, tt, now=halfway)
    next_leg = next(leg for leg in shift1 if leg.depart >= halfway)
    assert detail[0].depart == next_leg.depart
    assert detail[0].plan_miles == pytest.approx(sum(leg.miles for leg in shift1 if leg.depart >= halfway), abs=0.1)
    # a few minutes later the request is the same, so it comes from the cache
    n = len(calls)
    traffic.route_traffic(legs, tt, now=halfway + timedelta(minutes=5))
    assert len(calls) == n
    after_all = legs[-1].arrive + timedelta(hours=1)
    assert traffic.route_traffic(legs, tt, now=after_all)[1] == []


def test_notable_incidents_are_listed(tmp_path):
    sections = [{"sectionType": "TRAFFIC", "startPointIndex": 4, "endPointIndex": 6, "simpleCategory": "ROAD_CLOSURE",
                 "magnitudeOfDelay": 4, "delayInSeconds": 1500},
                {"sectionType": "TRAFFIC", "startPointIndex": 1, "endPointIndex": 2, "simpleCategory": "JAM",
                 "magnitudeOfDelay": 1, "delayInSeconds": 60}]                     # too small to list
    legs = legs_for(300)
    tt, _ = fake_tomtom(tmp_path, sections=sections)
    _, detail, _ = traffic.route_traffic(legs, tt, "America/Chicago", now=BEFORE)
    assert len(detail[0].incidents) == 1
    assert detail[0].incidents[0].startswith("Road closure (indefinite)") and "+25 min" in detail[0].incidents[0]


def test_traffic_delay_uses_up_driver_hours():
    legs = legs_for(500)                                         # 10 h: fits one shift
    hz = hazards.Hazard(key="t", kind="TRAFFIC", label="Traffic", points=[], source="TomTom", evidence="x")
    fd = hazards.FixedDelay(hazard=hz, minutes=120, miles=500, first_at=T0, last_at=T0, near="between A and B")
    d = hazards.route_delay(legs, [], hos_left=11, fixed=[fd])   # 12 h of driving: past 11 h + 30 min grace
    assert d.extra_breaks == 1 and d.minutes == 120 + 10 * 60


# --------------------------------------------------------------------------- client: cache, key handling

def test_cache_reuse_and_no_key_in_the_cache_file(tmp_path):
    legs = legs_for(300)
    tt, calls = fake_tomtom(tmp_path, key="SECRET-KEY-123")
    traffic.route_traffic(legs, tt, now=BEFORE)
    traffic.route_traffic(legs, tt, now=BEFORE)
    assert len(calls) == 1
    tt.save()
    assert "SECRET-KEY-123" not in (tmp_path / "traffic.json").read_text(encoding="utf-8")
    offline = traffic.TomTom(key="", offline=True, cache_path=tmp_path / "traffic.json",
                             http_get=lambda *a: pytest.fail("offline mode made a request"))
    assert offline.available and traffic.route_traffic(legs, offline, now=BEFORE)[1]


def test_http_errors_are_reported_without_the_key(tmp_path):
    tt = traffic.TomTom(key="SECRET-KEY-123", offline=False, cache_path=None)
    tt._http = httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(403, json={"detailedError": {"message": "Invalid API key"}})))
    assert tt.route(CHI, DAL, T0) is None
    assert tt.errors == ["TomTom HTTP 403: Invalid API key"]


def _route_json():
    return {"routes": [{"summary": {"lengthInMeters": 1000, "travelTimeInSeconds": 60,
                                    "noTrafficTravelTimeInSeconds": 60}, "legs": [], "sections": []}]}


def test_rate_limit_is_retried_after_waiting(tmp_path):
    replies = [httpx.Response(429, headers={"Retry-After": "2"}, json={"detailedError": {"message": "rate limit"}}),
               httpx.Response(429, json={"detailedError": {"message": "rate limit"}}),
               httpx.Response(200, json=_route_json())]
    tt = traffic.TomTom(key="k", offline=False, cache_path=None)
    tt._http = httpx.Client(transport=httpx.MockTransport(lambda req: replies.pop(0)))
    sleeps = []
    tt._sleep = sleeps.append
    assert tt.route(CHI, DAL, T0) is not None
    assert [s for s in sleeps if s >= 1] == [2.0, 2.0]          # Retry-After, then backoff 2^1
    assert tt.errors == [] and not tt.denied


def test_persistent_rate_limit_stops_further_calls(tmp_path):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(429, json={"detailedError": {"message": "You have exceeded the permitted rate limit"}})

    tt = traffic.TomTom(key="k", offline=False, cache_path=None)
    tt._http = httpx.Client(transport=httpx.MockTransport(handler))
    tt._sleep = lambda s: None
    assert tt.route(CHI, DAL, T0) is None
    assert len(calls) == traffic.RETRIES_429 + 1
    assert tt.denied and "rate limit" in tt.stop_reason
    assert tt.route(DAL, CHI, T0) is None and len(calls) == traffic.RETRIES_429 + 1   # no more calls this run


def test_requests_are_spaced_out():
    tt = traffic.TomTom(key="k", offline=False, cache_path=None)
    sleeps = []
    tt._sleep = sleeps.append
    for _ in range(3):
        tt._throttle()
    assert len(sleeps) == 2 and all(s > 0.3 for s in sleeps)    # 2 QPS: ~0.5 s apart


def test_no_key_means_no_traffic():
    assert not traffic.TomTom(key="", offline=False, cache_path=None).available


# --------------------------------------------------------------------------- weather + traffic together

def test_outlook_combines_weather_and_traffic(tmp_path):
    st = in_transit_status()
    nws, _ = fake_nws(tmp_path, snow_from=datetime(2026, 11, 14, 22, 0, tzinfo=UTC), snow_hours=3)
    tt, _ = fake_tomtom(tmp_path, extra_min=20)
    o = outlook.outlook(st, load(), FACILITIES, nws, tt, now=st.as_of)
    assert o.weather_minutes > 0 and o.traffic_minutes > 0
    assert o.delay.minutes >= o.weather_minutes + o.traffic_minutes - 2       # rounding of the shares
    assert o.predicted_eta == st.eta + timedelta(minutes=o.delay.minutes)
    assert any(n.startswith(("Traffic between", "Traffic around")) and "TomTom" in n for n in o.notes)
    assert any(n.startswith("Heavy snow") for n in o.notes)


def _outlook_for(st, delay_minutes: int, note: str = "Heavy snow near Joplin, MO, Sat 18:00–20:00: assumed 40% slower"):
    eta, late, late_mabd, status = hazards.adjusted_status(st, delay_minutes)
    return outlook.DelayOutlook(shipment_id=st.shipment_id, checked_at=st.as_of, eta=st.eta, predicted_eta=eta,
                                minutes_late=late, minutes_late_mabd=late_mabd, status=st.status,
                                predicted_status=status, delay=hazards.RouteDelay(minutes=delay_minutes),
                                weather_minutes=delay_minutes, notes=[note])


def test_apply_without_a_delay_changes_nothing():
    st = in_transit_status()
    assert outlook.apply(st, None) is st
    assert outlook.apply(st, _outlook_for(st, 0)) is st


def test_apply_moves_eta_and_explains_it():
    st = in_transit_status()
    assert st.status == "ON_TIME"
    applied = outlook.apply(st, _outlook_for(st, 45))
    assert applied.eta == st.eta + timedelta(minutes=45)
    assert applied.minutes_late == st.minutes_late + 45 and applied.status == "ON_TIME"
    assert applied.exceptions == st.exceptions                    # status not worse: no new exception
    assert applied.assumptions[:len(st.assumptions)] == st.assumptions
    assert "weather & traffic forecast adds 45 min" in applied.assumptions[len(st.assumptions)]


def test_forecast_that_makes_a_load_at_risk_reaches_the_load_watch():
    from datetime import date
    from meridian import risk_rules
    st = in_transit_status()
    o = _outlook_for(st, -st.minutes_late - 60)                  # lands 1 h before the window closes
    applied = outlook.apply(st, o)
    assert applied.status == "AT_RISK"
    assert applied.exceptions[0].startswith("Weather & traffic forecast adds") and "Heavy snow" in applied.exceptions[0]
    assert risk_rules.late_eta(risk_rules.RiskContext(today=date(2026, 11, 14), tracking={"1": st})) == []
    items = risk_rules.late_eta(risk_rules.RiskContext(today=date(2026, 11, 14), tracking={"1": applied}))
    assert len(items) == 1 and items[0].title.startswith("Weather & traffic forecast adds")
    assert any("Heavy snow" in a for a in items[0].evidence["eta_assumptions"])


def test_at_risk_title_when_just_past_the_window():
    from datetime import date
    from meridian import risk_rules
    st = in_transit_status()
    applied = outlook.apply(st, _outlook_for(st, -st.minutes_late + 10)).model_copy(update={"exceptions": []})
    assert applied.status == "AT_RISK" and applied.minutes_late == 10
    item = risk_rules.late_eta(risk_rules.RiskContext(today=date(2026, 11, 14), tracking={"1": applied}))[0]
    assert item.title == "Projected 10 min past the window end (within the 30-min tolerance)"


def test_forecast_that_makes_a_load_late_is_high():
    from datetime import date
    from meridian import risk_rules
    st = in_transit_status()
    applied = outlook.apply(st, _outlook_for(st, -st.minutes_late + 4 * 60))   # 4 h past the window
    assert applied.status == "LATE"
    item = risk_rules.late_eta(risk_rules.RiskContext(today=date(2026, 11, 14), tracking={"1": applied}))[0]
    assert item.severity == "HIGH" and item.title.startswith("Projected 4.0 h past")


def test_outlook_without_a_tomtom_key_still_gives_weather(tmp_path):
    st = in_transit_status()
    nws, _ = fake_nws(tmp_path, snow_from=datetime(2026, 11, 14, 22, 0, tzinfo=UTC), snow_hours=3)
    o = outlook.outlook(st, load(), FACILITIES, nws, traffic.TomTom(key="", offline=False, cache_path=None))
    assert o.weather_minutes > 0 and o.traffic_minutes == 0
    assert o.errors == ["traffic not checked: set TOMTOM_API_KEY in .env"]
