import json
import pathlib
from datetime import datetime, timedelta

from meridian import board, risk_rules, tracking
from meridian.geo import UTC
from meridian.hazards import RouteDelay
from meridian.models import PositionReport, RiskItem, TrackingStatus
from meridian.outlook import DelayOutlook
from meridian.risk_rules import RiskContext

SNAP = pathlib.Path(__file__).resolve().parent.parent / "snapshots"
AS_OF = datetime(2026, 11, 13, 13, 0, tzinfo=UTC)
SHIP = {"shipment_id": "7300001", "status": "IN_TRANSIT", "carrier_id": "ACME", "customer_id": "NORTHSTAR",
        "shipper_city": "Chicago", "shipper_state": "IL", "consignee_city": "Dallas", "consignee_state": "TX"}


def status(stage="IN_TRANSIT", st="ON_TIME", hours_ago=1.0, late=-600, late_mabd=None, eta_in_h=10.0,
           driver_eta_in_h=None, gap=None, reason=None, exceptions=()):
    pos = PositionReport(at=AS_OF - timedelta(hours=hours_ago), city="Springfield", state="MO", lat=37.2, lon=-93.3,
                         reason_code=reason)
    return TrackingStatus(
        shipment_id="7300001", as_of=AS_OF, stage=stage, status=st, position=pos, hours_since_report=hours_ago,
        eta=AS_OF + timedelta(hours=eta_in_h),
        driver_eta=AS_OF + timedelta(hours=driver_eta_in_h) if driver_eta_in_h is not None else None,
        eta_gap_minutes=gap, minutes_late=late, minutes_late_mabd=late_mabd, delivery_tz="America/Chicago",
        exceptions=list(exceptions))


def risk(kind, severity="MEDIUM", score=200.0, rid="7300001", record_type="shipment", **kw):
    return RiskItem(kind=kind, severity=severity, score=score, record_type=record_type, record_id=rid,
                    title=kw.pop("title", kind.replace("_", " ").capitalize()), evidence=kw.pop("evidence", {}),
                    suggested_action=kw.pop("suggested_action", f"Fix {kind.lower()}"), **kw)


def keys(v):
    return [a.key for a in v.actions]


def codes(v):
    return [s.code for s in v.signals]


# --------------------------------------------------------------------------- tracking signals → actions

def test_dark_late_truck_calls_carrier_first_then_receiver_then_customer():
    v = board.view("7300001", shipment=SHIP,
                   status=status(st="LATE", hours_ago=36, late=1200, late_mabd=60, eta_in_h=-17))
    assert codes(v)[:2] == ["DARK", "ETA_PASSED"]
    assert {"PROJECTED_LATE", "MISSES_MABD"} <= set(codes(v))
    assert keys(v) == ["call_carrier", "rebook_appointment", "notify_customer"]
    assert v.headline.startswith("No contact for 36 h (last Springfield, MO) — projected 20.0 h past the window")
    assert "misses the MABD" in v.headline
    assert "once the carrier confirms" in v.actions[2].label
    assert (v.urgency, v.phase) == ("ACT_NOW", "IN_MOTION")


def test_one_carrier_call_answers_every_carrier_signal():
    v = board.view("7300001", shipment=SHIP, status=status(st="AT_RISK", hours_ago=8, eta_in_h=-1,
                                                           driver_eta_in_h=-0.5, gap=420, late=-200))
    assert keys(v) == ["call_carrier"]
    assert len(v.actions[0].why) == 4    # dark, driver ETA unrealistic, our ETA passed, driver ETA passed


def test_unrealistic_driver_eta_alone_asks_for_a_realistic_eta():
    v = board.view("7300001", shipment=SHIP, status=status(st="AT_RISK", driver_eta_in_h=2, gap=480, late=-300))
    assert codes(v) == ["DRIVER_ETA_UNREALISTIC"]
    assert "legal hours" in v.actions[0].label
    assert v.urgency == "ACT_NOW"


