"""Delay cost calculator, checked against the bundled workbook. Several cases are ported from
Allison Wong's ui/test_synthetic_costs.py (feat/tracking-cost-estimates, a7f8042)."""

from copy import deepcopy
from hashlib import sha256

import pytest

from meridian import board, delay_cost
from meridian.delay_cost import SOURCE_SHA256, WORKBOOK, dollars, estimate, estimate_all, load_model


def test_workbook_provenance_and_every_rollup_matches_to_the_cent():
    assert sha256(WORKBOOK.read_bytes()).hexdigest() == SOURCE_SHA256
    model = load_model()
    assert (len(model["Events"]), len(model["Cost_Lines"]), len(model["Recommendations"])) == (123, 302, 123)
    for sid in {e["shipment_id"] for e in model["Events"]}:
        c = estimate(sid, model=model)       # raises if any total disagrees with Shipment_Rollup
        rollup = next(r for r in model["Shipment_Rollup"] if r["shipment_id"] == sid)
        assert dollars(c.gross_usd) == dollars(rollup["gross_complication_cost"])
        assert dollars(c.gp_after_usd) == dollars(rollup["gp_after_complications"])


@pytest.mark.parametrize("sid", sorted({e["shipment_id"] for e in load_model()["Events"]}))
def test_who_pays_adds_up_to_gross(sid):
    c = estimate(sid)
    assert dollars(c.customer_usd + c.carrier_usd + c.broker_margin_usd) == dollars(c.gross_usd)


def test_weather_then_missed_appointment_counts_each_line_once():
    c = estimate("7300117")
    assert [e.event_id for e in c.events] == ["DLY-00108", "DLY-00109"]
    assert c.events[1].parent_event_id == "DLY-00108"
    assert len(c.lines) == 5
    assert c.gross_usd == 630.64            # 250 + .57×42 + 250 + 50 + 1.35×42
    assert (c.customer_usd, c.carrier_usd, c.broker_margin_usd) == (0.0, 0.0, 630.64)
    assert (c.gp_before_usd, c.gp_after_usd) == (469.0, -161.64)
    assert c.off_ledger_usd == 80.64        # the two OPS labor lines
    assert len(c.mitigations) == 2 and c.simulated


def test_customer_and_carrier_recoveries_go_to_the_right_payer():
    c = estimate("7300125")                 # detention at consignee, rebilled to the customer
    assert (c.customer_usd, c.carrier_usd, c.broker_margin_usd) == (150.0, 0.0, 192.0)
    c = estimate("7300130")                 # carrier-caused layover disputed back to the carrier
    assert (c.customer_usd, c.carrier_usd, c.broker_margin_usd) == (0.0, 250.0, 410.1)
    assert {ln.payer for ln in c.lines if ln.sign == -1} == {"CARRIER"}


def test_lines_already_in_tms_are_not_taken_off_gp_twice():
    c = estimate("7300012")                 # recorded: 225 cost − 235 recovery = −10
    assert c.already_in_tms_usd == -10.0
    assert (c.gross_usd, c.recoveries_usd) == (267.0, 235.0)
    assert c.additional_usd == 42.0         # 32 net − (−10)
    assert c.gp_after_usd == 192.65         # 234.65 − 42
    assert any("already on the TMS ledger" in b for b in c.basis)


def test_no_event_means_unavailable_not_zero():
    assert estimate("7300175") is None
    assert estimate("UNKNOWN") is None


def test_unknown_amount_stays_unknown():
    model = deepcopy(load_model())
    line = next(ln for ln in model["Cost_Lines"] if ln["shipment_id"] == "7300125" and ln["sign"] == "1")
    line.update(quantity=None, amount=None, net_cost=None)
    c = estimate("7300125", model=model)
    assert c.gross_usd is None and c.broker_margin_usd is None and c.gp_after_usd is None
    assert c.customer_usd == 150.0          # the recovery itself is still known


def test_duplicates_and_bad_links_are_caught():
    model = deepcopy(load_model())
    line = next(ln for ln in model["Cost_Lines"] if ln["shipment_id"] == "7300125")
    model["Cost_Lines"].append(deepcopy(line))
    assert estimate("7300125", model=model).broker_margin_usd == 192.0     # exact duplicate counts once
    model["Cost_Lines"][-1]["rate"] = "99"
    with pytest.raises(ValueError, match="Conflicting"):
        estimate("7300125", model=model)

    model = deepcopy(load_model())
    next(e for e in model["Events"] if e["event_id"] == "DLY-00109")["parent_event_id"] = "DLY-00109"
    with pytest.raises(ValueError, match="Cyclic|parent"):
        estimate("7300117", model=model)


