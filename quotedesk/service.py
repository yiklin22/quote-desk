"""Use cases. Each takes a Session and commits its own work.

There is deliberately no function here that sends anything to a customer.
The outputs are a PDF and a text draft for the operator to send themselves.
"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .domain import (FIELD_LABELS, Adjustments, CustomLine, Override, PriceList, Priced, Requirements, Value,
                     inputs_hash, price_quote)
from .drafting import draft_reply
from .extraction import Extractor, RuleExtractor, _valid
from .models import Approval, Enquiry, Event, PriceListVersion, Quote, QuoteLine
from .pdf import render_quote_pdf

MAX_ENQUIRY_CHARS = 20_000
EDITABLE_FIELDS = [f for f in FIELD_LABELS]
DEFAULT_PRICE_LIST = Path(__file__).parent / "pricelist_v1.json"


class QuoteError(ValueError):
    """The request is invalid, or not allowed in the quote's current state."""


# --- price lists -------------------------------------------------------------

def current_price_list(session: Session) -> PriceListVersion:
    row = session.scalar(select(PriceListVersion).order_by(PriceListVersion.version.desc()).limit(1))
    if row is None:
        row = PriceListVersion(version=1, data=json.loads(DEFAULT_PRICE_LIST.read_text()), created_by="system",
                               note="Placeholder prices invented for the demo. Replace with the operator's own.")
        session.add(row)
        session.commit()
    return row


def publish_price_list(session: Session, raw_json: str, actor: str, note: str = "") -> PriceListVersion:
    """Validate and store a new version. Existing quotes are not touched."""
    try:
        parsed = PriceList.model_validate_json(raw_json)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first["loc"])
        raise QuoteError(f"The price list is not valid: {where}: {first['msg']}") from None
    codes = [s.code for s in parsed.services]
    if not codes or len(set(codes)) != len(codes):
        raise QuoteError("The price list needs at least one service, and service codes must be unique.")
    latest = current_price_list(session)
    row = PriceListVersion(version=latest.version + 1, data=json.loads(parsed.model_dump_json()),
                           created_by=actor, note=note.strip() or None)
    session.add(row)
    session.commit()
    return row


def as_price_list(row: PriceListVersion) -> PriceList:
    return PriceList.model_validate(row.data)


# --- enquiry state -----------------------------------------------------------

def get_enquiry(session: Session, enquiry_id: int) -> Enquiry:
    enq = session.get(Enquiry, enquiry_id)
    if enq is None:
        raise LookupError(f"No enquiry {enquiry_id}")
    return enq


def requirements(enq: Enquiry) -> Requirements:
    return Requirements.model_validate(enq.requirements)


def adjustments(quote: Quote) -> Adjustments:
    return Adjustments.model_validate(quote.adjustments or {})


def priced(enq: Enquiry) -> Priced:
    q = enq.quote
    return price_quote(requirements(enq), as_price_list(q.price_list), adjustments(q), q.priced_on)


def is_approved(quote: Quote) -> bool:
    """Approved means the newest approval is for the current revision."""
    return bool(quote.approvals) and quote.approvals[-1].revision == quote.revision


def status(enq: Enquiry) -> str:
    result = priced(enq)
    if is_approved(enq.quote):
        return "approved"
    if any(c.code == "NO_SERVICE" for c in result.checklist):
        return "cannot_quote"
    return "needs_info" if result.blockers else "ready"


def _log(enq: Enquiry, actor: str, action: str, detail: str | None = None) -> None:
    enq.events.append(Event(actor=actor, action=action, detail=detail))


