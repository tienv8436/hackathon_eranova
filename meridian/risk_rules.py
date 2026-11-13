"""Risk detectors: pure functions, easy to unit test. Owner: tools & rules lead.

Each rule takes a RiskContext and returns list[RiskItem], with `evidence` holding the exact facts
it used (so the LLM explanation and the UI evidence table can only cite those).

Field names follow API_DOCS.md (Shipment, Stop, Carrier, Facility and Quote objects). Still to
confirm against a snapshot of the reset data: that GET /shipments rows carry `stops[]`,
`vendor_costs[]` and `customer_charges[]` (the docs say rows are full Shipment objects minus `summary`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .geo import fmt_local
from .models import RiskItem, Severity, TrackingStatus
from .tracking import LATE_TOLERANCE_MIN

DEFAULT_MARGIN_FLOOR = 18.0   # API default when a customer has no margin_floor_pct (same as pricing.py)
PRE_PICKUP = {"BOOKED", "COVERED", "DISPATCHED", "AT_PICKUP"}
AFTER_PICKUP = {"PICKED_UP", "IN_TRANSIT", "AT_DELIVERY"}


@dataclass
class RiskContext:
    today: date
    shipments: list[dict] = field(default_factory=list)         # open shipments (all pages)
    tracking_queue: list[dict] = field(default_factory=list)    # rows carry `overdue`
    vendors: dict[str, dict] = field(default_factory=dict)      # by vendor_id
    facilities: dict[str, dict] = field(default_factory=dict)   # by facility_id
    customers: dict[str, dict] = field(default_factory=dict)    # by customer_id
    quotes: list[dict] = field(default_factory=list)            # QUOTED + SENT
    tracking: dict[str, TrackingStatus] = field(default_factory=dict)   # by shipment_id (meridian.tracking)


# --------------------------------------------------------------------------- helpers

SEVERITY_BASE = {"HIGH": 300.0, "MEDIUM": 200.0, "LOW": 100.0}


def score(severity: Severity, days_until: int | None = None, money_at_risk: float = 0.0) -> float:
    """Severity first, then sooner is more urgent, then money. Higher = more urgent."""
    s = SEVERITY_BASE[severity]
    if days_until is not None:
        s += max(0, 30 - max(days_until, 0)) * 3     # up to +90 for today/past
    s += min(money_at_risk / 1000, 9.0)              # tie-breaker, up to +9
    return round(s, 2)


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value[:10])


def days_until(value: str | None, today: date) -> int | None:
    d = parse_date(value)
    return (d - today).days if d else None


def _shipment_item(s: dict, **kw) -> RiskItem:
    return RiskItem(record_type="shipment", record_id=str(s["shipment_id"]), **kw)


def _lane(s: dict) -> str:
    return f"{s.get('shipper_city')},{s.get('shipper_state')} → {s.get('consignee_city')},{s.get('consignee_state')}"


def _effective_status(s: dict) -> str | None:
    """For ON_HOLD loads, the status they will return to."""
    return s.get("held_from") if s.get("status") == "ON_HOLD" else s.get("status")


def _next_milestone_date(s: dict) -> str | None:
    """Pickup date until the freight is picked up, then the delivery date."""
    return s.get("pickup_date") if _effective_status(s) in PRE_PICKUP else s.get("delivery_date_target")


def _stop_date(stop: dict) -> str | None:
    window = stop.get("scheduled_window") or {}
    return window.get("earliest_local") or window.get("latest_local")


def _stop_is_done(s: dict, stop: dict) -> bool:
    """A pickup stop no longer needs attention once the freight is picked up."""
    return stop.get("stop_type") == "PICKUP" and _effective_status(s) in AFTER_PICKUP


def _appt_status(stop: dict) -> str | None:
    return (stop.get("appointment") or {}).get("status")


def _frt_margin_pct(s: dict) -> float | None:
    """Margin on the linehaul (FRT) alone, ignoring cancelled cost lines."""
    buy = sum(l.get("amount") or 0 for l in s.get("vendor_costs") or []
              if l.get("charge_code") == "FRT" and l.get("status") != "CANCELLED")
    sell = sum(l.get("amount") or 0 for l in s.get("customer_charges") or [] if l.get("charge_code") == "FRT")
    if not sell:
        return None
    return round((sell - buy) / sell * 100, 1)


# --------------------------------------------------------------------------- rules

def uncovered_soon(ctx: RiskContext) -> list[RiskItem]:
    """BOOKED (no carrier) and pickup within 2 days -> HIGH. (Worked example; copy this pattern.)"""
    items = []
    for s in ctx.shipments:
        if s.get("status") != "BOOKED":
            continue
        d = days_until(s.get("pickup_date"), ctx.today)
        if d is None or d > 2:
            continue
        items.append(_shipment_item(
            s,
            kind="UNCOVERED_SOON",
            severity="HIGH",
            score=score("HIGH", d, s.get("sell_total") or 0),
            title="No carrier, pickup in ≤2 days" if d >= 0 else "No carrier, pickup date passed",
            evidence={"status": s["status"], "pickup_date": s.get("pickup_date"), "days_until_pickup": d,
                      "customer_id": s.get("customer_id"), "lane": _lane(s)},
            suggested_action="Find a carrier",
        ))
    return items


def carrier_compliance(ctx: RiskContext) -> list[RiskItem]:
    """Carrier not ACTIVE / DO_NOT_USE / authority revoked / no unexpired CARGO policy, or the cargo
    policy expires before delivery -> HIGH; cargo expiring within 30 days (YELLOW) -> MEDIUM.

    Cargo expiry is judged from `expiration_date` against ctx.today (so snapshots score the same on any
    day); a policy with no date counts as expired, as the API's compliance light does.
    """
    items = []
    for s in ctx.shipments:
        carrier_id = s.get("carrier_id")
        vendor = ctx.vendors.get(carrier_id) if carrier_id else None
        if not vendor:
            continue
        cargo = [p for p in vendor.get("insurance") or [] if p.get("policy_type") == "CARGO"]
        expiries = [parse_date(p.get("expiration_date")) for p in cargo]
        latest_expiry = max((d for d in expiries if d), default=None)
        delivery = parse_date(s.get("delivery_date_target"))

        problems: list[str] = []
        if vendor.get("status") != "ACTIVE":
            problems.append(f"carrier status is {vendor.get('status')}"
                            + (f" ({vendor['status_reason']})" if vendor.get("status_reason") else ""))
        if vendor.get("authority_status") == "REVOKED":
            problems.append("operating authority revoked")
        if not cargo:
            problems.append("no cargo insurance policy on file")
        elif latest_expiry is None or latest_expiry < ctx.today:
            problems.append("cargo insurance expired")
        elif delivery and latest_expiry < delivery:
            problems.append("cargo insurance expires before delivery")

        if problems:
            severity: Severity = "HIGH"
            title = problems[0][0].upper() + problems[0][1:]
        elif latest_expiry and (latest_expiry - ctx.today).days <= 30:
            severity, title = "MEDIUM", "Carrier cargo insurance expires within 30 days"
        else:
            continue

        on_road = _effective_status(s) in AFTER_PICKUP
        if severity == "MEDIUM":
            action = "Request an updated certificate of insurance"
        elif on_road:
            action = "Freight is on the road: get a current cargo certificate from the carrier now"
        else:
            action = "Re-cover with a compliant carrier"

        d = days_until(_next_milestone_date(s), ctx.today)
        items.append(_shipment_item(
            s,
            kind="CARRIER_COMPLIANCE",
            severity=severity,
            score=score(severity, d, s.get("sell_total") or 0),
            title=title,
            evidence={"carrier_id": carrier_id, "carrier_status": vendor.get("status"),
                      "authority_status": vendor.get("authority_status"),
                      "cargo_policies": [{"insurer": p.get("insurer"), "expiration_date": p.get("expiration_date"),
                                          "insurance_status": p.get("insurance_status")} for p in cargo],
                      "problems": problems, "shipment_status": s.get("status"),
                      "pickup_date": s.get("pickup_date"), "delivery_date": s.get("delivery_date_target"),
                      "lane": _lane(s)},
            suggested_action=action,
        ))
    return items


def missing_rate_con(ctx: RiskContext) -> list[RiskItem]:
    """COVERED TL/EXPEDITE with no generated rate confirmation (rate_con_doc_id), so it can't be
    dispatched -> HIGH if pickup ≤1 day, else MEDIUM. LTL dispatches on a PRO number instead."""
    items = []
    for s in ctx.shipments:
        if s.get("status") != "COVERED" or s.get("mode") == "LTL" or s.get("rate_con_doc_id"):
            continue
        d = days_until(s.get("pickup_date"), ctx.today)
        severity: Severity = "HIGH" if d is not None and d <= 1 else "MEDIUM"
        items.append(_shipment_item(
            s,
            kind="MISSING_RATE_CON",
            severity=severity,
            score=score(severity, d, s.get("sell_total") or 0),
            title="No rate confirmation, can't dispatch",
            evidence={"status": s["status"], "mode": s.get("mode"), "carrier_id": s.get("carrier_id"),
                      "rate_con_doc_id": None, "pickup_date": s.get("pickup_date"), "days_until_pickup": d,
                      "lane": _lane(s)},
            suggested_action="Generate and send the rate confirmation",
        ))
    return items


def tracking_overdue(ctx: RiskContext) -> list[RiskItem]:
    """In the tracking queue with overdue=true -> MEDIUM (HIGH if delivering today or already late)."""
    items = []
    for s in ctx.tracking_queue:
        if not s.get("overdue"):
            continue
        d = days_until(s.get("delivery_date_target"), ctx.today)
        severity: Severity = "HIGH" if d is not None and d <= 0 else "MEDIUM"
        items.append(_shipment_item(
            s,
            kind="TRACKING_OVERDUE",
            severity=severity,
            score=score(severity, d, s.get("sell_total") or 0),
            title="Check call overdue" + (", delivering today" if d == 0 else ", delivery date passed" if d is not None and d < 0 else ""),
            evidence={"status": s.get("status"), "next_tracking_at": s.get("next_tracking_at"),
                      "carrier_id": s.get("carrier_id"), "delivery_date": s.get("delivery_date_target"),
                      "days_until_delivery": d, "lane": _lane(s)},
            suggested_action="Make a check call",
        ))
    return items


def mabd_risk(ctx: RiskContext) -> list[RiskItem]:
    """A stop with a must-arrive-by date that is at risk, citing the facility's mabd_program_note:
    - window ends after the MABD, or the MABD has passed -> HIGH
    - appointment NEEDED or MISSED -> HIGH if the MABD is within 3 days, else MEDIUM.
    One item per shipment (its most urgent stop)."""
    items = []
    for s in ctx.shipments:
        found = []
        for stop in s.get("stops") or []:
            mabd = parse_date(stop.get("mabd_date"))
            if not mabd or _stop_is_done(s, stop):
                continue
            window_end = parse_date((stop.get("scheduled_window") or {}).get("latest_local"))
            appt = _appt_status(stop)
            d = (mabd - ctx.today).days
            if d < 0:
                severity, title = "HIGH", "Must-arrive-by date has passed"
            elif window_end and window_end > mabd:
                severity, title = "HIGH", "Delivery window ends after the must-arrive-by date"
            elif appt in ("NEEDED", "MISSED"):
                severity = "HIGH" if d <= 3 else "MEDIUM"
                title = f"Must-arrive-by in {d} day{'s' if d != 1 else ''}, appointment {appt.lower()}"
            else:
                continue
            found.append((severity, d, title, stop))
        if not found:
            continue
        severity, d, title, stop = min(found, key=lambda f: (f[0] != "HIGH", f[1]))
        fac = ctx.facilities.get(stop.get("facility_id") or "", {})
        items.append(_shipment_item(
            s,
            kind="MABD_RISK",
            severity=severity,
            score=score(severity, d, s.get("sell_total") or 0),
            title=title,
            evidence={"stop_seq": stop.get("stop_seq"), "facility_id": stop.get("facility_id"),
                      "facility_name": fac.get("name"), "mabd_date": stop.get("mabd_date"), "days_until_mabd": d,
                      "window_end": (stop.get("scheduled_window") or {}).get("latest_local"),
                      "appointment_status": _appt_status(stop),
                      "mabd_program_note": fac.get("mabd_program_note"),
                      "scheduling_method": fac.get("scheduling_method"), "lane": _lane(s)},
            suggested_action="Book or move the appointment to land before the MABD",
        ))
    return items


def appointment_needed(ctx: RiskContext) -> list[RiskItem]:
    """Appointment NEEDED or MISSED with the stop within 3 days -> MEDIUM, or HIGH when there is less
    time left than the facility's appointment_lead_time_hours. Stops with an MABD are left to mabd_risk.
    One item per shipment (its soonest stop)."""
    items = []
    for s in ctx.shipments:
        found = []
        for stop in s.get("stops") or []:
            appt = _appt_status(stop)
            if appt not in ("NEEDED", "MISSED") or stop.get("mabd_date") or _stop_is_done(s, stop):
                continue
            fallback = s.get("pickup_date") if stop.get("stop_type") == "PICKUP" else s.get("delivery_date_target")
            d = days_until(_stop_date(stop) or fallback, ctx.today)
            if d is None or d > 3:
                continue
            found.append((d, stop))
        if not found:
            continue
        d, stop = min(found, key=lambda f: f[0])
        fac = ctx.facilities.get(stop.get("facility_id") or "", {})
        lead = fac.get("appointment_lead_time_hours")
        too_late = lead is not None and max(d, 0) * 24 < lead
        severity: Severity = "HIGH" if too_late else "MEDIUM"
        kind = "pickup" if stop.get("stop_type") == "PICKUP" else "delivery"
        appt = _appt_status(stop)
        items.append(_shipment_item(
            s,
            kind="APPOINTMENT_NEEDED",
            severity=severity,
            score=score(severity, d, s.get("sell_total") or 0),
            title=(f"Missed {kind} appointment" if appt == "MISSED" else f"No {kind} appointment booked")
                  + (", inside the facility's lead time" if too_late else ""),
            evidence={"stop_seq": stop.get("stop_seq"), "stop_type": stop.get("stop_type"),
                      "facility_id": stop.get("facility_id"), "facility_name": fac.get("name"),
                      "stop_date": _stop_date(stop), "days_until_stop": d, "appointment_status": appt,
                      "appointment_lead_time_hours": lead, "scheduling_method": fac.get("scheduling_method"),
                      "lane": _lane(s)},
            suggested_action=f"Book the {kind} appointment" + (f" ({fac['scheduling_method'].lower()})" if fac.get("scheduling_method") else ""),
        ))
    return items


def hold_reconfirm(ctx: RiskContext) -> list[RiskItem]:
    """ON_HOLD before pickup, with the pickup date today or past -> MEDIUM.
    Holds after pickup (e.g. a claim) are not about the pickup date, so they are skipped."""
    items = []
    for s in ctx.shipments:
        if s.get("status") != "ON_HOLD" or (s.get("held_from") and s["held_from"] not in PRE_PICKUP):
            continue
        d = days_until(s.get("pickup_date"), ctx.today)
        if d is None or d > 0:
            continue
        items.append(_shipment_item(
            s,
            kind="HOLD_RECONFIRM",
            severity="MEDIUM",
            score=score("MEDIUM", d, s.get("sell_total") or 0),
            title="On hold with the pickup date " + ("today" if d == 0 else "passed"),
            evidence={"status": s["status"], "held_from": s.get("held_from"), "hold_reason": s.get("hold_reason"),
                      "pickup_date": s.get("pickup_date"), "days_until_pickup": d,
                      "customer_id": s.get("customer_id"), "lane": _lane(s)},
            suggested_action="Re-confirm the pickup date with the customer",
        ))
    return items


def quote_expiring(ctx: RiskContext) -> list[RiskItem]:
    """QUOTED/SENT with valid_until within 2 days -> LOW. (Cut-list item.)"""
    items = []
    for q in ctx.quotes:
        if q.get("status") not in ("QUOTED", "SENT"):
            continue
        d = days_until(q.get("valid_until"), ctx.today)
        if d is None or d < 0 or d > 2:
            continue
        o, dest = q.get("origin") or {}, q.get("destination") or {}
        items.append(RiskItem(
            kind="QUOTE_EXPIRING",
            severity="LOW",
            score=score("LOW", d, q.get("sell_total") or 0),
            record_type="quote",
            record_id=str(q["quote_id"]),
            title="Quote expires " + ("today" if d == 0 else f"in {d} day{'s' if d != 1 else ''}"),
            evidence={"status": q.get("status"), "valid_until": q.get("valid_until"), "days_until_expiry": d,
                      "customer_id": q.get("customer_id"), "sell_total": q.get("sell_total"),
                      "lane": f"{o.get('city')},{o.get('state')} → {dest.get('city')},{dest.get('state')}"},
            suggested_action="Follow up with the customer before the price expires",
        ))
    return items


def low_margin(ctx: RiskContext) -> list[RiskItem]:
    """Open load gp_pct below the customer's floor -> MEDIUM; negative -> HIGH. (Cut-list item.)

    TL fuel passes through at cost, so a TL load priced at the floor shows a blended GP slightly
    under it. For TL, only flag when the linehaul (FRT) margin is also below the floor."""
    items = []
    for s in ctx.shipments:
        gp, sell = s.get("gp_pct"), s.get("sell_total") or 0
        if gp is None or sell <= 0:
            continue
        floor = float((ctx.customers.get(s.get("customer_id") or "") or {}).get("margin_floor_pct") or DEFAULT_MARGIN_FLOOR)
        frt = _frt_margin_pct(s) if s.get("mode") == "TL" else None
        if gp < 0:
            severity: Severity = "HIGH"
        elif gp < floor and not (frt is not None and frt >= floor - 0.1):
            severity = "MEDIUM"
        else:
            continue
        shortfall = round((floor - gp) / 100 * sell, 2)
        d = days_until(_next_milestone_date(s), ctx.today)
        items.append(_shipment_item(
            s,
            kind="LOW_MARGIN",
            severity=severity,
            score=score(severity, d, shortfall),
            title="Load is losing money" if gp < 0 else f"Margin {gp}% below the {floor:g}% floor",
            evidence={"gp_pct": gp, "margin_floor_pct": floor, "linehaul_gp_pct": frt,
                      "sell_total": sell, "buy_total": s.get("buy_total"), "gp_amount": s.get("gp_amount"),
                      "shortfall_to_floor": shortfall, "customer_id": s.get("customer_id"),
                      "mode": s.get("mode"), "lane": _lane(s)},
            suggested_action="Review the carrier rate and any unbilled accessorials",
        ))
    return items


def late_eta(ctx: RiskContext) -> list[RiskItem]:
    """Tracking (check calls + legal driving hours) says the load is late or at risk.
    HIGH: arrived after the window closed; projected to miss the MABD or the window by >2 h;
    or no arrival recorded after the window closed. MEDIUM: other late or at-risk loads."""
    by_id = {str(s["shipment_id"]): s for s in ctx.shipments}
    items = []
    for sid, st in ctx.tracking.items():
        if st.status not in ("LATE", "AT_RISK"):
            continue
        s = by_id.get(sid, {"shipment_id": sid})
        late = st.minutes_late or 0
        misses_mabd = (st.minutes_late_mabd or 0) > 0
        arrived_late = st.status == "LATE" and st.stage == "AT_DELIVERY"
        no_show = any(x.startswith(("No arrival recorded", "Driver's ETA passed")) for x in st.exceptions)
        severity: Severity = "HIGH" if st.status == "LATE" and (
            arrived_late or misses_mabd or late > 120 or no_show) else "MEDIUM"
        if arrived_late:
            title = f"Arrived {late / 60:.1f} h after the delivery window closed"
            action = "Rebook the delivery appointment with the receiver and tell the customer"
        elif st.status == "LATE":
            title = f"Projected {late / 60:.1f} h past the delivery window" + (", misses MABD" if misses_mabd else "")
            action = "Call the driver: confirm location, hours left and ETA; then warn the receiver and customer"
        else:
            title = st.exceptions[0] if st.exceptions else (
                f"Only {-late} min of slack before the window closes" if late <= 0
                else f"Projected {late} min past the window end (within the {LATE_TOLERANCE_MIN}-min tolerance)")
            action = "Call the driver to confirm location and ETA"
        tz_name = st.delivery_tz
        pos = st.position
        d = days_until(st.window_end.date().isoformat() if st.window_end else None, ctx.today)
        items.append(_shipment_item(
            s,
            kind="LATE_ETA",
            severity=severity,
            score=score(severity, d, s.get("sell_total") or 0),
            title=title,
            evidence={"stage": st.stage, "tracking_status": st.status,
                      "last_position": f"{pos.city}, {pos.state}" if pos else None,
                      "last_position_at": fmt_local(pos.at, tz_name) if pos else None,
                      "hours_since_update": st.hours_since_report, "miles_remaining": st.miles_remaining,
                      "our_eta_legal_hours": fmt_local(st.eta, tz_name),
                      "driver_reported_eta": fmt_local(st.driver_eta, tz_name) if st.driver_eta else None,
                      "window_end": fmt_local(st.window_end, tz_name), "mabd_end": fmt_local(st.mabd_end, tz_name),
                      "minutes_late": st.minutes_late, "minutes_late_mabd": st.minutes_late_mabd,
                      "exceptions": st.exceptions, "eta_assumptions": st.assumptions,
                      "carrier_id": s.get("carrier_id"), "data_source": "TMS check calls and status changes",
                      "lane": _lane(s)},
            suggested_action=action,
        ))
    return items


ALL_RULES = [
    carrier_compliance,
    missing_rate_con,
    uncovered_soon,
    tracking_overdue,
    mabd_risk,
    appointment_needed,
    hold_reconfirm,
    quote_expiring,
    low_margin,
    late_eta,
]


def run_all(ctx: RiskContext) -> list[RiskItem]:
    items: list[RiskItem] = []
    for rule in ALL_RULES:
        items += rule(ctx)
    return items
