"""Requirements, price list, checklist and pricing. Pure code, no database,
no model. Given the same inputs, `price_quote` returns the same lines and
totals every time; `inputs_hash` identifies those inputs.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel, ConfigDict, Field

CENT = Decimal("0.01")
INT_FIELDS = ("bedrooms", "bathrooms", "carpet_rooms", "windows")
FIELD_LABELS = {
    "area_sqm": "Floor area (m2)", "bedrooms": "Bedrooms", "bathrooms": "Bathrooms",
    "carpet_rooms": "Carpeted rooms", "windows": "Windows", "frequency": "Frequency",
    "weekend": "Weekend visit", "promo_code": "Promo code", "concession": "Concession",
    "customer_name": "Customer name", "customer_email": "Customer email",
}
FREQUENCIES = ("one_off", "weekly", "fortnightly", "monthly")
QUESTIONS = {
    "area_sqm": "What is the floor area to be cleaned, in square metres?",
    "bedrooms": "How many bedrooms are there?",
    "bathrooms": "How many bathrooms are there?",
    "carpet_rooms": "How many carpeted rooms need steam cleaning?",
    "windows": "How many windows need cleaning?",
    "frequency": "Is this a one-off clean or a regular service, and how often?",
}


def cents(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


class Model(BaseModel):
    model_config = ConfigDict(frozen=True)


# --- requirements ------------------------------------------------------------

class Value(Model):
    """One fact about the job and where it came from. For `extracted` values
    `quote` is the exact text in the enquiry at [start, end)."""
    value: str
    source: str = "extracted"           # extracted | form | operator
    quote: str | None = None
    start: int | None = None
    end: int | None = None
    by: str | None = None               # operator name, when source is operator


class Requirements(Model):
    services: dict[str, Value] = Field(default_factory=dict)       # service code -> evidence
    unsupported: dict[str, Value] = Field(default_factory=dict)    # label -> evidence
    fields: dict[str, Value] = Field(default_factory=dict)
    conflicts: dict[str, list[Value]] = Field(default_factory=dict)  # field -> differing values
    discount_request: Value | None = None
    acknowledged: list[str] = Field(default_factory=list)          # unsupported labels the operator accepted
    discount_decision: str | None = None    # None (undecided) | "declined" | granted percent, e.g. "5"
    rejected_evidence: list[str] = Field(default_factory=list)     # values dropped because the quote was not in the text

    def number(self, field: str) -> Decimal | None:
        v = self.fields.get(field)
        if v is None or field in self.conflicts:
            return None
        try:
            return Decimal(v.value)
        except ArithmeticError:
            return None

    def text(self, field: str) -> str | None:
        v = self.fields.get(field)
        return None if v is None or field in self.conflicts else v.value


# --- price list --------------------------------------------------------------

class Service(Model):
    code: str
    name: str
    base: Decimal = Decimal("0")
    rates: dict[str, Decimal] = Field(default_factory=dict)   # requirement field -> price per unit
    unit_labels: dict[str, str] = Field(default_factory=dict)
    minimum: Decimal | None = None
    requires: list[str] = Field(default_factory=list)


class Discount(Model):
    code: str
    label: str
    percent: Decimal
    kind: str                           # frequency | promo | concession
    when: str                           # the frequency, promo code or concession it applies to
    valid_until: date | None = None


class PriceList(Model):
    business_name: str
    currency: str = "AUD"
    gst_rate: Decimal = Decimal("0.10")
    weekend_surcharge_rate: Decimal = Decimal("0.20")
    quote_valid_days: int = 30
    services: list[Service]
    discounts: list[Discount] = Field(default_factory=list)

    def service(self, code: str) -> Service | None:
        return next((s for s in self.services if s.code == code), None)


# --- operator adjustments ------------------------------------------------------

class Override(Model):
    quantity: Decimal | None = None
    unit_price: Decimal | None = None


class CustomLine(Model):
    description: str
    amount: Decimal


class Adjustments(Model):
    overrides: dict[str, Override] = Field(default_factory=dict)   # line code -> override
    custom: list[CustomLine] = Field(default_factory=list)


# --- results -----------------------------------------------------------------

class ChecklistItem(Model):
    code: str               # NO_SERVICE | UNSUPPORTED_SERVICE | CONTRADICTION | MISSING_INFO | DISCOUNT_REQUEST | PROMO_INVALID | EVIDENCE_REJECTED
    key: str                # field, label or "-"
    question: str
    blocking: bool = True
    resolve: str = "provide"            # provide | acknowledge | decide | none

    @property
    def fingerprint(self) -> str:
        return f"{self.code}:{self.key}"


class Line(Model):
    code: str
    description: str
    quantity: Decimal
    unit_price: Decimal
    amount: Decimal
    kind: str = "service"               # service | custom | surcharge | discount
    manual: bool = False


class Priced(Model):
    lines: list[Line]
    subtotal: Decimal
    surcharge: Decimal
    discount: Decimal
    total_ex_gst: Decimal
    gst: Decimal
    total: Decimal
    checklist: list[ChecklistItem]
    excluded: list[str]                 # unsupported services the operator agreed to leave out

    @property
    def blockers(self) -> list[ChecklistItem]:
        return [c for c in self.checklist if c.blocking]

    @property
    def can_approve(self) -> bool:
        return not self.blockers and any(ln.kind in ("service", "custom") for ln in self.lines) and self.total > 0


def _missing(req: Requirements, service: Service) -> list[str]:
    return [f for f in service.requires if f not in req.fields and f not in req.conflicts]


def _eligible_discounts(req: Requirements, pl: PriceList, priced_on: date) -> tuple[list[Discount], list[ChecklistItem]]:
    found, notes = [], []
    promo = req.text("promo_code")
    for d in pl.discounts:
        if d.kind == "frequency" and req.text("frequency") == d.when:
            found.append(d)
        elif d.kind == "concession" and req.text("concession") == d.when:
            found.append(d)
        elif d.kind == "promo" and promo and promo.upper() == d.when.upper():
            if d.valid_until and priced_on > d.valid_until:
                notes.append(ChecklistItem(code="PROMO_INVALID", key=promo, blocking=False, resolve="none",
                                           question=f"Promo code {promo} expired on {d.valid_until}. It was not applied."))
            else:
                found.append(d)
    if promo and not any(d.kind == "promo" and d.when.upper() == promo.upper() for d in pl.discounts):
        notes.append(ChecklistItem(code="PROMO_INVALID", key=promo, blocking=False, resolve="none",
                                   question=f"Promo code {promo} is not in the price list. It was not applied."))
    return found, notes


def build_checklist(req: Requirements, pl: PriceList, priced_on: date) -> list[ChecklistItem]:
    """What must be settled before a final quote. Order is fixed."""
    items = []
    known = [c for c in req.services if pl.service(c)]
    for label in req.unsupported:
        if label not in req.acknowledged:
            items.append(ChecklistItem(
                code="UNSUPPORTED_SERVICE", key=label, resolve="acknowledge",
                question=f"The customer asked for {label.lower()}, which is not on the price list. "
                         f"Confirm it will be left out of the quote and the customer told."))
    if not known:
        items.append(ChecklistItem(code="NO_SERVICE", key="-", resolve="none",
                                   question="No service on the price list was recognised in this enquiry. "
                                            "Nothing can be quoted."))
    for fld in req.conflicts:
        options = " or ".join(v.value for v in req.conflicts[fld])
        items.append(ChecklistItem(code="CONTRADICTION", key=fld,
                                   question=f"The enquiry gives two answers for {FIELD_LABELS.get(fld, fld).lower()}: "
                                            f"{options}. Which is right?"))
    asked = set()
    for code in known:
        for fld in _missing(req, pl.service(code)):
            if fld not in asked:
                asked.add(fld)
                items.append(ChecklistItem(code="MISSING_INFO", key=fld, question=QUESTIONS.get(fld, f"What is the {fld}?")))
    if req.discount_request and req.discount_decision is None:
        items.append(ChecklistItem(
            code="DISCOUNT_REQUEST", key="-", resolve="decide",
            question=f'The customer asked for a discount ("{req.discount_request.quote or req.discount_request.value}") '
                     f"that is not on the price list. Decline it or grant a percentage."))
    items += _eligible_discounts(req, pl, priced_on)[1]
    for dropped in req.rejected_evidence:
        items.append(ChecklistItem(code="EVIDENCE_REJECTED", key=dropped, blocking=False, resolve="none",
                                   question=f"A value for {dropped} was discarded because its quoted source "
                                            f"is not in the enquiry text."))
    return items


def price_quote(req: Requirements, pl: PriceList, adj: Adjustments, priced_on: date) -> Priced:
    """Price every service whose required information is present."""
    lines: list[Line] = []

    def add(code: str, description: str, quantity: Decimal, unit_price: Decimal, kind: str = "service") -> None:
        o = adj.overrides.get(code)
        manual = False
        if o and (o.quantity is not None or o.unit_price is not None):
            quantity = o.quantity if o.quantity is not None else quantity
            unit_price = o.unit_price if o.unit_price is not None else unit_price
            manual = True
        lines.append(Line(code=code, description=description, quantity=quantity, unit_price=unit_price,
                          amount=cents(quantity * unit_price), kind=kind, manual=manual))

    for service in pl.services:
        if service.code not in req.services or _missing(req, service):
            continue
        if any(f in req.conflicts for f in service.requires):
            continue
        start = len(lines)
        if service.base:
            add(f"{service.code}.base", service.name, Decimal("1"), service.base)
        for fld, rate in service.rates.items():
            qty = req.number(fld)
            if qty is None:
                continue
            unit = service.unit_labels.get(fld, FIELD_LABELS.get(fld, fld).lower())
            label = f"{service.name}: {unit}" if service.base else f"{service.name} ({unit})"
            add(f"{service.code}.{fld}", label, qty, rate)
        if not service.base and not service.rates:
            add(f"{service.code}.base", service.name, Decimal("1"), Decimal("0"))
        so_far = sum((ln.amount for ln in lines[start:]), Decimal("0"))
        if service.minimum is not None and so_far < service.minimum and not any(ln.manual for ln in lines[start:]):
            add(f"{service.code}.minimum", f"{service.name}: minimum charge of {service.minimum:.2f} applies",
                Decimal("1"), service.minimum - so_far)

    for n, c in enumerate(adj.custom, 1):
        lines.append(Line(code=f"custom.{n}", description=c.description, quantity=Decimal("1"),
                          unit_price=c.amount, amount=cents(c.amount), kind="custom", manual=True))

    subtotal = sum((ln.amount for ln in lines), Decimal("0"))
    surcharge = Decimal("0")
    if req.text("weekend") == "yes" and subtotal > 0:
        surcharge = cents(subtotal * pl.weekend_surcharge_rate)
        lines.append(Line(code="surcharge.weekend",
                          description=f"Weekend surcharge {pl.weekend_surcharge_rate * 100:.0f}%",
                          quantity=Decimal("1"), unit_price=surcharge, amount=surcharge, kind="surcharge"))

    discount = Decimal("0")
    if subtotal > 0:
        percent, label = None, ""
        if req.discount_decision not in (None, "declined"):
            percent, label, manual = Decimal(req.discount_decision), "Discount granted by operator", True
        else:
            eligible, _ = _eligible_discounts(req, pl, priced_on)
            if eligible:
                # Discounts do not stack: the largest one applies. Ties go to price-list order.
                best = max(eligible, key=lambda d: d.percent)
                percent, label, manual = best.percent, best.label, False
        if percent:
            discount = cents(subtotal * percent / 100)
            lines.append(Line(code="discount", description=f"{label} ({percent.normalize():f}%)",
                              quantity=Decimal("1"), unit_price=-discount, amount=-discount,
                              kind="discount", manual=manual))

    total_ex = subtotal + surcharge - discount
    gst = cents(total_ex * pl.gst_rate)
    return Priced(lines=lines, subtotal=subtotal, surcharge=surcharge, discount=discount,
                  total_ex_gst=total_ex, gst=gst, total=total_ex + gst,
                  checklist=build_checklist(req, pl, priced_on),
                  excluded=[label for label in req.unsupported if label in req.acknowledged])


def inputs_hash(req: Requirements, price_list_version: int, adj: Adjustments, priced_on: date) -> str:
    """Identify everything a quote's numbers depend on. Evidence positions
    and who typed a value are left out: they do not change a price."""
    payload = {
        "price_list_version": price_list_version,
        "priced_on": priced_on.isoformat(),
        "services": sorted(req.services),
        "unsupported": sorted(req.unsupported),
        "fields": {k: v.value for k, v in sorted(req.fields.items())},
        "conflicts": {k: sorted(v.value for v in vs) for k, vs in sorted(req.conflicts.items())},
        "discount_request": req.discount_request.value if req.discount_request else None,
        "acknowledged": sorted(req.acknowledged),
        "discount_decision": req.discount_decision,
        "adjustments": json.loads(adj.model_dump_json()),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