def _refresh(session: Session, enq: Enquiry, req: Requirements, adj: Adjustments, actor: str,
             price_list: PriceListVersion | None = None, today: date | None = None) -> Priced:
    """Store the requirements, recompute the quote, and bump the revision if
    anything a price depends on changed."""
    enq.requirements = json.loads(req.model_dump_json())
    quote = enq.quote
    if quote is None:
        quote = Quote(price_list=price_list or current_price_list(session), priced_on=today or date.today(),
                      revision=0, inputs_hash="", adjustments={},
                      subtotal=0, surcharge=0, discount=0, gst=0, total=0)
        enq.quote = quote
    elif price_list is not None:
        quote.price_list = price_list
    quote.adjustments = json.loads(adj.model_dump_json())
    pl = as_price_list(quote.price_list)
    result = price_quote(req, pl, adj, quote.priced_on)
    digest = inputs_hash(req, quote.price_list.version, adj, quote.priced_on)
    if digest != quote.inputs_hash:
        was_approved = bool(quote.approvals) and quote.approvals[-1].revision == quote.revision
        quote.revision += 1
        quote.inputs_hash = digest
        if was_approved:
            _log(enq, "system", "approval_invalidated",
                 f"Revision {quote.revision - 1} was approved; revision {quote.revision} is not.")
    quote.lines.clear()
    session.flush()
    for n, ln in enumerate(result.lines, 1):
        quote.lines.append(QuoteLine(position=n, **ln.model_dump()))
    quote.subtotal, quote.surcharge, quote.discount = result.subtotal, result.surcharge, result.discount
    quote.gst, quote.total = result.gst, result.total
    session.commit()
    return result


def create_enquiry(session: Session, text: str, actor: str, form: dict[str, str] | None = None,
                   extractor: Extractor | None = None, today: date | None = None) -> Enquiry:
    """Record an enquiry from pasted text and/or form fields and price it.
    Form values win over values read from the text."""
    text = (text or "").strip()
    form = {k: v.strip() for k, v in (form or {}).items() if v and v.strip()}
    if not text and not form:
        raise QuoteError("Paste the enquiry or fill in the form.")
    if len(text) > MAX_ENQUIRY_CHARS:
        raise QuoteError(f"The enquiry is longer than {MAX_ENQUIRY_CHARS:,} characters.")
    extractor = extractor or RuleExtractor()
    req = extractor.extract(text) if text else Requirements()
    fields, services, conflicts = dict(req.fields), dict(req.services), dict(req.conflicts)
    pl_row = current_price_list(session)
    for code in [c.strip() for c in form.pop("service", "").split(",") if c.strip()]:
        if as_price_list(pl_row).service(code) is None:
            raise QuoteError(f"{code} is not a service on the price list.")
        services[code] = Value(value=code, source="form")
    for name, raw in form.items():
        value = _clean_field(name, raw)
        fields[name] = Value(value=value, source="form")
        conflicts.pop(name, None)
    req = req.model_copy(update={"fields": fields, "services": services, "conflicts": conflicts})
    enq = Enquiry(text=text, channel="email" if text else "form", extractor=type(extractor).__name__,
                  requirements={})
    session.add(enq)
    _log(enq, actor, "enquiry_received", f"{len(text)} characters, {len(form)} form value(s)")
    _refresh(session, enq, req, Adjustments(), actor, price_list=pl_row, today=today)
    return enq


# --- operator actions --------------------------------------------------------

def _clean_field(name: str, raw: str) -> str:
    if name not in FIELD_LABELS:
        raise QuoteError(f"Unknown field {name!r}.")
    value = raw.strip()
    if name == "weekend":
        value = {"yes": "yes", "true": "yes", "on": "yes", "no": "no", "false": "no"}.get(value.lower(), value)
        if value not in ("yes", "no"):
            raise QuoteError("Weekend visit must be yes or no.")
    elif name == "frequency":
        value = value.lower().replace("-", "_").replace(" ", "_")
    elif name == "promo_code":
        value = value.upper()
    if not _valid(name, Value(value=value)):
        raise QuoteError(f"{FIELD_LABELS[name]}: {raw!r} is not a valid value.")
    return value