def test_breakdown_reported_asks_for_repair_eta():
    v = board.view("7300001", shipment=SHIP, status=status(st="AT_RISK", reason="AI", late=-200))
    assert "REPORTED_DELAY" in codes(v)
    assert "mechanical breakdown" in v.headline
    assert "repair" in v.actions[0].label


def test_arrived_late_goes_to_receiver_and_customer_not_carrier():
    v = board.view("7300001", shipment={**SHIP, "status": "AT_DELIVERY"},
                   status=status(stage="AT_DELIVERY", st="LATE", late=150))
    assert codes(v) == ["ARRIVED_LATE"]
    assert keys(v) == ["rebook_appointment", "notify_customer"]
    assert v.headline == "Arrived 2.5 h after the delivery window closed."


def test_on_time_load_is_fine_with_nothing_to_do():
    v = board.view("7300001", shipment=SHIP, status=status())
    assert (v.urgency, v.signals, v.actions) == ("FINE", [], [])
    assert v.headline == "On time — 10.0 h to spare."


def test_tight_slack_is_watch_with_a_light_action():
    v = board.view("7300001", shipment=SHIP, status=status(st="AT_RISK", late=-45))
    assert codes(v) == ["TIGHT"] and v.urgency == "WATCH"
    assert keys(v) == ["confirm_eta"]


def test_at_shipper_wait_calls_the_shipper():
    v = board.view("7300001", shipment={**SHIP, "status": "AT_PICKUP"},
                   status=status(stage="AT_PICKUP", st="AT_RISK", exceptions=["At the shipper for 3.0 h — still not loaded"]))
    assert keys(v)[0] == "call_shipper" and v.phase == "PRE_PICKUP"


def test_weather_slowdown_on_an_on_time_load_stays_fine():
    o = DelayOutlook(shipment_id="7300001", checked_at=AS_OF, delay=RouteDelay(minutes=25), weather_minutes=25)
    v = board.view("7300001", shipment=SHIP, status=status(), outlook=o)
    assert codes(v) == ["WEATHER_TRAFFIC"] and v.urgency == "FINE" and v.actions == []
    assert v.headline == "On time — 10.0 h to spare; weather adds 25 min on the route."


def test_weather_slowdown_on_a_tight_load_is_watch():
    o = DelayOutlook(shipment_id="7300001", checked_at=AS_OF, delay=RouteDelay(minutes=40), weather_minutes=20,
                     traffic_minutes=20)
    v = board.view("7300001", shipment=SHIP, status=status(st="AT_RISK", late=-30), outlook=o)
    assert v.urgency == "WATCH"
    assert "Weather and traffic add 40 min on the route" in [s.text for s in v.signals]


# --------------------------------------------------------------------------- risk items

def test_mabd_rule_folds_into_the_appointment_call_on_a_late_load():
    items = [risk("MABD_RISK", "HIGH", 380, title="Must-arrive-by date has passed")]
    v = board.view("7300001", shipment=SHIP, risks=items,
                   status=status(st="LATE", late=300, late_mabd=60, eta_in_h=5))
    assert keys(v) == ["rebook_appointment", "notify_customer"]
    assert "Must-arrive-by date has passed" in v.actions[0].why


def test_late_eta_item_is_absorbed_and_its_draft_reused():
    late = risk("LATE_ETA", "HIGH", 390, draft_message="Please confirm location.", draft_audience="carrier")
    v = board.view("7300001", shipment=SHIP, risks=[late],
                   status=status(st="LATE", hours_ago=12, late=300, eta_in_h=-2))
    assert "RISK" not in codes(v)
    assert "risk:LATE_ETA" not in keys(v)
    assert v.actions[0].key == "call_carrier" and v.actions[0].draft == "Please confirm location."


