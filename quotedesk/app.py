"""Web application: enquiry -> clarification checklist -> editable estimate -> approval -> PDF."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote as urlquote

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import service
from .db import get_engine, get_session, new_session
from .domain import FIELD_LABELS, FREQUENCIES
from .models import Enquiry, PriceListVersion

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "web")
STATUS_LABELS = {"needs_info": "Needs information", "ready": "Ready for approval", "approved": "Approved",
                 "cannot_quote": "Cannot quote"}
FORM_FIELDS = ["area_sqm", "bedrooms", "bathrooms", "carpet_rooms", "windows", "frequency", "weekend",
               "promo_code", "customer_name", "customer_email"]
templates.env.globals.update(status_labels=STATUS_LABELS, field_labels=FIELD_LABELS, frequencies=FREQUENCIES)


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_engine()
    session = new_session()
    try:
        service.current_price_list(session)      # seed version 1 on first start
    finally:
        session.close()
    yield


app = FastAPI(title="Quote desk", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "web" / "static"), name="static")


def operator(request: Request) -> str:
    return (request.cookies.get("operator") or "").strip()[:80] or "operator"


def page(request: Request, name: str, **context):
    context.setdefault("flash", request.query_params.get("msg") or request.query_params.get("err"))
    context.setdefault("flash_kind", "err" if request.query_params.get("err") else "ok")
    context["operator"] = operator(request)
    return templates.TemplateResponse(request, name, context)


def back(path: str, msg: str = "", err: str = "") -> RedirectResponse:
    query = f"?msg={urlquote(msg)}" if msg else f"?err={urlquote(err)}" if err else ""
    return RedirectResponse(path + query, status_code=303)


def enquiry_or_404(session: Session, enquiry_id: int) -> Enquiry:
    enq = session.get(Enquiry, enquiry_id)
    if enq is None:
        raise HTTPException(404, "No such enquiry")
    return enq


def act(session: Session, enquiry_id: int, done: str, fn, *args) -> RedirectResponse:
    """Run one operator action and return to the enquiry with a message."""
    enquiry_or_404(session, enquiry_id)
    try:
        fn(session, enquiry_id, *args)
    except service.QuoteError as exc:
        session.rollback()
        return back(f"/enquiries/{enquiry_id}", err=str(exc))
    return back(f"/enquiries/{enquiry_id}", msg=done)


def highlighted(text: str, req) -> list[dict]:
    """Split the enquiry into plain and cited pieces for display. Where two
    citations overlap, the field value keeps its words and the service
    citation is shown on what is left of its phrase."""
    wanted = []                         # (priority, start, end, anchor, label)
    for name, v in req.fields.items():
        wanted.append((0, v, f"f-{name}", FIELD_LABELS.get(name, name)))
    for name, values in req.conflicts.items():
        wanted += [(0, v, f"f-{name}", f"{FIELD_LABELS.get(name, name)} (conflict)") for v in values]
    if req.discount_request:
        wanted.append((1, req.discount_request, "discount", "Discount request"))
    wanted += [(1, v, f"u-{n}", "Not offered") for n, v in enumerate(req.unsupported.values())]
    wanted += [(2, v, f"s-{code}", "Service") for code, v in req.services.items()]
    wanted = sorted(((prio, v.start, v.end, anchor, label) for prio, v, anchor, label in wanted
                     if v.source == "extracted" and v.start is not None), key=lambda w: (w[0], w[1], -w[2]))
    placed: list[tuple[int, int, str, str]] = []
    for _, start, end, anchor, label in wanted:
        free = [(start, end)]
        for ps, pe, *_ in placed:
            free = [(s, e) for a, b in free for s, e in ((a, min(b, ps)), (max(a, pe), b)) if s < e]
        free = [(s, e) for s, e in free if text[s:e].strip()]
        if free:
            s, e = free[0]
            s += len(text[s:e]) - len(text[s:e].lstrip())
            e -= len(text[s:e]) - len(text[s:e].rstrip())
            placed.append((s, e, anchor, label))
    pieces, pos, used = [], 0, set()
    for start, end, anchor, label in sorted(placed):
        if start > pos:
            pieces.append({"text": text[pos:start]})
        pieces.append({"text": text[start:end], "label": label, "anchor": None if anchor in used else anchor})
        used.add(anchor)
        pos = end
    if pos < len(text):
        pieces.append({"text": text[pos:]})
    return pieces


# --- pages -------------------------------------------------------------------

@app.get("/")
def index(request: Request, session: Session = Depends(get_session)):
    rows = []
    for enq in session.scalars(select(Enquiry).order_by(Enquiry.id.desc())):
        req = service.requirements(enq)
        rows.append({"enq": enq, "status": service.status(enq), "services": sorted(req.services),
                     "who": req.text("customer_name") or req.text("customer_email") or "",
                     "preview": " ".join(enq.text.split())[:90] or "(entered by form)"})
    pl = service.current_price_list(session)
    return page(request, "index.html", rows=rows, price_list=service.as_price_list(pl), form_fields=FORM_FIELDS)


@app.post("/enquiries")
async def enquiry_create(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    values = {f: str(form.get(f, "")) for f in FORM_FIELDS}
    values["service"] = ",".join(str(v) for v in form.getlist("service"))
    try:
        enq = service.create_enquiry(session, str(form.get("text", "")), operator(request), form=values)
    except service.QuoteError as exc:
        session.rollback()
        return back("/", err=str(exc))
    return back(f"/enquiries/{enq.id}", msg="Enquiry read. Check the highlighted values against the text.")


@app.get("/enquiries/{enquiry_id}")
def enquiry_page(request: Request, enquiry_id: int, session: Session = Depends(get_session)):
    enq = enquiry_or_404(session, enquiry_id)
    req, result, quote = service.requirements(enq), service.priced(enq), enq.quote
    pl = service.as_price_list(quote.price_list)
    latest = service.current_price_list(session)
    adj = service.adjustments(quote)
    custom_index = {f"custom.{n}": n - 1 for n in range(1, len(adj.custom) + 1)}
    return page(request, "enquiry.html", enq=enq, req=req, priced=result, quote=quote, pl=pl,
                status=service.status(enq), approved=service.is_approved(quote),
                pieces=highlighted(enq.text, req), number=service.quote_number(quote),
                newer_list=latest.version if latest.id != quote.price_list_id else None,
                overrides=adj.overrides, custom_index=custom_index, draft=service.reply_draft(enq),
                other_services=[s for s in pl.services if s.code not in req.services])


@app.get("/enquiries/{enquiry_id}/quote.pdf")
def enquiry_pdf(request: Request, enquiry_id: int, session: Session = Depends(get_session)):
    enquiry_or_404(session, enquiry_id)
    try:
        number, data = service.quote_pdf(session, enquiry_id, operator(request))
    except service.QuoteError as exc:
        raise HTTPException(409, str(exc)) from None
    return Response(data, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="quote-{number}.pdf"'})


# --- operator actions --------------------------------------------------------

@app.post("/enquiries/{enquiry_id}/field")
def set_field(request: Request, enquiry_id: int, name: str = Form(), value: str = Form(""),
              session: Session = Depends(get_session)):
    return act(session, enquiry_id, f"{FIELD_LABELS.get(name, name)} saved.", service.set_field,
               name, value, operator(request))


@app.post("/enquiries/{enquiry_id}/service")
def set_service(request: Request, enquiry_id: int, code: str = Form(), include: str = Form("yes"),
                session: Session = Depends(get_session)):
    return act(session, enquiry_id, "Services updated.", service.set_service, code, include == "yes",
               operator(request))


@app.post("/enquiries/{enquiry_id}/acknowledge")
def acknowledge(request: Request, enquiry_id: int, label: str = Form(), session: Session = Depends(get_session)):
    return act(session, enquiry_id, f"{label} will be left out of the quote.", service.acknowledge_unsupported,
               label, operator(request))


@app.post("/enquiries/{enquiry_id}/discount")
def discount(request: Request, enquiry_id: int, decision: str = Form(), percent: str = Form(""),
             session: Session = Depends(get_session)):
    choice = "declined" if decision == "decline" else percent
    return act(session, enquiry_id, "Discount decision recorded.", service.decide_discount, choice,
               operator(request))


@app.post("/enquiries/{enquiry_id}/override")
def override(request: Request, enquiry_id: int, code: str = Form(), quantity: str = Form(""),
             unit_price: str = Form(""), session: Session = Depends(get_session)):
    return act(session, enquiry_id, "Line updated.", service.set_override, code, quantity, unit_price,
               operator(request))


@app.post("/enquiries/{enquiry_id}/custom")
def custom_add(request: Request, enquiry_id: int, description: str = Form(""), amount: str = Form(""),
               session: Session = Depends(get_session)):
    return act(session, enquiry_id, "Line added.", service.add_custom_line, description, amount,
               operator(request))


@app.post("/enquiries/{enquiry_id}/custom/remove")
def custom_remove(request: Request, enquiry_id: int, index: int = Form(), session: Session = Depends(get_session)):
    return act(session, enquiry_id, "Line removed.", service.remove_custom_line, index, operator(request))


@app.post("/enquiries/{enquiry_id}/reprice")
def reprice(request: Request, enquiry_id: int, session: Session = Depends(get_session)):
    return act(session, enquiry_id, "Repriced with the current price list.", service.reprice_with_current_list,
               operator(request))


@app.post("/enquiries/{enquiry_id}/approve")
def approve(request: Request, enquiry_id: int, session: Session = Depends(get_session)):
    return act(session, enquiry_id, "Quote approved. Nothing has been sent; download the PDF and send it yourself.",
               service.approve, operator(request))


@app.get("/prices")
def prices(request: Request, session: Session = Depends(get_session)):
    current = service.current_price_list(session)
    versions = session.scalars(select(PriceListVersion).order_by(PriceListVersion.version.desc())).all()
    return page(request, "prices.html", current=current, pl=service.as_price_list(current), versions=versions,
                raw=json.dumps(current.data, indent=2))


@app.post("/prices")
def prices_publish(request: Request, data: str = Form(), note: str = Form(""),
                   session: Session = Depends(get_session)):
    try:
        row = service.publish_price_list(session, data, operator(request), note)
    except service.QuoteError as exc:
        session.rollback()
        return back("/prices", err=str(exc))
    return back("/prices", msg=f"Published version {row.version}. Existing quotes keep the version they were priced with.")


@app.post("/operator")
def set_operator(name: str = Form(), next: str = Form("/")):
    target = next if next.startswith("/") and not next.startswith("//") else "/"
    response = RedirectResponse(target, status_code=303)
    response.set_cookie("operator", name.strip()[:80], max_age=60 * 60 * 24 * 90, httponly=True, samesite="lax")
    return response


@app.get("/healthz")
def healthz(session: Session = Depends(get_session)):
    session.execute(select(1))
    return {"ok": True}