def test_gp_mismatch_with_tms_is_reported():
    c = estimate("7300117", {"gp_amount": 500})
    assert any("TMS GP is $500.00" in b for b in c.basis)
    assert not any("TMS GP" in b for b in estimate("7300117", {"gp_amount": 469}).basis)


def test_estimate_all_skips_a_bad_load_and_reports_it():
    model = deepcopy(load_model())
    next(ln for ln in model["Cost_Lines"] if ln["shipment_id"] == "7300125")["rate"] = "99"   # ≠ cached amount
    costs, errors = estimate_all({"7300117": {}, "7300125": {}, "7300175": {}}, model)
    assert set(costs) == {"7300117"}
    assert len(errors) == 1 and "7300125" in errors[0]


def test_estimate_all_survives_a_changed_workbook(monkeypatch):
    def changed():
        raise ValueError("differs from the documented source")
    monkeypatch.setattr(delay_cost, "load_model", changed)
    costs, errors = estimate_all({"7300117": {}})
    assert costs == {} and "unavailable" in errors[0]


# --------------------------------------------------------------------------- projections

from datetime import datetime, timezone  # noqa: E402

from meridian.delay_cost import cause_of, layover_nights, project  # noqa: E402
from meridian.models import PositionReport  # noqa: E402

UTC = timezone.utc
DOCK = {"timezone": "America/Chicago", "avg_unload_minutes": 60, "appointment_required": True,
        "receiving_hours": {d: {"open": "06:00", "close": "14:00"} for d in ("mon", "tue", "wed", "thu", "fri")}}


@pytest.fixture(scope="module")
def tr():
    from meridian.weather import _tracker_from_snapshots
    return _tracker_from_snapshots()


def _late(tr, sid, **kw):
    return tr.statuses[sid].model_copy(update=kw)


def test_layover_waits_for_the_next_receiving_day():
    fri_after_close = datetime(2026, 11, 13, 21, 0, tzinfo=UTC)          # Fri 15:00 CST
    nights, slot = layover_nights(DOCK, fri_after_close)
    assert nights == 3 and slot == datetime(2026, 11, 16, 12, 0, tzinfo=UTC)   # Mon 06:00 CST
    assert layover_nights(DOCK, datetime(2026, 11, 12, 16, 0, tzinfo=UTC))[0] == 0   # Thu 10:00: worked in
    assert layover_nights(DOCK, datetime(2026, 11, 12, 19, 30, tzinfo=UTC))[0] == 1  # Thu 13:30: unload runs past close


def test_cause_from_reason_code_tracking_or_unknown(tr):
    s = tr.statuses["7300119"]
    pos = s.position or PositionReport(at=s.as_of, city="X", state="TX", source="check_call")
    assert cause_of(s.model_copy(update={"position": pos.model_copy(update={"reason_code": "AI"})}))[0] == "CARRIER"
    assert cause_of(s.model_copy(update={"position": pos.model_copy(update={"reason_code": "B1"})}))[0] == "CONSIGNEE"
    assert cause_of(s.model_copy(update={"position": pos.model_copy(update={"reason_code": "AO"})}))[0] == "EXTERNAL"
    assert cause_of(s.model_copy(update={"eta_gap_minutes": 500}))[0] == "CARRIER"
    assert cause_of(s.model_copy(update={"eta_gap_minutes": 0, "position": pos}))[0] is None

    class Flip:   # on time without the forecast, late with it
        status, predicted_status = "AT_RISK", "LATE"
    assert cause_of(s.model_copy(update={"eta_gap_minutes": 0}), Flip())[0] == "EXTERNAL"


def test_projected_missed_appointment_carrier_pays_the_weekend_layover(tr):
    c = project("7300119", tr.shipments["7300119"], tr.statuses["7300119"], tr.facilities)
    assert c.kind == "projected" and c.simulated
    assert c.events[0].cause_owner == "CARRIER" and "3 nights" in c.events[0].detail
    # $50 re-appointment + 3 × $250 layover + 1.36 h × $42 labor; the layover is charged back to the carrier
    assert (c.gross_usd, c.carrier_usd, c.customer_usd, c.broker_margin_usd) == (857.12, 750.0, 0.0, 107.12)
    assert c.gp_after_usd == round(tr.shipments["7300119"]["gp_amount"] - 107.12, 2)


def test_unknown_cause_keeps_the_layover_with_us(tr):
    s = _late(tr, "7300119", eta_gap_minutes=0)
    c = project("7300119", tr.shipments["7300119"], s, tr.facilities)
    assert c.carrier_usd == 0.0 and c.broker_margin_usd == 857.12
    assert any("no one to bill" in b for b in c.basis)