def test_overdue_check_call_leads_the_headline_and_the_actions():
    items = [risk("CARRIER_COMPLIANCE", "MEDIUM", 250, title="Cargo insurance expires soon"),
             risk("TRACKING_OVERDUE", "MEDIUM", 220, title="Check call overdue")]
    v = board.view("7300001", shipment={**SHIP, "status": "AT_PICKUP"}, risks=items)
    assert v.headline == "Check call overdue."
    assert keys(v) == ["call_carrier", "risk:CARRIER_COMPLIANCE"]


def test_overdue_on_a_delivered_load_is_close_out_paperwork():
    item = risk("TRACKING_OVERDUE", "HIGH", 390, rid="7300123", evidence={"status": "DELIVERED"})
    v = board.view("7300123", risks=[item])
    assert (v.stage, v.phase, v.urgency) == ("DELIVERED", "ADMIN", "WATCH")
    assert keys(v) == ["close_out"]


def test_high_risk_item_is_act_now_and_medium_is_watch():
    assert board.view("1", shipment={"status": "BOOKED"}, risks=[risk("UNCOVERED_SOON", "HIGH", rid="1")]).urgency == "ACT_NOW"
    assert board.view("1", shipment={"status": "COVERED"}, risks=[risk("MISSING_RATE_CON", rid="1")]).urgency == "WATCH"


def test_quotes_and_margin_only_loads_are_admin():
    q = board.view("Q1", record_type="quote", risks=[risk("QUOTE_EXPIRING", "LOW", rid="Q1", record_type="quote")])
    m = board.view("7300001", shipment={**SHIP, "status": "COVERED"}, risks=[risk("LOW_MARGIN")])
    assert q.phase == m.phase == "ADMIN"


# --------------------------------------------------------------------------- build

def test_build_groups_by_load_and_ranks_by_urgency_then_consequence():
    ships = {"A": {**SHIP, "shipment_id": "A"}, "B": {**SHIP, "shipment_id": "B"}, "C": {**SHIP, "shipment_id": "C"}}
    statuses = {"A": status(), "B": status(st="LATE", hours_ago=10, late=600, eta_in_h=-1),
                "C": status(st="LATE", hours_ago=10, late=60, eta_in_h=-1)}
    risks = [risk("CARRIER_COMPLIANCE", rid="A"), risk("LOW_MARGIN", rid="A"),
             risk("QUOTE_EXPIRING", "LOW", 100, rid="Q9", record_type="quote")]
    views = board.build(ships, statuses, risks)
    assert [v.record_id for v in views][:2] == ["B", "C"]       # later = first
    assert len([v for v in views if v.record_id == "A"]) == 1
    assert len(next(v for v in views if v.record_id == "A").risks) == 2
    assert {v.record_id for v in views} == {"A", "B", "C", "Q9"}


def test_snapshot_board_every_act_now_load_has_a_next_action():
    read = lambda n: json.loads((SNAP / f"{n}.json").read_text(encoding="utf-8"))  # noqa: E731
    ships = {str(s["shipment_id"]): s for s in read("open_shipments")}
    facs = {f["facility_id"]: f for f in read("facilities")}
    tr = tracking.Tracker(list(ships.values()), facs, read("shipment_events"))
    ctx = RiskContext(today=tr.as_of.date(), shipments=list(ships.values()), tracking_queue=read("tracking_queue"),
                      vendors={v["vendor_id"]: v for v in read("vendors")}, facilities=facs,
                      customers={c["customer_id"]: c for c in read("customers")},
                      quotes=read("quotes_quoted") + read("quotes_sent"), tracking=tr.statuses)
    views = board.build(ships, tr.statuses, risk_rules.run_all(ctx))
    ids = [v.record_id for v in views]
    assert len(ids) == len(set(ids))
    assert set(tr.statuses) <= set(ids)
    assert all(v.next_action for v in views if v.urgency == "ACT_NOW")
    order = [board.URGENCY_BASE[v.urgency] for v in views]
    assert order == sorted(order, reverse=True)
