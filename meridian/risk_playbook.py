"""At-risk load watch playbook. Owner: agent & evals lead.

scan()            -> ranked list[RiskItem], top N explained and drafted
act(item, action) -> result dict (adds a note or records a check call; dry-run unless DRY_RUN=0)
"""

from __future__ import annotations

from datetime import date
from typing import Callable, Literal

from . import client, llm, risk_rules
from .models import RiskItem, TrackingStatus
from .risk_rules import RiskContext

StepFn = Callable[[str], None]


def _noop(_: str) -> None:
    pass


def gather(today: date | None = None, on_step: StepFn = _noop,
           tracking: dict[str, TrackingStatus] | None = None) -> RiskContext:
    """`tracking`: statuses from meridian.tracking (the simulated feed today), for the LATE_ETA rule."""
    def data(name: str, res: dict) -> list:
        """A failed read becomes an empty list, but is reported, so a partial scan never looks clean."""
        if res["ok"]:
            return res["data"]
        on_step(f"Warning: could not read {name}: {res.get('detail') or res.get('error')}")
        return []

    shipments = data("open shipments", client.list_shipments())
    on_step(f"Loaded {len(shipments)} open loads")
    on_step("Checking carriers, facilities and quotes")
    return RiskContext(
        today=today or date.today(),
        shipments=shipments,
        tracking_queue=data("tracking queue", client.tracking_queue()),
        vendors={v["vendor_id"]: v for v in data("carriers", client.list_vendors())},
        facilities={f["facility_id"]: f for f in data("facilities", client.list_facilities())},
        customers={c["customer_id"]: c for c in data("customers", client.list_customers())},
        quotes=data("quoted quotes", client.list_quotes("QUOTED")) + data("sent quotes", client.list_quotes("SENT")),
        tracking=tracking or {},
    )


def rank(items: list[RiskItem]) -> list[RiskItem]:
    return sorted(items, key=lambda it: it.score, reverse=True)


def scan(top_n: int = 10, today: date | None = None, on_step: StepFn = _noop,
         tracking: dict[str, TrackingStatus] | None = None, templated: bool = False) -> list[RiskItem]:
    """templated=True: explanations and drafts from a template instead of the model (fake-data mode)."""
    ctx = gather(today, on_step, tracking)
    if tracking:
        on_step(f"Using tracking for {len(tracking)} trucks")
    on_step("Running risk rules")
    items = rank(risk_rules.run_all(ctx))
    on_step(f"Writing explanations for the top {min(top_n, len(items))}")
    llm.explain_and_draft(items[:top_n], templated=templated)
    return items


def act(item: RiskItem, action: Literal["note", "check_call"], text: str | None = None) -> dict:
    """Log the approved explanation/draft on the shipment."""
    if item.record_type != "shipment":
        return {"ok": False, "detail": f"No write action for {item.record_type} items yet."}
    note = text or f"[Copilot] {item.title}: {item.explanation or item.suggested_action}"
    if action == "check_call":
        return client.record_check_call(item.record_id, note)
    return client.add_shipment_note(item.record_id, note)