def test_receiver_closed_bills_the_layover_to_the_customer(tr):
    s = tr.statuses["7300119"]
    s = s.model_copy(update={"position": s.position.model_copy(update={"reason_code": "B1"})})
    c = project("7300119", tr.shipments["7300119"], s, tr.facilities)
    assert (c.customer_usd, c.carrier_usd, c.broker_margin_usd) == (750.0, 0.0, 107.12)


def test_missed_mabd_adds_the_otif_chargeback(tr):
    ship = dict(tr.shipments["7300119"], declared_value=50000)       # delivers to BBR-DC135 (MABD program)
    s = _late(tr, "7300119", minutes_late_mabd=120)
    c = project("7300119", ship, s, tr.facilities)
    otif = next(ln for ln in c.lines if ln.charge_code == "OTIF")
    assert otif.amount_usd == 1500.0                                   # 3% of $50,000
    assert dollars(c.customer_usd + c.carrier_usd + c.broker_margin_usd) == dollars(c.gross_usd)
    no_value = {k: v for k, v in ship.items() if k not in ("declared_value", "freight_totals")}
    c = project("7300119", no_value, s, tr.facilities)
    assert next(ln for ln in c.lines if ln.charge_code == "OTIF").amount_usd is None and c.gross_usd is None


@pytest.mark.parametrize("status", ["NOT_REQUIRED", "NEEDED"])
def test_no_reappointment_fee_without_a_booked_appointment(tr, status):
    """Every TMS stop has an `appointment` dict; only a booked one can be missed."""
    ship = deepcopy(tr.shipments["7300119"])
    next(s for s in ship["stops"] if s["stop_type"] == "DELIVERY")["appointment"] = {"status": status}
    c = project("7300119", ship, tr.statuses["7300119"], tr.facilities)
    assert "APPT" not in {ln.charge_code for ln in c.lines}
    assert c.events[0].label == "Late delivery (projected)"
    assert c.gross_usd == 807.12                       # 3 × $250 layover + 1.36 h × $42, no $50 fee


def test_modeled_load_also_gets_what_tracking_adds(tr):
    """7300117: the model has a weather delay and a missed appointment, but tracking also has it missing
    the MABD at BBR-DC135 (OTIF program); the chargeback is added, the appointment isn't counted twice."""
    costs, errors = estimate_all(tr.shipments, statuses=tr.statuses, facilities=tr.facilities)
    c, modeled = costs["7300117"], estimate("7300117", tr.shipments["7300117"])
    assert not errors and c.kind == "modeled"
    assert [e.event_id for e in c.events] == ["DLY-00108", "DLY-00109", "PROJ-7300117-OTIF"]
    otif = sum(ln.amount_usd for ln in c.lines if ln.event_id == "PROJ-7300117-OTIF")
    assert c.gross_usd == round(modeled.gross_usd + otif, 2)
    assert dollars(c.customer_usd + c.carrier_usd + c.broker_margin_usd) == dollars(c.gross_usd)
    assert c.gp_after_usd == round(modeled.gp_before_usd - c.additional_usd, 2)
    assert not any(b.startswith("Priced like the model's missed") for b in c.basis)   # that part was left out
    assert any("OTIF chargeback = 3% of PO value" in b for b in c.basis)


def test_no_projection_for_loads_that_make_it(tr):
    on_time = next(s for s in tr.statuses.values() if s.status == "ON_TIME")
    assert project(on_time.shipment_id, tr.shipments[on_time.shipment_id], on_time, tr.facilities) is None
    assert project("7300119", tr.shipments["7300119"], None, tr.facilities) is None


def test_estimate_all_prefers_the_model_then_projects(tr):
    costs, errors = estimate_all(tr.shipments, statuses=tr.statuses, facilities=tr.facilities)
    assert not errors
    assert costs["7300117"].kind == "modeled"            # late too, but the model has its events
    assert costs["7300127"].kind == "projected"
    assert "7300166" not in costs                        # on time, nothing modeled
    assert set(estimate_all(tr.shipments)[0]) == {k for k, c in costs.items() if c.kind == "modeled"}


def test_board_carries_the_cost_on_each_load():
    from meridian.weather import _tracker_from_snapshots
    tr = _tracker_from_snapshots()
    costs, errors = estimate_all(tr.shipments)
    assert not errors and "7300117" in costs
    views = {v.record_id: v for v in board.build(tr.shipments, tr.statuses, [], delay_costs=costs)}
    assert views["7300117"].delay_cost.broker_margin_usd == 630.64
    assert views["7300166"].delay_cost is None              # tracked, nothing modeled: unavailable
    assert all(v.delay_cost is None for v in board.build(tr.shipments, tr.statuses, []))
