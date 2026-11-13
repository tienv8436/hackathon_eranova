"""Realistic fake data so the UI can be finished before the playbooks are. Owner: UI lead.

The load watch and tracking no longer use fake data: in fake mode they read the snapshots
(see ui/state.apply_data_source), so both tabs show the same loads."""

from __future__ import annotations

from datetime import date, timedelta

from meridian.models import Check, FreightLine, Location, QuoteProposal, QuoteRequest


def fake_proposal(has_po: bool = False) -> QuoteProposal:
    req = QuoteRequest(
        customer_name="Northstar", mode="TL", equipment_type="V",
        origin=Location(city="Chicago", state="IL"), destination=Location(city="Dallas", state="TX"),
        pickup_date=date.today() + timedelta(days=3),
        freight_lines=[FreightLine(pieces=22, weight_lb=38000, description="Packaged goods")],
        references={"PO": "45812"} if has_po else {},
    )
    checks = [
        Check(name="Customer found", passed=True, detail="NORTHSTAR (Northstar Foods Inc.)"),
        Check(name="Required reference: PO", passed=has_po, detail="45812" if has_po else "NORTHSTAR requires a PO to quote"),
        Check(name="Freight details complete", passed=True, detail="ok"),
        Check(name="Margin floor", passed=True, detail="Linehaul GP 18.0% ≥ floor 18%; blended 15.4% because TL fuel passes through at cost"),
    ]
    buy = [{"charge_code": "FRT", "description": "Linehaul", "rate_basis": "FLAT", "quantity": 1, "rate": 2350.0, "amount": 2350.0},
           {"charge_code": "FSC", "description": "Fuel surcharge", "rate_basis": "PER_MILE", "quantity": 925, "rate": 0.52, "amount": 481.0}]
    sell = [{"charge_code": "FRT", "description": "Linehaul", "rate_basis": "FLAT", "quantity": 1, "rate": 2865.85, "amount": 2865.85},
            {"charge_code": "FSC", "description": "Fuel surcharge", "rate_basis": "PER_MILE", "quantity": 925, "rate": 0.52, "amount": 481.0}]
    missing = [] if has_po else ["PO number"]
    reply = ("Thanks for the request. Our all-in rate for Chicago, IL → Dallas, TX is $3,346.85, valid for 7 days.\n\nMeridian Logistics"
             if has_po else
             "Thanks for the request. Before we can confirm a rate for Chicago, IL → Dallas, TX, could you send the PO number for this load?\n\nMeridian Logistics")
    return QuoteProposal(request=req, customer_id="NORTHSTAR", missing_fields=missing, checks=checks,
                         buy_total=2831.0, sell_total=3346.85, gp_pct=15.4, margin_pct_used=18.0,
                         buy_lines=buy, sell_lines=sell, rate_id="RE-FAKE-0001", draft_reply=reply,
                         ready_to_create=has_po)
