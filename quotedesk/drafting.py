"""Draft the words of a reply. Never the numbers.

The draft is text for the operator to read, change and send from their own
mailbox. This module cannot send anything. `amounts_ok` is the check any
drafter, including a model, must pass: every money amount in the text has
to be an amount the pricing code produced.
"""
from __future__ import annotations

import re
from decimal import Decimal

from .domain import PriceList, Priced, Requirements

AMOUNT = re.compile(r"\$\s?(\d[\d,]*(?:\.\d{1,2})?)")
CUSTOMER_QUESTIONS = ("MISSING_INFO", "CONTRADICTION")


def amounts_ok(text: str, priced: Priced) -> bool:
    allowed = {priced.subtotal, priced.surcharge, priced.discount, priced.total_ex_gst, priced.gst, priced.total}
    allowed |= {abs(ln.amount) for ln in priced.lines} | {abs(ln.unit_price) for ln in priced.lines}
    for raw in AMOUNT.findall(text):
        if Decimal(raw.replace(",", "")) not in allowed:
            return False
    return True


def draft_reply(pl: PriceList, req: Requirements, priced: Priced, approved: bool) -> str:
    name = req.text("customer_name")
    hello = f"Hi {name.split()[0]}," if name else "Hello,"
    business = pl.business_name
    not_offered = sorted(req.unsupported)
    sorry = ""
    if not_offered:
        listed = " or ".join(s.lower() for s in not_offered)
        sorry = f"I'm sorry, we don't offer {listed}, so that is not included.\n\n"

    if any(c.code == "NO_SERVICE" for c in priced.checklist):
        return (f"{hello}\n\nThank you for getting in touch with {business}.\n\n"
                f"{sorry or 'I could not tell which of our services you need. '}"
                f"If one of our cleaning services would help, tell me a little more and I will put a quote together.\n\n"
                f"Kind regards,\n{business}")

    questions = [c.question for c in priced.checklist if c.code in CUSTOMER_QUESTIONS]
    if questions:
        asked = "\n".join(f"{n}. {q}" for n, q in enumerate(questions, 1))
        return (f"{hello}\n\nThank you for your enquiry. {sorry}"
                f"Before I can give you a firm price I need to check:\n\n{asked}\n\n"
                f"Once I have that I will send the quote straight away.\n\nKind regards,\n{business}")

    if priced.blockers or not approved:
        return ("(No customer wording yet. Settle the checklist and approve the quote first; "
                "the draft reply is written from the approved figures.)")

    return (f"{hello}\n\nThank you for your enquiry. {sorry}"
            f"Your quote is attached. The total is ${priced.total:,.2f} including GST of ${priced.gst:,.2f}, "
            f"and it is valid for {pl.quote_valid_days} days.\n\n"
            f"If you would like to go ahead, reply with a day that suits and I will confirm the booking.\n\n"
            f"Kind regards,\n{business}")
