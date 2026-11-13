"""Model calls: extract, explain, draft. Owner: agent & evals lead.

The LLM does language work only. Rules, prices and audiences are decided in code.
Set MODEL=fake in .env to run everything without an API key (templated output).
"""

from __future__ import annotations

import os
from datetime import date
from functools import lru_cache

from dotenv import load_dotenv
from pydantic import BaseModel

from .client import ROOT
from .models import QuoteProposal, QuoteRequest, RiskItem

load_dotenv(ROOT / ".env")

MODEL = os.environ.get("MODEL", "anthropic:claude-sonnet-5-5")


def is_fake() -> bool:
    return MODEL == "fake"


@lru_cache(maxsize=1)
def _chat():
    from langchain.chat_models import init_chat_model
    return init_chat_model(MODEL, temperature=0)


# --------------------------------------------------------------------------- email -> QuoteRequest

EQUIPMENT_CODES = "V dry van, R reefer, F flatbed, SD step deck, PO power only, HS hotshot, SB straight box truck, SV sprinter van, VC curtain van"
ACCESSORIAL_CODES = ("APPT appointment, DET detention, GTD guaranteed delivery, HAZ hazmat, IDL inside delivery, "
                     "LGD liftgate delivery, LGP liftgate pickup, LUM lumper, RSD residential delivery, "
                     "RSP residential pickup, SEC security screening, STOP extra stop")

EXTRACT_SYSTEM = f"""You extract freight quote requests from customer emails for a freight broker.

Rules:
- Extract only what the email states. Leave a field empty when it is not stated; never guess.
- Today's date is {{today}} ({{weekday}}). Convert relative dates ("next Tuesday", "end of week") to YYYY-MM-DD.
- States are 2-letter codes. Weights in pounds, dimensions in inches (per piece).
- mode: TL for full truckload, LTL for less-than-truckload / pallets on a shared truck, EXPEDITE for hot / urgent / sprinter / time-critical.
- equipment_type codes: {EQUIPMENT_CODES}.
- accessorials are charge codes: {ACCESSORIAL_CODES}.
- references: reference numbers the customer gives, keyed by type (PO, SO, BOL, PU, ...).
- customer_name: the shipper company asking for the quote (from the signature or sender), not the broker.
- If the email asks for more than one load, extract the first and say so in notes.
"""


def extract_quote_request(email: str, today: date | None = None) -> QuoteRequest:
    today = today or date.today()
    if is_fake():
        return QuoteRequest(notes="(fake model) extraction skipped; set MODEL in .env for real extraction.")
    system = EXTRACT_SYSTEM.format(today=today.isoformat(), weekday=today.strftime("%A"))
    structured = _chat().with_structured_output(QuoteRequest)
    return structured.invoke([("system", system), ("human", email)])


# --------------------------------------------------------------------------- QuoteProposal -> reply

REPLY_SYSTEM = """You write short, professional replies from a freight broker to a customer who asked for a quote.
- If a price is given, state the all-in price in USD and that it is valid for 7 days.
- If anything is missing, ask for exactly those items, as a short list. Do not invent a price when none is given.
- No more than 120 words. Sign off as "Meridian Logistics"."""


def draft_quote_reply(proposal: QuoteProposal) -> str:
    if is_fake():
        if proposal.missing_fields:
            return "Thanks for reaching out. To finish your quote, could you send: " + ", ".join(proposal.missing_fields) + "?\n\nMeridian Logistics"
        price = f"${proposal.sell_total:,.2f}" if proposal.sell_total else "(price pending)"
        return f"Thanks for the request. Our all-in rate is {price}, valid for 7 days.\n\nMeridian Logistics"
    facts = {
        "request": proposal.request.model_dump(mode="json", exclude_none=True),
        "price_usd": proposal.sell_total,
        "missing": proposal.missing_fields,
        "failed_checks": [c.detail for c in proposal.checks if not c.passed],
    }
    return _chat().invoke([("system", REPLY_SYSTEM), ("human", str(facts))]).content


# --------------------------------------------------------------------------- RiskItems -> explanation + draft

AUDIENCE_BY_KIND = {
    "CARRIER_COMPLIANCE": "carrier",
    "MISSING_RATE_CON": "carrier",
    "UNCOVERED_SOON": "internal",
    "TRACKING_OVERDUE": "carrier",
    "MABD_RISK": "internal",
    "APPOINTMENT_NEEDED": "internal",
    "HOLD_RECONFIRM": "customer",
    "QUOTE_EXPIRING": "customer",
    "LOW_MARGIN": "internal",
    "LATE_ETA": "carrier",
}

RISK_SYSTEM = """You help a busy freight broker act on flagged loads.
For each item write:
- explanation: ONE plain-English sentence saying what is wrong and why it matters, using ONLY facts in `evidence`. Never invent facts.
- draft_message: 2-4 sentences to the given audience (customer, carrier, or internal note), ready to send after review.
Return one entry per item, with the same index."""


class _Writeup(BaseModel):
    index: int
    explanation: str
    draft_message: str


class _Writeups(BaseModel):
    items: list[_Writeup]


def explain_and_draft(items: list[RiskItem], templated: bool = False) -> list[RiskItem]:
    """Fill explanation, draft_message and draft_audience for each item in ONE model call.
    templated=True (or MODEL=fake): fill them from a template, no model call."""
    for it in items:
        it.draft_audience = AUDIENCE_BY_KIND[it.kind]
    if not items:
        return items
    if templated or is_fake():
        for it in items:
            it.explanation = f"(fake model) {it.title}: {it.suggested_action.lower()}."
            it.draft_message = f"(fake model) Draft to {it.draft_audience} about {it.record_type} {it.record_id}."
        return items
    payload = [{"index": i, "kind": it.kind, "title": it.title, "record": f"{it.record_type} {it.record_id}",
                "evidence": it.evidence, "suggested_action": it.suggested_action, "audience": it.draft_audience}
               for i, it in enumerate(items)]
    out = _chat().with_structured_output(_Writeups).invoke([("system", RISK_SYSTEM), ("human", str(payload))])
    by_index = {w.index: w for w in out.items}
    for i, it in enumerate(items):
        if w := by_index.get(i):
            it.explanation, it.draft_message = w.explanation, w.draft_message
    return items


def explain_risk(item: RiskItem) -> str:
    return explain_and_draft([item])[0].explanation


def draft_risk_message(item: RiskItem) -> tuple[str, str]:
    it = explain_and_draft([item])[0]
    return it.draft_audience, it.draft_message
