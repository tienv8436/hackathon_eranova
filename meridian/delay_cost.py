"""Delay cost: what a load's disruptions cost, in dollars, split by who pays. Owner: agent & evals lead.

Source: the team's simulated cost model, snapshots/Delay_Cost_Model.xlsx (fake_data branch @ b76d73d).
It holds disruption events per shipment (weather, missed appointment, carrier fall-off, detention…), the
cost lines each one causes, and which of those are billed back to the customer or charged back to the
carrier. The TMS has none of this, so every result is SIMULATED and says so (DelayCost.simulated).

Adapted from Allison Wong's workbook adapter (ui/synthetic_costs.py and ui/delay_cost_model.py on
feat/tracking-cost-estimates, commit a7f8042), moved out of the UI so the board can use it:
  - the workbook is read with the standard library (no openpyxl, no formulas or macros run), and its
    SHA-256 is checked, so a changed file is never used without updating SOURCE_SHA256
  - join: shipment → events → unique cost lines; parent/root links are checked (including cycles),
    exact duplicate lines count once, conflicting ones are rejected
  - line amount = ROUND(quantity × rate, 2); sign +1 = cost, −1 = recovery
    gross = Σ costs, recoveries = Σ recoveries, net = gross − recoveries
    already in TMS = Σ signed lines flagged in_tms; additional = net − already in TMS
    GP after = the model's baseline GP − additional
  - every total is checked against the workbook's own Shipment_Rollup to the cent
  - unknown stays unknown: a missing amount gives None, never a made-up zero, and a load with no modeled
    event gets no DelayCost at all (unavailable, not zero)

Added here: who pays. A recovery line names the party it is billed to (recoverable_from: CUSTOMER or
CARRIER); whatever is not recovered stays on our margin. So customer + carrier + our margin = gross.

Projections (kind="projected"): most open loads have no modeled event, including late ones. For a load
that tracking says will miss its delivery window, project() builds the cost the same way the model
prices a missed appointment, from the model's own rates (Assumptions sheet) and averages:
  - re-appointment fee (APPT_RATE), if the receiver works by appointment
  - a layover night (LAY_RATE) for each night until the receiver's next open receiving slot, from its
    receiving hours, our ETA and its average unload time
  - an OTIF chargeback (OTIF_PCT × PO value) when the must-arrive-by date is missed at a facility in an
    MABD program; PO value = declared value, else weight × the model's $/lb for the customer
  - dispatch labor (OPS_RATE_HR × the model's average hours for that kind of disruption)
Who pays depends on the cause (late reason code), as in the model: a carrier-caused layover is disputed
back to the carrier, a receiver-caused one (B1) is billed to the customer, weather stays with us. With
no reason code on the check call, the cause is inferred from tracking where it can be (the driver's ETA
was impossible at legal hours = carrier; on time without the forecast weather/traffic = weather);
otherwise we carry it. These projections are NOT checked against the model's outcomes: they use the
same rates that generated them, so a match would prove nothing.

    from meridian import delay_cost
    cost = delay_cost.estimate("7300117", shipment)        # modeled: DelayCost | None
    cost = delay_cost.project(sid, shipment, status, facilities, outlook)   # projected: DelayCost | None
    costs, errors = delay_cost.estimate_all(shipments, statuses=..., facilities=..., outlooks=...)

    python -m meridian.delay_cost                          # every open load in the snapshot
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from posixpath import normpath
from typing import TYPE_CHECKING, Literal
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from pydantic import BaseModel

from . import geo, tracking
from .client import SNAPSHOT_DIR
from .models import TrackingStatus

if TYPE_CHECKING:
    from .outlook import DelayOutlook

WORKBOOK = SNAPSHOT_DIR / "Delay_Cost_Model.xlsx"
SOURCE = "Delay_Cost_Model.xlsx (fake_data @ b76d73d)"
SOURCE_SHA256 = "ffe014e555faaf851f8891903a90cc269d069035531888cc1542789f77a9c997"
NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
REL_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
ZERO = Decimal("0.00")


# --------------------------------------------------------------------------- results

class CostEvent(BaseModel):
    """One modeled disruption on the load. A child event (parent_event_id) is a knock-on of its parent,
    e.g. a missed appointment caused by weather."""
    event_id: str
    parent_event_id: str | None = None
    label: str                         # "Weather / road closure"
    complication_type: str             # WEATHER, MISSED_APPOINTMENT, CARRIER_FALLOFF, …
    cause_code: str | None = None      # late reason code: AI, T3, WEATHER, NS (normal status), …
    cause_owner: str | None = None     # CARRIER, CONSIGNEE, SHIPPER, CUSTOMER, BROKER, EXTERNAL
    delay_hours: float | None = None
    detail: str = ""
    model_status: str | None = None    # the workbook's cost_status (a simulation label, not a verified charge)


class CostLine(BaseModel):
    line_id: str
    event_id: str
    category: str                      # CARRIER_ACCESSORIAL, OPS_LABOR, CUSTOMER_RECOVERY, CARRIER_CHARGEBACK, …
    ledger: str                        # BUY (paid to carrier), SELL (billed to customer), OFF_LEDGER (our own cost)
    charge_code: str                   # DET, LAY, APPT, OPS, FRT, …
    quantity: float | None = None
    rate: float | None = None
    amount_usd: float | None = None    # ROUND(quantity × rate, 2); None = unknown
    sign: int                          # +1 cost, −1 recovery
    payer: str                         # who ends up paying: BROKER for costs, CUSTOMER/CARRIER for recoveries
    in_tms: bool | None = None         # already on the model's TMS ledger
    description: str = ""
    source_row: int | None = None      # Cost_Lines row; None for a projected line


class DelayCost(BaseModel):
    """Who pays for a load's delays, in dollars. None = unknown (never a stand-in zero)."""
    customer_usd: float | None = None          # billed back to the customer
    carrier_usd: float | None = None           # charged back to the carrier
    broker_margin_usd: float | None = None     # left on us = gross − recoveries
    basis: list[str] = []                      # what the numbers assume, one line each
    simulated: bool = True
    kind: Literal["modeled", "projected"] = "modeled"   # from the model's events, or projected from tracking
    gross_usd: float | None = None
    recoveries_usd: float | None = None
    already_in_tms_usd: float | None = None    # net of lines the model says are already on the TMS ledger
    additional_usd: float | None = None        # what the margin still loses beyond that
    gp_before_usd: float | None = None         # the model's baseline GP for the load
    gp_after_usd: float | None = None
    off_ledger_usd: float | None = None        # our labor / cost of capital: real cost, never on a TMS invoice
    events: list[CostEvent] = []
    lines: list[CostLine] = []
    mitigations: list[str] = []                # the model's suggestions, "OWNER: solution"
    source: str = SOURCE


