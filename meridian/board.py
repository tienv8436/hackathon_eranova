"""Dispatch board: one view per load, merging tracking, the load watch and (later) delay cost.
Owner: agent & evals lead.

The UI used to show the same problem in several places: a risk card on the load watch, a map dot,
and several feed lines on the tracker ("no update for 36 h", "our ETA passed", "driver's ETA
passed"...). This module folds all of that into one `LoadView` per load:

  - signals:  what is wrong, read from the tracking status fields and the risk items (no LLM)
  - headline: one sentence, built in code, saying the main problem and what it means for delivery
  - actions:  what the dispatcher should do, in order; actions[0] is the next one
  - urgency:  ACT_NOW / WATCH / FINE, and a score to rank loads by consequence
  - phase:    PRE_PICKUP / IN_MOTION / ADMIN, so the board can separate loads on the road
              (where tracking applies) from coverage and paperwork issues

How the pieces connect: meridian.tracking gives each truck a status; the risk rules (including
LATE_ETA, which reads that status) attach issues to loads; build() groups both by load. LATE_ETA
items are not turned into separate actions, since the tracking signals already cover them; their
drafted message is reused on the matching action.

    python -m meridian.board        # the board from snapshots, no weather/traffic
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from . import tracking
from .llm import AUDIENCE_BY_KIND
from .models import RiskItem, TrackingStatus
from .outlook import DelayOutlook
from .risk_rules import PRE_PICKUP

Phase = Literal["PRE_PICKUP", "IN_MOTION", "ADMIN"]
Urgency = Literal["ACT_NOW", "WATCH", "FINE"]
Party = Literal["carrier", "customer", "receiver", "shipper", "internal"]

SignalCode = Literal[
    "ARRIVED_LATE", "DARK", "REPORTED_DELAY", "STUCK_AT_SHIPPER", "DRIVER_ETA_UNREALISTIC",
    "ETA_PASSED", "DRIVER_ETA_PASSED", "PROJECTED_LATE", "MISSES_MABD", "WEATHER_TRAFFIC", "TIGHT", "RISK"]

# Which signal leads the headline when a load has several (first wins). A dark truck explains its
# passed ETAs, so it outranks them; lateness itself is the consequence, so it comes after the causes.
SIGNAL_ORDER: list[SignalCode] = [
    "ARRIVED_LATE", "DARK", "REPORTED_DELAY", "STUCK_AT_SHIPPER", "DRIVER_ETA_UNREALISTIC", "ETA_PASSED",
    "DRIVER_ETA_PASSED", "PROJECTED_LATE", "MISSES_MABD", "RISK", "WEATHER_TRAFFIC", "TIGHT"]
# Signals that need someone to do something now; the rest are "watch".
ACT_SIGNALS = {"ARRIVED_LATE", "DARK", "REPORTED_DELAY", "STUCK_AT_SHIPPER", "DRIVER_ETA_UNREALISTIC",
               "ETA_PASSED", "DRIVER_ETA_PASSED", "PROJECTED_LATE", "MISSES_MABD"}
MOVING = {"PICKED_UP", "IN_TRANSIT"}
AFTER_DELIVERY = {"AT_DELIVERY", "DELIVERED"}
ADMIN_KINDS = {"QUOTE_EXPIRING", "LOW_MARGIN"}       # issues that don't touch the freight itself
URGENCY_BASE = {"ACT_NOW": 2000.0, "WATCH": 1000.0, "FINE": 0.0}


class Signal(BaseModel):
    code: SignalCode
    text: str                          # short, e.g. "No contact for 36 h (last Springfield, MO)"
    act: bool                          # needs action now (else: watch)
    risk_kind: str | None = None       # for code RISK: the RiskItem kind


class Action(BaseModel):
    key: str                           # "call_carrier", "rebook_appointment", "notify_customer", "risk:<KIND>"
    label: str                         # imperative, e.g. "Call the carrier for location, hours left and ETA"
    who: Party
    why: list[str]                     # the signal texts this action answers
    done_when: str                     # what closes it, e.g. "A check call with a position is logged"
    draft: str = ""                    # LLM-drafted message from a matching risk item, if any
    draft_audience: str = ""
    risk_kind: str | None = None       # the load-watch item behind it (for drafting or logging a note)


class DelayCost(BaseModel):
    """Slot for the delay cost estimate (build step 6): who pays for the delay, in dollars.
    Nothing fills it yet."""
    customer_usd: float | None = None
    carrier_usd: float | None = None
    broker_margin_usd: float | None = None
    basis: list[str] = []              # what the numbers assume
    simulated: bool = True


class LoadView(BaseModel):
    record_id: str
    record_type: Literal["shipment", "quote"] = "shipment"
    lane: str = ""
    carrier_id: str | None = None
    customer_id: str | None = None
    stage: str | None = None           # TMS status (effective status for loads on hold)
    phase: Phase
    urgency: Urgency
    score: float                       # higher = look at it first
    headline: str
    signals: list[Signal] = []
    actions: list[Action] = []
    tracking: TrackingStatus | None = None
    outlook: DelayOutlook | None = None
    risks: list[RiskItem] = []
    delay_cost: DelayCost | None = None

    @property
    def next_action(self) -> Action | None:
        return self.actions[0] if self.actions else None


# --------------------------------------------------------------------------- signals

def _dur(minutes: float) -> str:
    return tracking._dur(minutes)


def _mins_since(at: datetime | None, as_of: datetime) -> float | None:
    return (as_of - at).total_seconds() / 60 if at else None


def tracking_signals(st: TrackingStatus, o: DelayOutlook | None = None) -> list[Signal]:
    """What is wrong with a tracked load, from the status fields (not by parsing the exception text,
    except for the at-shipper wait, which has no field of its own)."""
    out: list[Signal] = []
    add = lambda code, text: out.append(Signal(code=code, text=text, act=code in ACT_SIGNALS))  # noqa: E731
    pos, late, late_mabd = st.position, st.minutes_late, st.minutes_late_mabd
    arrived = st.stage == "AT_DELIVERY"

    if arrived:
        if st.status == "LATE" and late is not None and late > 0:
            add("ARRIVED_LATE", f"Arrived {_dur(late)} after the delivery window closed")
        return out

    if st.stage in MOVING and pos and st.hours_since_report is not None \
            and st.hours_since_report >= tracking.STALE_REPORT_H:
        add("DARK", f"No contact for {st.hours_since_report:.0f} h (last {pos.city}, {pos.state})")
    if st.stage in MOVING and pos and pos.reason_code:
        code = pos.reason_code.upper()
        add("REPORTED_DELAY", f"Driver reported {tracking.REASON_TEXT.get(code, 'a delay')} ({code}) "
                              f"at {pos.city}, {pos.state}")
    for x in st.exceptions:
        if x.startswith("At the shipper for"):
            add("STUCK_AT_SHIPPER", x)
    if st.eta_gap_minutes is not None and st.eta_gap_minutes >= tracking.DRIVER_ETA_GAP_MIN:
        add("DRIVER_ETA_UNREALISTIC", f"Driver's ETA is {_dur(st.eta_gap_minutes)} earlier than legal hours allow")
    if st.stage in MOVING:
        if (m := _mins_since(st.eta, st.as_of)) is not None and m > 0:
            add("ETA_PASSED", f"Our ETA passed {_dur(m)} ago with no arrival")
        if (m := _mins_since(st.driver_eta, st.as_of)) is not None and m > 0:
            add("DRIVER_ETA_PASSED", f"Driver's ETA passed {_dur(m)} ago with no arrival")
    if late is not None and late > tracking.LATE_TOLERANCE_MIN:
        add("PROJECTED_LATE", f"Projected {_dur(late)} past the delivery window")
    if late_mabd is not None and late_mabd > 0:
        add("MISSES_MABD", f"Misses the must-arrive-by date by {_dur(late_mabd)}")
    if o and o.delay.minutes:
        parts = [n for n, m in (("weather", o.weather_minutes), ("traffic", o.traffic_minutes)) if m]
        add("WEATHER_TRAFFIC", f"{' and '.join(parts).capitalize() or 'Weather and traffic'} "
                               f"add{'s' if len(parts) == 1 else ''} {_dur(o.delay.minutes)} on the route")
    if st.status == "AT_RISK" and late is not None and late > -tracking.AT_RISK_SLACK_MIN \
            and not any(s.code == "PROJECTED_LATE" for s in out):
        add("TIGHT", f"Only {_dur(-late)} of slack before the window closes" if late <= 0
            else f"Projected {_dur(late)} past the window (within the {tracking.LATE_TOLERANCE_MIN}-min tolerance)")
    return out


def risk_signals(risks: list[RiskItem], stage: str | None = None) -> list[Signal]:
    """One signal per risk item, except LATE_ETA (tracking_signals covers it). An overdue check call
    comes first, as its action (call the carrier) does; then by the rules' score.
    The TMS keeps flagging check calls on loads already at or past the receiver: that is close-out
    paperwork, not a truck to find, so it never needs action now."""
    out = []
    for r in sorted(risks, key=lambda r: (r.kind != "TRACKING_OVERDUE", -r.score)):
        if r.kind == "LATE_ETA":
            continue
        if r.kind == "TRACKING_OVERDUE" and stage in AFTER_DELIVERY:
            out.append(Signal(code="RISK", text="Tracking update overdue, but the truck is "
                                                + ("at the receiver" if stage == "AT_DELIVERY" else "delivered"),
                              act=False, risk_kind=r.kind))
        else:
            out.append(Signal(code="RISK", text=r.title, act=r.severity == "HIGH", risk_kind=r.kind))
    return out


# --------------------------------------------------------------------------- actions

CARRIER_CALL = {"DARK", "REPORTED_DELAY", "DRIVER_ETA_UNREALISTIC", "ETA_PASSED", "DRIVER_ETA_PASSED"}
LATE_CODES = {"PROJECTED_LATE", "MISSES_MABD"}


def _risk(risks: list[RiskItem], kind: str) -> RiskItem | None:
    return next((r for r in risks if r.kind == kind), None)


def actions_for(signals: list[Signal], risks: list[RiskItem], stage: str | None = None) -> list[Action]:
    """The delay playbook, in the order a dispatcher should do it: get a solid ETA from the carrier
    first, then move the appointment, then tell the customer; then any other load-watch issues."""
    by = {s.code: s for s in signals}
    texts = lambda codes: [s.text for s in signals if s.code in codes]  # noqa: E731
    out: list[Action] = []

    if "STUCK_AT_SHIPPER" in by:
        out.append(Action(key="call_shipper", who="shipper", why=texts({"STUCK_AT_SHIPPER"}),
                          label="Call the shipper: when will loading finish?",
                          done_when="Loading time confirmed and noted on the load"))

    carrier_why = texts(CARRIER_CALL)
    overdue = _risk(risks, "TRACKING_OVERDUE")
    if overdue and stage in AFTER_DELIVERY:
        out.append(Action(key="close_out", who="carrier", why=[overdue.title],
                          label="Confirm the unload time and close out tracking (POD)",
                          done_when="Delivery recorded and the tracking item cleared"))
        overdue = None
    if overdue:
        carrier_why.append(overdue.title)
    if carrier_why:
        if "DRIVER_ETA_UNREALISTIC" in by and not by.keys() & {"DARK", "ETA_PASSED", "DRIVER_ETA_PASSED"}:
            label = "Call the carrier: the driver's ETA isn't possible at legal hours, get a realistic one"
        elif "REPORTED_DELAY" in by:
            label = "Call the carrier: get the repair or delay ETA and the driver's hours left"
        else:
            label = "Call the carrier for location, hours left and a new ETA"
        late_item = _risk(risks, "LATE_ETA") or overdue
        out.append(Action(key="call_carrier", who="carrier", why=carrier_why, label=label,
                          done_when="A check call with a position and ETA is logged",
                          draft=late_item.draft_message if late_item else "",
                          draft_audience=late_item.draft_audience if late_item else "",
                          risk_kind=late_item.kind if late_item else None))

    if "ARRIVED_LATE" in by:
        out.append(Action(key="rebook_appointment", who="receiver", why=texts({"ARRIVED_LATE"}),
                          label="Ask the receiver to work the load in, or rebook the delivery",
                          done_when="Receiver agreed to unload, or a new appointment is recorded"))
        out.append(Action(key="notify_customer", who="customer", why=texts({"ARRIVED_LATE"}),
                          label="Tell the customer it arrived late and when it will unload",
                          done_when="Customer update noted on the load"))
    elif by.keys() & LATE_CODES:
        then = " once the carrier confirms the ETA" if carrier_why else ""
        out.append(Action(key="rebook_appointment", who="receiver", why=texts(LATE_CODES),
                          label="Move the delivery appointment" + then
                                + (" (check the facility's late-delivery terms)" if "MISSES_MABD" in by else ""),
                          done_when="A new appointment is recorded"))
        out.append(Action(key="notify_customer", who="customer", why=texts(LATE_CODES),
                          label="Tell the customer the new ETA" + then,
                          done_when="Customer update noted on the load"))
    elif "TIGHT" in by and not carrier_why:
        out.append(Action(key="confirm_eta", who="carrier", why=texts({"TIGHT"}),
                          label="Confirm the ETA with the carrier before the slack runs out",
                          done_when="A check call with an ETA is logged"))

    handled = {"LATE_ETA", "TRACKING_OVERDUE"}
    rebook = next((a for a in out if a.key == "rebook_appointment"), None)
    if rebook:   # one appointment call, not two: fold the delivery-appointment rules into it
        for r in risks:
            if r.kind == "MABD_RISK" or (r.kind == "APPOINTMENT_NEEDED" and r.evidence.get("stop_type") == "DELIVERY"):
                rebook.why.append(r.title)
                handled.add(r.kind)
    for r in sorted(risks, key=lambda r: -r.score):
        if r.kind in handled:
            continue
        handled.add(r.kind)
        out.append(Action(key=f"risk:{r.kind}", who=AUDIENCE_BY_KIND.get(r.kind, "internal"), why=[r.title],
                          label=r.suggested_action, done_when="Resolved and noted on the load",
                          draft=r.draft_message, draft_audience=r.draft_audience, risk_kind=r.kind))
    return out


# --------------------------------------------------------------------------- the view

def _consequence(st: TrackingStatus) -> str:
    """What the projection means for delivery, for the headline."""
    if st.stage == "AT_DELIVERY":
        return "arrived on time" if st.status == "ARRIVED" else ""
    late = st.minutes_late
    if late is None:
        return ""
    if late > tracking.LATE_TOLERANCE_MIN:
        text = f"projected {_dur(late)} past the window"
    elif late > 0:
        text = f"projected {_dur(late)} past the window (within tolerance)"
    else:
        text = f"{_dur(-late)} to spare"
    if (st.minutes_late_mabd or 0) > 0:
        text += ", misses the MABD"
    return text


def headline(signals: list[Signal], st: TrackingStatus | None) -> str:
    lead = min(signals, key=lambda s: SIGNAL_ORDER.index(s.code)) if signals else None
    cons = _consequence(st) if st else ""
    if lead and lead.code == "WEATHER_TRAFFIC" and st and st.status == "ON_TIME":
        return f"On time — {cons}; {lead.text[0].lower()}{lead.text[1:]}."
    if lead and lead.code in ("PROJECTED_LATE", "MISSES_MABD", "ARRIVED_LATE", "TIGHT", "RISK"):
        # these already say what happens to the delivery, or (RISK) aren't about the delivery time
        cons = ", misses the MABD" if lead.code == "PROJECTED_LATE" and "MABD" in cons else ""
        return f"{lead.text}{cons}."
    if lead:
        return f"{lead.text} — {cons}." if cons else f"{lead.text}."
    if st:
        return f"{st.status.replace('_', ' ').capitalize()}" + (f" — {cons}." if cons else ".")
    return "No issues."


def _phase(stage: str | None, record_type: str, signals: list[Signal], risks: list[RiskItem]) -> Phase:
    if record_type == "quote":
        return "ADMIN"
    tracking_issue = any(s.code != "RISK" for s in signals)
    if stage == "DELIVERED" or (risks and not tracking_issue and all(r.kind in ADMIN_KINDS for r in risks)):
        return "ADMIN"
    return "PRE_PICKUP" if stage in PRE_PICKUP or stage is None else "IN_MOTION"


def _urgency(signals: list[Signal], st: TrackingStatus | None) -> Urgency:
    if any(s.act for s in signals) or (st and st.status == "LATE"):
        return "ACT_NOW"
    # A forecast slowdown that still leaves the load on time is context, not something to watch.
    watch = [s for s in signals if not (s.code == "WEATHER_TRAFFIC" and st and st.status == "ON_TIME")]
    return "WATCH" if watch or (st and st.status == "AT_RISK") else "FINE"


def _score(urgency: Urgency, st: TrackingStatus | None, risks: list[RiskItem]) -> float:
    """Urgency first; then how late (up to 48 h counts), missing the MABD, and the risk rules' own score.
    Delay cost will join this once it exists."""
    s = URGENCY_BASE[urgency]
    if st:
        s += min(max(st.minutes_late or 0, 0) / 60, 48) * 10
        s += 200 if (st.minutes_late_mabd or 0) > 0 else 0
    s += max((r.score for r in risks), default=0)
    return round(s, 1)


def _lane(ship: dict, risks: list[RiskItem]) -> str:
    if ship.get("shipper_city"):
        return (f"{ship.get('shipper_city')}, {ship.get('shipper_state')} → "
                f"{ship.get('consignee_city')}, {ship.get('consignee_state')}").replace(", None", "")
    lane = next((str(r.evidence["lane"]) for r in risks if r.evidence.get("lane")), "")
    return re.sub(r",(?=\S)", ", ", lane)    # rule evidence writes "Pittsburgh,PA"


def view(record_id: str, *, record_type: Literal["shipment", "quote"] = "shipment", shipment: dict | None = None,
         status: TrackingStatus | None = None, outlook: DelayOutlook | None = None,
         risks: list[RiskItem] | None = None) -> LoadView:
    ship, risks = shipment or {}, risks or []
    # Delivered loads aren't in the open-shipments list, so fall back to the status a risk rule saw.
    stage = (ship.get("held_from") if ship.get("status") == "ON_HOLD" else ship.get("status")) \
        or (status.stage if status else None) \
        or next((r.evidence["status"] for r in risks if r.record_type == "shipment" and r.evidence.get("status")), None)
    signals = (tracking_signals(status, outlook) if status else []) + risk_signals(risks, stage)
    signals.sort(key=lambda s: SIGNAL_ORDER.index(s.code))   # stable: risk signals keep their score order
    urgency = _urgency(signals, status)
    return LoadView(
        record_id=record_id, record_type=record_type, lane=_lane(ship, risks),
        carrier_id=ship.get("carrier_id"), customer_id=ship.get("customer_id"), stage=stage,
        phase=_phase(stage, record_type, signals, risks), urgency=urgency, score=_score(urgency, status, risks),
        headline=headline(signals, status), signals=signals, actions=actions_for(signals, risks, stage),
        tracking=status, outlook=outlook, risks=risks)


def build(shipments: dict[str, dict], statuses: dict[str, TrackingStatus], risks: list[RiskItem],
          outlooks: dict[str, DelayOutlook] | None = None) -> list[LoadView]:
    """One view per tracked load and per load or quote with a risk item, most urgent first.
    `statuses` should already have the weather/traffic outlook applied (outlook.apply), as the UI does."""
    outlooks = outlooks or {}
    by_record: dict[tuple[str, str], list[RiskItem]] = {}
    for r in risks:
        by_record.setdefault((r.record_type, r.record_id), []).append(r)
    keys = {("shipment", sid) for sid in statuses} | set(by_record)
    views = [view(rid, record_type=rtype, shipment=shipments.get(rid) if rtype == "shipment" else None,
                  status=statuses.get(rid) if rtype == "shipment" else None,
                  outlook=outlooks.get(rid) if rtype == "shipment" else None,
                  risks=by_record.get((rtype, rid), []))
             for rtype, rid in keys]
    return sorted(views, key=lambda v: (-v.score, v.record_id))


if __name__ == "__main__":
    import json

    from . import risk_rules
    from .client import SNAPSHOT_DIR
    from .risk_rules import RiskContext

    read = lambda name: json.loads((SNAPSHOT_DIR / f"{name}.json").read_text(encoding="utf-8"))  # noqa: E731
    ships = {str(s["shipment_id"]): s for s in read("open_shipments")}
    facs = {f["facility_id"]: f for f in read("facilities")}
    tr = tracking.Tracker(list(ships.values()), facs, read("shipment_events"))
    ctx = RiskContext(today=tr.as_of.date(), shipments=list(ships.values()), tracking_queue=read("tracking_queue"),
                      vendors={v["vendor_id"]: v for v in read("vendors")}, facilities=facs,
                      customers={c["customer_id"]: c for c in read("customers")},
                      quotes=read("quotes_quoted") + read("quotes_sent"), tracking=tr.statuses)
    for v in build(ships, tr.statuses, risk_rules.run_all(ctx)):
        nxt = v.next_action.label if v.next_action else "—"
        print(f"{v.urgency:8} {v.phase:10} {v.record_id}  {v.headline}\n{'':20}→ {nxt}")