def set_field(session: Session, enquiry_id: int, name: str, raw: str, actor: str) -> Priced:
    """Set, correct or clear one requirement. Settles a contradiction on that field."""
    enq = get_enquiry(session, enquiry_id)
    req = requirements(enq)
    fields, conflicts = dict(req.fields), dict(req.conflicts)
    old = fields.get(name)
    if raw.strip() == "":
        if name not in FIELD_LABELS:
            raise QuoteError(f"Unknown field {name!r}.")
        fields.pop(name, None)
        new_text = "(cleared)"
    else:
        value = _clean_field(name, raw)
        if old is not None and old.value == value and name not in conflicts:
            return priced(enq)
        fields[name] = Value(value=value, source="operator", by=actor)
        new_text = value
    conflicts.pop(name, None)
    _log(enq, actor, "requirement_set", f"{name}: {old.value if old else '(empty)'} -> {new_text}")
    return _refresh(session, enq, req.model_copy(update={"fields": fields, "conflicts": conflicts}),
                    adjustments(enq.quote), actor)


def set_service(session: Session, enquiry_id: int, code: str, include: bool, actor: str) -> Priced:
    enq = get_enquiry(session, enquiry_id)
    req = requirements(enq)
    if as_price_list(enq.quote.price_list).service(code) is None:
        raise QuoteError(f"{code} is not a service on this quote's price list.")
    services = dict(req.services)
    if include:
        services.setdefault(code, Value(value=code, source="operator", by=actor))
    else:
        services.pop(code, None)
    _log(enq, actor, "service_added" if include else "service_removed", code)
    return _refresh(session, enq, req.model_copy(update={"services": services}), adjustments(enq.quote), actor)


def acknowledge_unsupported(session: Session, enquiry_id: int, label: str, actor: str) -> Priced:
    enq = get_enquiry(session, enquiry_id)
    req = requirements(enq)
    if label not in req.unsupported:
        raise QuoteError(f"{label!r} is not on this enquiry's list of unsupported services.")
    if label in req.acknowledged:
        return priced(enq)
    _log(enq, actor, "exclusion_acknowledged", label)
    return _refresh(session, enq, req.model_copy(update={"acknowledged": [*req.acknowledged, label]}),
                    adjustments(enq.quote), actor)


def decide_discount(session: Session, enquiry_id: int, decision: str, actor: str) -> Priced:
    """`decision` is "declined" or a percentage between 0 and 50."""
    enq = get_enquiry(session, enquiry_id)
    req = requirements(enq)
    decision = decision.strip().rstrip("%").strip()
    if decision != "declined":
        try:
            pct = Decimal(decision)
        except InvalidOperation:
            raise QuoteError("Enter the discount as a percentage, for example 5.") from None
        if not (Decimal("0") < pct <= Decimal("50")):
            raise QuoteError("A granted discount must be more than 0% and at most 50%.")
        decision = f"{pct.normalize():f}"
    _log(enq, actor, "discount_decided", decision)
    return _refresh(session, enq, req.model_copy(update={"discount_decision": decision}),
                    adjustments(enq.quote), actor)


def _decimal(raw: str, what: str, allow_negative: bool = False) -> Decimal:
    try:
        value = Decimal(raw.replace("$", "").replace(",", "").strip())
    except InvalidOperation:
        raise QuoteError(f"{what}: {raw!r} is not a number.") from None
    if not value.is_finite() or (value < 0 and not allow_negative) or abs(value) > Decimal("1000000"):
        raise QuoteError(f"{what}: {raw!r} is out of range.")
    return value