# --------------------------------------------------------------------------- arithmetic (Decimal, cents)

def number(value) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        n = Decimal(str(value))
    except InvalidOperation:
        return None
    return n if n.is_finite() else None


def dollars(value) -> Decimal | None:
    """Round to cents; also removes Excel float noise such as 630.6400000000001."""
    n = number(value)
    return n.quantize(Decimal(".01"), rounding=ROUND_HALF_UP) if n is not None else None


def _product(*values) -> Decimal | None:
    nums = [number(v) for v in values]
    if any(n is None or n < 0 for n in nums):
        return None
    out = Decimal(1)
    for n in nums:
        out *= n
    return dollars(out)


def _sum(values) -> Decimal | None:
    values = list(values)
    return None if any(v is None for v in values) else dollars(sum(values, ZERO))


def _f(d: Decimal | None) -> float | None:
    return float(d) if d is not None else None


def _usd(d: Decimal | float | None) -> str:
    return "unknown" if d is None else (f"−${-d:,.2f}" if d < 0 else f"${d:,.2f}")


# --------------------------------------------------------------------------- the workbook

def read_workbook(path: Path = WORKBOOK) -> dict[str, list[dict]]:
    """Every sheet as a list of {header: value} rows, from stored values and cached formula results."""
    result = {}
    with ZipFile(path) as archive:
        strings = []
        if "xl/sharedStrings.xml" in archive.namelist():
            strings = ["".join(si.itertext())
                       for si in ET.fromstring(archive.read("xl/sharedStrings.xml")).findall("s:si", NS)]
        rels = {r.attrib["Id"]: r.attrib["Target"] for r in ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))}
        for sheet in ET.fromstring(archive.read("xl/workbook.xml")).findall("s:sheets/s:sheet", NS):
            target = rels[sheet.attrib[REL_ID]]
            target = target.lstrip("/") if target.startswith("/") else normpath("xl/" + target)
            rows, headers = [], {}
            for row in ET.fromstring(archive.read(target)).findall("s:sheetData/s:row", NS):
                values = {}
                for cell in row.findall("s:c", NS):
                    column = "".join(c for c in cell.attrib["r"] if c.isalpha())
                    v = cell.find("s:v", NS)
                    value = v.text if v is not None else None
                    kind = cell.attrib.get("t")
                    if kind == "s":
                        value = strings[int(value)]
                    elif kind == "inlineStr":
                        inline = cell.find("s:is", NS)
                        value = "".join(inline.itertext()) if inline is not None else ""
                    elif kind == "b":
                        value = value == "1"
                    values[column] = value
                if not headers:
                    headers = values
                else:
                    record = {name: values.get(col) for col, name in headers.items() if name}
                    record["_row"] = int(row.attrib["r"])
                    rows.append(record)
            result[sheet.attrib["name"]] = rows
    return result


@lru_cache(maxsize=1)
def load_model() -> dict[str, list[dict]]:
    if sha256(WORKBOOK.read_bytes()).hexdigest() != SOURCE_SHA256:
        raise ValueError(f"{WORKBOOK.name} differs from the documented source; check where it came from "
                         "and update SOURCE_SHA256.")
    return read_workbook()


def _unique(rows: list[dict], key: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for row in rows:
        ident = row.get(key)
        if not ident:
            raise ValueError(f"Workbook row without {key}")
        if ident in out and row != out[ident]:
            raise ValueError(f"Conflicting workbook {key}: {ident}")
        out[ident] = row
    return out


def _check_links(events: dict[str, dict]) -> None:
    for event_id, event in events.items():
        root = events.get(event.get("root_event_id"))
        if root is None or root.get("parent_event_id") or root.get("root_event_id") != root["event_id"]:
            raise ValueError(f"Invalid workbook root relationship for {event_id}")
        parent = event.get("parent_event_id")
        if parent and (parent not in events or events[parent]["root_event_id"] != event["root_event_id"]):
            raise ValueError(f"Invalid workbook parent relationship for {event_id}")
        seen, cursor = set(), event_id
        while cursor:
            if cursor in seen:
                raise ValueError("Cyclic workbook event relationship")
            seen.add(cursor)
            cursor = events[cursor].get("parent_event_id")


# --------------------------------------------------------------------------- the calculator

def estimate(shipment_id: str, shipment: dict | None = None, model: dict | None = None) -> DelayCost | None:
    """Delay cost for one load, or None when the model has no event for it (unavailable, not zero).
    `shipment`: the TMS record, only used to say when its GP differs from the model's baseline.
    Raises ValueError when the workbook contradicts itself."""
    model = load_model() if model is None else model
    sid = str(shipment_id)
    events = {k: e for k, e in _unique(model["Events"], "event_id").items() if str(e["shipment_id"]) == sid}
    if not events:
        return None
    _check_links(events)

    picked = [r for r in model["Cost_Lines"] if str(r.get("shipment_id")) == sid or r.get("event_id") in events]
    lines: list[tuple[dict, Decimal | None, int]] = []      # (row, amount, sign)
    for row in _unique(picked, "line_id").values():
        event = events.get(row.get("event_id"))
        if event is None or str(row["shipment_id"]) != sid or row.get("root_event_id") != event["root_event_id"]:
            raise ValueError(f"Workbook cost line {row['line_id']} does not match its shipment/event/root IDs")
        amount = _product(row.get("quantity"), row.get("rate"))
        if (cached := dollars(row.get("amount"))) is not None and amount is not None and cached != amount:
            raise ValueError(f"Workbook amount disagrees with quantity × rate: {row['line_id']}")
        sign = number(row.get("sign"))
        if sign not in (Decimal(1), Decimal(-1)):
            raise ValueError(f"Workbook line sign must be +1 (cost) or −1 (recovery): {row['line_id']}")
        signed = dollars(amount * sign) if amount is not None else None
        if (cached := dollars(row.get("net_cost"))) is not None and signed is not None and cached != signed:
            raise ValueError(f"Workbook signed amount disagrees with the cost line: {row['line_id']}")
        lines.append((row, amount, int(sign)))

    gross, recoveries, net, customer, carrier, off_ledger, other = _split(lines)
    flags_known = all(isinstance(r.get("in_tms"), bool) for r, _, _ in lines)
    recorded = _sum(dollars(a * s) if a is not None else None for r, a, s in lines if r.get("in_tms") is True) \
        if flags_known else None
    additional = dollars(net - recorded) if net is not None and recorded is not None else None

    rollups = [r for r in model["Shipment_Rollup"] if str(r.get("shipment_id")) == sid]
    if len(rollups) > 1:
        raise ValueError(f"Duplicate workbook shipment rollup for {sid}")
    rollup = rollups[0] if rollups else None
    gp_before = dollars(rollup.get("gp_amount_tms")) if rollup else None
    gp_after = dollars(gp_before - additional) if gp_before is not None and additional is not None else None
    if rollup:   # the workbook's own totals must agree with the line arithmetic, to the cent
        for field, value in (("gross_complication_cost", gross), ("recoveries", recoveries),
                             ("net_complication_cost", net), ("already_in_tms_net", recorded),
                             ("gp_after_complications", gp_after)):
            cached = dollars(rollup.get(field))
            if cached is not None and value is not None and cached != value:
                raise ValueError(f"Workbook rollup check failed: {sid} / {field}")

    basis = [f"Simulated: {SOURCE}, matched by shipment ID. Not invoices or confirmed charges.",
             "Line = ROUND(quantity × rate, 2). Recoveries are billed to the party the model names; "
             "what isn't recovered stays on our margin."]
    if off_ledger:
        basis.append(f"Includes {_usd(off_ledger)} of our own cost (dispatch labor, cost of capital) "
                     "that never shows on a TMS invoice.")
    if recorded:
        basis.append(f"{_usd(recorded)} of this is already on the TMS ledger, so GP falls by "
                     f"the additional {_usd(additional)} only.")
    if other:
        basis.append(f"{len(other)} recovery line(s) name no customer or carrier; counted in recoveries only.")
    if any(a is None for _, a, _ in lines) or not flags_known:
        basis.append("Some line amounts or ledger flags are missing in the model, so some totals are unknown.")
    tms_gp = dollars((shipment or {}).get("gp_amount"))
    if shipment is not None and gp_before is not None and tms_gp != gp_before:
        basis.append(f"TMS GP is {_usd(tms_gp)}; the model's baseline is {_usd(gp_before)}. "
                     "GP after uses the model's baseline.")

    rec_rows = [r for r in model.get("Recommendations", []) if r.get("event_id") in events and str(r.get("shipment_id")) == sid]
    return DelayCost(
        customer_usd=_f(customer), carrier_usd=_f(carrier), broker_margin_usd=_f(net), basis=basis,
        gross_usd=_f(gross), recoveries_usd=_f(recoveries), already_in_tms_usd=_f(recorded),
        additional_usd=_f(additional), gp_before_usd=_f(gp_before), gp_after_usd=_f(gp_after),
        off_ledger_usd=_f(off_ledger),
        events=[CostEvent(event_id=e["event_id"], parent_event_id=e.get("parent_event_id"),
                          label=e.get("complication_label") or e.get("complication_type") or "",
                          complication_type=e.get("complication_type") or "", cause_code=e.get("late_reason_code"),
                          cause_owner=e.get("root_cause_owner"), delay_hours=_f(number(e.get("delay_hours"))),
                          detail=e.get("detail") or "", model_status=e.get("cost_status"))
                for e in sorted(events.values(), key=lambda e: e["event_id"])],
        lines=_cost_lines(lines),
        mitigations=[f"{r.get('action_owner') or 'BROKER'}: {r['primary_solution']}"
                     for r in rec_rows if r.get("primary_solution")],
    )


def _split(lines: list[tuple[dict, Decimal | None, int]]):
    """(gross, recoveries, net, customer, carrier, off_ledger, other recovery rows) from (row, amount, sign)."""
    costs = [(r, a) for r, a, s in lines if s == 1]
    recs = [(r, a) for r, a, s in lines if s == -1]
    gross = _sum(a for _, a in costs)
    recoveries = _sum(a for _, a in recs)
    net = dollars(gross - recoveries) if gross is not None and recoveries is not None else None
    by_payer = lambda who: _sum(a for r, a in recs if (r.get("recoverable_from") or "").upper() == who)  # noqa: E731
    other = [r for r, _ in recs if (r.get("recoverable_from") or "").upper() not in ("CUSTOMER", "CARRIER")]
    off_ledger = _sum(a for r, a in costs if r.get("ledger") == "OFF_LEDGER")
    return gross, recoveries, net, by_payer("CUSTOMER"), by_payer("CARRIER"), off_ledger, other


def _cost_lines(lines: list[tuple[dict, Decimal | None, int]]) -> list[CostLine]:
    return [CostLine(line_id=r["line_id"], event_id=r["event_id"], category=r.get("cost_category") or "",
                     ledger=r.get("ledger") or "", charge_code=r.get("charge_code") or "",
                     quantity=_f(number(r.get("quantity"))), rate=_f(number(r.get("rate"))), amount_usd=_f(a),
                     sign=s, payer="BROKER" if s == 1 else (r.get("recoverable_from") or "UNKNOWN").upper(),
                     in_tms=r.get("in_tms") if isinstance(r.get("in_tms"), bool) else None,
                     description=r.get("description") or "", source_row=r.get("_row"))
            for r, a, s in sorted(lines, key=lambda x: x[0]["line_id"])]


# --------------------------------------------------------------------------- projections from tracking

# Late reason code → whose fault (the model's root_cause_owner for the same codes).
CAUSE_BY_CODE = {"AI": "CARRIER", "MECHANICAL": "CARRIER", "T3": "CARRIER", "D1": "CARRIER",
                 "AO": "EXTERNAL", "WEATHER": "EXTERNAL", "B1": "CONSIGNEE"}
# Cause → who a layover is billed back to. In the model a carrier-caused layover is disputed back to the
# carrier and a closed receiver's is billed to the customer; weather and unknown causes stay with us.
LAYOVER_BILLED_TO = {"CARRIER": "CARRIER", "CONSIGNEE": "CUSTOMER"}
BOOKED_APPOINTMENT = {"CONFIRMED", "MISSED"}     # stop appointment statuses where there is one to miss


def _g(x: Decimal | None) -> str:
    """A rate or hours for a sentence; a missing one reads 'unknown' instead of failing the whole load."""
    if x is None:
        return "unknown"
    s = format(x, "f")                 # Decimal: no exponent, then drop trailing zeros (3.00 -> 3)
    return s.rstrip("0").rstrip(".") if "." in s else s


def _rate(model: dict, key: str) -> Decimal | None:
    return next((number(r.get("value")) for r in model.get("Assumptions", []) if r.get("key") == key), None)


def _avg_ops_hours(model: dict, complication_type: str) -> Decimal | None:
    """The model's average dispatch-labor hours for one kind of disruption."""
    events = {e["event_id"] for e in model["Events"] if e.get("complication_type") == complication_type}
    hours = [number(r.get("quantity")) for r in model["Cost_Lines"]
             if r.get("event_id") in events and r.get("charge_code") == "OPS"]
    hours = [h for h in hours if h is not None]
    return (sum(hours, Decimal(0)) / len(hours)).quantize(Decimal(".01"), rounding=ROUND_HALF_UP) if hours else None


def cause_of(status: TrackingStatus, outlook: DelayOutlook | None = None) -> tuple[str | None, str | None, str]:
    """(owner, late reason code, why) for a late load. owner None = not known yet."""
    code = ((status.position.reason_code if status.position else None) or "").upper()
    if code in CAUSE_BY_CODE:
        return CAUSE_BY_CODE[code], code, f"the driver reported reason code {code}"
    if outlook is not None and outlook.status != "LATE" and outlook.predicted_status == "LATE":
        return "EXTERNAL", "WEATHER", "it is on time without the forecast weather and traffic, late with them"
    if (status.eta_gap_minutes or 0) >= tracking.DRIVER_ETA_GAP_MIN:
        return "CARRIER", None, ("the driver's ETA wasn't possible at legal driving hours "
                                 "(trip plan / hours of service, on the carrier's side)")
    return None, None, "no reason code on the check calls yet"


def layover_nights(facility: dict | None, arrival: datetime) -> tuple[int, datetime]:
    """Nights the truck waits for the receiver's next open slot after missing its appointment, and that slot.
    Worked in the same day if the dock is open on arrival and still open once it's unloaded."""
    unload = timedelta(minutes=float((facility or {}).get("avg_unload_minutes") or 120))
    if geo.next_open(facility, arrival) == arrival and geo.next_open(facility, arrival + unload) == arrival + unload:
        return 0, arrival
    zone = geo.tz((facility or {}).get("timezone"))
    loc = arrival.astimezone(zone)
    start = arrival if geo.next_open(facility, arrival) != arrival else \
        datetime.combine(loc.date() + timedelta(days=1), datetime.min.time(), tzinfo=zone).astimezone(geo.UTC)
    slot = geo.next_open(facility, start)
    return max((slot.astimezone(zone).date() - loc.date()).days, 0), slot


def _po_value(shipment: dict, model: dict) -> tuple[Decimal | None, str]:
    if (declared := number(shipment.get("declared_value"))) is not None:
        return declared, "the declared value"
    lb = number((shipment.get("freight_totals") or {}).get("actual_lb"))
    per_lb = _rate(model, f"VALUE_PER_LB_{shipment.get('customer_id')}")
    if lb is None or per_lb is None:
        return None, "unknown (no declared value, weight or $/lb for this customer)"
    return dollars(lb * per_lb), f"{lb:,.0f} lb × ${per_lb:g}/lb (the model's assumption for {shipment.get('customer_id')})"


def project(shipment_id: str, shipment: dict, status: TrackingStatus | None, facilities: dict[str, dict],
            outlook: DelayOutlook | None = None, model: dict | None = None) -> DelayCost | None:
    """Projected delay cost for a load tracking says will miss its delivery window, or None when it won't
    (or isn't tracked). See the module docstring for the method."""
    if status is None or not ((status.minutes_late or 0) > tracking.LATE_TOLERANCE_MIN
                              or (status.minutes_late_mabd or 0) > 0):
        return None
    # A truck past its ETA with no arrival recorded can't arrive before the as-of time.
    arrival = status.arrived_at or (max(status.eta, status.as_of) if status.eta else None)
    if arrival is None:
        return None
    model = load_model() if model is None else model
    sid = str(shipment_id)
    stop = next((s for s in reversed(shipment.get("stops") or []) if s.get("stop_type") == "DELIVERY"), {})
    fac = facilities.get(stop.get("facility_id") or "") or {}
    name = fac.get("name") or stop.get("facility_id") or "the receiver"
    owner, code, why = cause_of(status, outlook)
    late_txt = tracking._dur(max(status.minutes_late or 0, 0))
    tz_name = fac.get("timezone") or status.delivery_tz

    events: list[CostEvent] = []
    lines: list[tuple[dict, Decimal | None, int]] = []
    basis = [f"Projected from tracking, not modeled: our ETA at legal driving hours (plus weather & traffic when on) "
             f"is {late_txt} past the delivery window at {name}. Nothing has been charged yet.",
             f"Cause: {owner.lower() if owner else 'not known'}, because {why}."]

    def line(event_id: str, n: int, category: str, ledger: str, code_: str, qty, rate, sign: int,
             desc: str, payer: str | None = None) -> None:
        row = {"line_id": f"{event_id}-{n}", "event_id": event_id, "cost_category": category, "ledger": ledger,
               "charge_code": code_, "quantity": qty, "rate": rate, "recoverable_from": payer, "description": desc,
               "in_tms": False}
        lines.append((row, _product(qty, rate), sign))

    ops_rate = _rate(model, "OPS_RATE_HR")
    if (status.minutes_late or 0) > tracking.LATE_TOLERANCE_MIN:
        eid = f"PROJ-{sid}-APPT"
        # Every TMS stop carries an `appointment` dict, NOT_REQUIRED and NEEDED (none booked yet) included,
        # so go by its status. A booked appointment is missed even where the receiver also takes walk-ins;
        # with no status on file, fall back to the facility's own rule.
        appt_status = ((stop.get("appointment") or {}).get("status") or "").upper()
        appointment = (appt_status in BOOKED_APPOINTMENT) if appt_status else \
            bool(fac.get("appointment_required") and not fac.get("first_come_first_served"))
        nights, slot = layover_nights(fac, arrival)
        events.append(CostEvent(
            event_id=eid, label="Missed delivery appointment (projected)" if appointment else "Late delivery (projected)",
            complication_type="MISSED_APPOINTMENT", cause_code=code, cause_owner=owner,
            delay_hours=round((status.minutes_late or 0) / 60, 1),
            detail=f"Arrives {geo.fmt_local(arrival, tz_name)}, {late_txt} after the window; "
                   + (f"next receiving slot {geo.fmt_local(slot, tz_name)} ({nights} night{'s' * (nights != 1)})."
                      if nights else "can still be worked in the same day.")))
        n = 0
        if appointment:
            n += 1
            line(eid, n, "CARRIER_ACCESSORIAL", "BUY", "APPT", 1, _rate(model, "APPT_RATE"), 1, "Re-appointment / redelivery")
        if nights:
            lay = _rate(model, "LAY_RATE")
            n += 1
            line(eid, n, "CARRIER_ACCESSORIAL", "BUY", "LAY", nights, lay, 1, f"Layover {nights} night(s) awaiting the next slot")
            if (payer := LAYOVER_BILLED_TO.get(owner or "")):
                n += 1
                line(eid, n, "CARRIER_CHARGEBACK" if payer == "CARRIER" else "CUSTOMER_RECOVERY",
                     "BUY" if payer == "CARRIER" else "SELL", "LAY", nights, lay, -1,
                     f"Layover billed back to the {payer.lower()} ({owner.lower()}-caused)", payer)
        hours = _avg_ops_hours(model, "MISSED_APPOINTMENT")
        n += 1
        line(eid, n, "OPS_LABOR", "OFF_LEDGER", "OPS", hours, ops_rate, 1,
             f"{_g(hours)} h rescheduling with receiver + customer updates (model average)")
        basis.append(f"Priced like the model's missed appointments: ${_g(_rate(model, 'APPT_RATE'))} re-appointment fee"
                     f"{'' if appointment else ' (not charged: no booked appointment at this receiver)'}, "
                     f"${_g(_rate(model, 'LAY_RATE'))} per layover night until the next receiving slot, and "
                     f"dispatch labor at ${_g(ops_rate)}/h.")
        if nights and not LAYOVER_BILLED_TO.get(owner or ""):
            basis.append("The layover stays with us: " + ("weather isn't billable." if owner == "EXTERNAL" else
                                                          "with no cause yet there's no one to bill it to."))

    if (status.minutes_late_mabd or 0) > 0 and fac.get("mabd_program"):
        eid = f"PROJ-{sid}-OTIF"
        po, po_how = _po_value(shipment, model)
        pct = _rate(model, "OTIF_PCT")
        events.append(CostEvent(
            event_id=eid, label="Retailer OTIF / MABD chargeback (projected)", complication_type="OTIF_CHARGEBACK",
            cause_code=code, cause_owner=owner, delay_hours=round(status.minutes_late_mabd / 60, 1),
            detail=f"Misses the must-arrive-by date by {tracking._dur(status.minutes_late_mabd)}. "
                   + (fac.get("mabd_program_note") or "")))
        line(eid, 1, "CUSTOMER_PENALTY", "OFF_LEDGER", "OTIF", 1, dollars(po * pct) if po is not None and pct is not None else None,
             1, "OTIF chargeback passed through by the customer")
        hours = _avg_ops_hours(model, "OTIF_CHARGEBACK")
        line(eid, 2, "OPS_LABOR", "OFF_LEDGER", "OPS", hours, ops_rate, 1,
             f"{_g(hours)} h chargeback dispute package (model average)")
        basis.append(f"OTIF chargeback = {_g(pct * 100 if pct is not None else None)}% of PO value; PO value = {po_how}.")

    if not lines:
        return None
    gross, recoveries, net, customer, carrier, off_ledger, _ = _split(lines)
    gp_before = dollars(shipment.get("gp_amount"))
    gp_after = dollars(gp_before - net) if gp_before is not None and net is not None else None
    basis.append(f"Rates: the cost model's Assumptions sheet ({SOURCE}). Not checked against the model's "
                 "outcomes, since those were generated from the same rates.")
    return DelayCost(
        kind="projected", customer_usd=_f(customer), carrier_usd=_f(carrier), broker_margin_usd=_f(net),
        basis=basis, gross_usd=_f(gross), recoveries_usd=_f(recoveries), already_in_tms_usd=_f(ZERO),
        additional_usd=_f(net), gp_before_usd=_f(gp_before), gp_after_usd=_f(gp_after),
        off_ledger_usd=_f(off_ledger), events=events, lines=_cost_lines(lines))


def combine(modeled: DelayCost, projected: DelayCost) -> DelayCost:
    """The model's cost plus whatever tracking projects that the model doesn't cover for this load.
    A projected disruption of a type the model already has (e.g. a missed appointment) is left out, so
    nothing counts twice; e.g. the model's weather delay and missed appointment, plus a projected OTIF
    chargeback when tracking says the must-arrive-by date will be missed too."""
    have = {e.complication_type for e in modeled.events}
    events = [e for e in projected.events if e.complication_type not in have]
    if not events:
        return modeled
    ids = {e.event_id for e in events}
    extra = [ln for ln in projected.lines if ln.event_id in ids]
    rows = [({"recoverable_from": ln.payer if ln.sign == -1 else None, "ledger": ln.ledger},
             dollars(ln.amount_usd), ln.sign) for ln in extra]
    gross, recoveries, net, customer, carrier, off_ledger, _ = _split(rows)
    add = lambda a, b: _f(dollars(number(a) + b)) if a is not None and b is not None else None  # noqa: E731
    additional = add(modeled.additional_usd, net)            # nothing projected is on the TMS ledger yet
    gp_after = (_f(dollars(number(modeled.gp_before_usd) - number(additional)))
                if modeled.gp_before_usd is not None and additional is not None else None)
    labels = ", ".join(e.label for e in events)
    kept_appt = any(e.complication_type == "MISSED_APPOINTMENT" for e in events)
    basis = [b for b in projected.basis if not b.startswith("Projected from tracking")
             and (kept_appt or not b.startswith(("Priced like the model's missed", "The layover stays")))]
    return modeled.model_copy(update=dict(
        gross_usd=add(modeled.gross_usd, gross), recoveries_usd=add(modeled.recoveries_usd, recoveries),
        customer_usd=add(modeled.customer_usd, customer), carrier_usd=add(modeled.carrier_usd, carrier),
        broker_margin_usd=add(modeled.broker_margin_usd, net), off_ledger_usd=add(modeled.off_ledger_usd, off_ledger),
        additional_usd=additional, gp_after_usd=gp_after,
        events=modeled.events + events, lines=modeled.lines + extra,
        basis=modeled.basis + [f"Plus, projected from tracking (the model doesn't have it): {labels}."] + basis))


def estimate_all(shipments: dict[str, dict], model: dict | None = None, *,
                 statuses: dict[str, TrackingStatus] | None = None, facilities: dict[str, dict] | None = None,
                 outlooks: dict[str, DelayOutlook] | None = None) -> tuple[dict[str, DelayCost], list[str]]:
    """Delay cost for every load the model covers and, given tracking `statuses`, a projection for each
    late load: on its own when the model has nothing for the load, else only the disruptions the model
    doesn't already have (combine). A load whose rows don't add up is skipped and reported, so one bad
    row never takes the board down."""
    try:
        model = load_model() if model is None else model
    except (OSError, ValueError) as e:
        return {}, [f"Delay cost model unavailable: {e}"]
    statuses, facilities, outlooks = statuses or {}, facilities or {}, outlooks or {}
    out, errors = {}, []
    for sid, ship in shipments.items():
        try:
            cost = estimate(sid, ship, model)
            proj = project(sid, ship, statuses[sid], facilities, outlooks.get(sid), model) if sid in statuses else None
            if cost is None:
                cost = proj
            elif proj is not None:
                cost = combine(cost, proj)
            if cost is not None:
                out[sid] = cost
        except (ValueError, KeyError, TypeError) as e:
            errors.append(f"Delay cost for {sid}: {e}")
    return out, errors


if __name__ == "__main__":
    import json

    read = lambda name: json.loads((SNAPSHOT_DIR / f"{name}.json").read_text(encoding="utf-8"))  # noqa: E731
    tr = tracking.Tracker(read("open_shipments"), {f["facility_id"]: f for f in read("facilities")},
                          read("shipment_events"))     # no weather/traffic here
    costs, errs = estimate_all(tr.shipments, statuses=tr.statuses, facilities=tr.facilities)
    kinds = [c.kind for c in costs.values()]
    print(f"{len(tr.shipments)} open loads: {kinds.count('modeled')} modeled, {kinds.count('projected')} projected "
          f"({SOURCE})")
    for sid, c in sorted(costs.items(), key=lambda kv: -(kv[1].broker_margin_usd or 0)):
        print(f"\n{sid}  [{c.kind}]  gross {_usd(c.gross_usd)} = customer {_usd(c.customer_usd)} + carrier "
              f"{_usd(c.carrier_usd)} + our margin {_usd(c.broker_margin_usd)}   GP {_usd(c.gp_before_usd)} → "
              f"{_usd(c.gp_after_usd)}")
        for e in c.events:
            print(f"    {e.event_id}{' ← ' + e.parent_event_id if e.parent_event_id else ''}  {e.label}"
                  f" (cause {e.cause_owner}/{e.cause_code}) {e.detail if c.kind == 'projected' else ''}")
    for e in errs:
        print("!", e)