def set_override(session: Session, enquiry_id: int, line_code: str, quantity: str, unit_price: str,
                 actor: str) -> Priced:
    """Change the quantity or unit price of a computed line. Empty values remove the override."""
    enq = get_enquiry(session, enquiry_id)
    adj = adjustments(enq.quote)
    current = {ln.code: ln for ln in priced(enq).lines if ln.kind == "service"}
    if line_code not in current:
        raise QuoteError("That line cannot be edited. Reload the page.")
    overrides = dict(adj.overrides)
    if not quantity.strip() and not unit_price.strip():
        overrides.pop(line_code, None)
        detail = f"{line_code}: override removed"
    else:
        o = Override(quantity=_decimal(quantity, "Quantity") if quantity.strip() else None,
                     unit_price=_decimal(unit_price, "Unit price") if unit_price.strip() else None)
        overrides[line_code] = o
        detail = f"{line_code}: quantity {o.quantity if o.quantity is not None else 'unchanged'}, " \
                 f"unit price {o.unit_price if o.unit_price is not None else 'unchanged'}"
    _log(enq, actor, "line_edited", detail)
    return _refresh(session, enq, requirements(enq), adj.model_copy(update={"overrides": overrides}), actor)


def add_custom_line(session: Session, enquiry_id: int, description: str, amount: str, actor: str) -> Priced:
    enq = get_enquiry(session, enquiry_id)
    description = description.strip()
    if not description:
        raise QuoteError("Describe the extra line.")
    line = CustomLine(description=description[:200], amount=_decimal(amount, "Amount", allow_negative=True))
    adj = adjustments(enq.quote)
    _log(enq, actor, "line_added", f"{line.description}: {line.amount}")
    return _refresh(session, enq, requirements(enq), adj.model_copy(update={"custom": [*adj.custom, line]}), actor)


def remove_custom_line(session: Session, enquiry_id: int, index: int, actor: str) -> Priced:
    enq = get_enquiry(session, enquiry_id)
    adj = adjustments(enq.quote)
    if not 0 <= index < len(adj.custom):
        raise QuoteError("That line no longer exists. Reload the page.")
    custom = list(adj.custom)
    removed = custom.pop(index)
    _log(enq, actor, "line_removed", removed.description)
    return _refresh(session, enq, requirements(enq), adj.model_copy(update={"custom": custom}), actor)


def reprice_with_current_list(session: Session, enquiry_id: int, actor: str) -> Priced:
    enq = get_enquiry(session, enquiry_id)
    latest = current_price_list(session)
    if latest.id == enq.quote.price_list_id:
        return priced(enq)
    _log(enq, actor, "repriced", f"price list version {enq.quote.price_list.version} -> {latest.version}")
    return _refresh(session, enq, requirements(enq), adjustments(enq.quote), actor, price_list=latest)


def approve(session: Session, enquiry_id: int, actor: str) -> Approval:
    enq = get_enquiry(session, enquiry_id)
    quote, result = enq.quote, priced(enq)
    if is_approved(quote):
        raise QuoteError("This revision is already approved.")
    if result.blockers:
        raise QuoteError(f"{len(result.blockers)} checklist item(s) must be settled before approval.")
    if not result.can_approve:
        raise QuoteError("There is nothing to approve: the quote has no priced lines.")
    approval = Approval(revision=quote.revision, inputs_hash=quote.inputs_hash, total=result.total,
                        approved_by=actor)
    quote.approvals.append(approval)
    _log(enq, actor, "quote_approved", f"revision {quote.revision}, total {result.total}")
    session.commit()
    return approval


def quote_number(quote: Quote) -> str:
    return f"Q{quote.enquiry_id:05d}-r{quote.revision}"


def quote_pdf(session: Session, enquiry_id: int, actor: str) -> tuple[str, bytes]:
    """The PDF exists only for an approved revision."""
    enq = get_enquiry(session, enquiry_id)
    quote = enq.quote
    if not is_approved(quote):
        raise QuoteError("The quote must be approved before a PDF can be produced.")
    number = quote_number(quote)
    data = render_quote_pdf(number, as_price_list(quote.price_list), quote.price_list.version,
                            requirements(enq), priced(enq), quote.priced_on, quote.approvals[-1].approved_by)
    _log(enq, actor, "pdf_downloaded", number)
    session.commit()
    return number, data


def reply_draft(enq: Enquiry) -> str:
    return draft_reply(as_price_list(enq.quote.price_list), requirements(enq), priced(enq),
                       is_approved(enq.quote))


def counts(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(Enquiry)) or 0
