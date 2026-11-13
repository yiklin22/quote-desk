"""Web application: accounts, then enquiry -> clarification checklist -> editable estimate -> approval -> PDF.

Every page except login, signup and the health check needs a logged-in
account, and every query is limited to that account's rows.
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote as urlquote
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import auth, config, service
from .db import get_engine, get_session
from .domain import FIELD_LABELS, FREQUENCIES
from .drafting import NOTHING_YET
from .models import Account, Enquiry, PriceListVersion

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "web")
STATUS_LABELS = {"needs_info": "Needs information", "ready": "Ready for approval", "approved": "Approved",
                 "cannot_quote": "Cannot quote"}
FORM_FIELDS = ["area_sqm", "bedrooms", "bathrooms", "carpet_rooms", "windows", "frequency", "weekend",
               "promo_code", "customer_name", "customer_email"]
templates.env.globals.update(status_labels=STATUS_LABELS, field_labels=FIELD_LABELS, frequencies=FREQUENCIES)
COOKIE = "session"
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                               "style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; "
                               "form-action 'self'; base-uri 'none'",
}


class NotLoggedIn(Exception):
    pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_engine()
    yield


app = FastAPI(title="Quote desk", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "web" / "static"), name="static")


@app.middleware("http")
async def guard(request: Request, call_next):
    """Refuse cross-site and oversized writes before anything reads the body,
    and put the same security headers on every response."""
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        # A browser says where a request came from. A form on another site
        # must not be able to act with this site's login cookie.
        site, origin = request.headers.get("sec-fetch-site"), request.headers.get("origin")
        if site is not None:
            cross = site not in ("same-origin", "none")
        else:
            cross = origin is not None and urlsplit(origin).netloc != request.headers.get("host")
        if cross:
            return PlainTextResponse("Requests from other sites are not accepted.", status_code=403)
        length = request.headers.get("content-length", "")
        if not length.isdigit():
            return PlainTextResponse("A Content-Length header is required.", status_code=411)
        if int(length) > config.MAX_REQUEST_BYTES:
            return PlainTextResponse(f"The request is larger than {config.MAX_REQUEST_BYTES // 1000} kB.",
                                     status_code=413)
    response = await call_next(request)
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    if not request.url.path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


def current_account(request: Request, session: Session = Depends(get_session)) -> Account:
    account = auth.account_for_token(session, request.cookies.get(COOKIE))
    if account is None:
        raise NotLoggedIn()
    request.state.account = account
    return account


@app.exception_handler(NotLoggedIn)
def not_logged_in(request: Request, exc: NotLoggedIn):
    target = request.url.path if request.method == "GET" else "/"
    return RedirectResponse(f"/login?next={urlquote(target)}", status_code=303)


def page(request: Request, template: str, /, status_code: int = 200, **context):
    context.setdefault("flash", request.query_params.get("msg") or request.query_params.get("err"))
    context.setdefault("flash_kind", "err" if request.query_params.get("err") else "ok")
    context["account"] = getattr(request.state, "account", None)
    return templates.TemplateResponse(request, template, context, status_code=status_code)


def back(path: str, msg: str = "", err: str = "") -> RedirectResponse:
    query = f"?msg={urlquote(msg)}" if msg else f"?err={urlquote(err)}" if err else ""
    return RedirectResponse(path + query, status_code=303)


def enquiry_or_404(session: Session, account: Account, enquiry_id: int) -> Enquiry:
    """Another account's enquiry gets the same answer as one that does not exist."""
    try:
        return service.get_enquiry(session, account.id, enquiry_id)
    except LookupError:
        raise HTTPException(404, "No such enquiry") from None


def safe_next(target: str) -> str:
    """Only ever redirect to a path on this site."""
    ok = target.startswith("/") and not target.startswith("//") and "\\" not in target
    return target if ok else "/"


def set_session_cookie(request: Request, response: Response, token: str) -> None:
    response.set_cookie(COOKIE, token, max_age=config.SESSION_DAYS * 86400, httponly=True, samesite="lax",
                        secure=config.COOKIE_SECURE or request.url.scheme == "https")


def client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def act(session: Session, account: Account, enquiry_id: int, done: str, fn, *args) -> RedirectResponse:
    """Run one operator action and return to the enquiry with a message."""
    enquiry_or_404(session, account, enquiry_id)
    try:
        fn(session, account.id, enquiry_id, *args)
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
def index(request: Request,
        account: Account = Depends(current_account), session: Session = Depends(get_session)):
    rows = []
    for enq in session.scalars(select(Enquiry).where(Enquiry.account_id == account.id).order_by(Enquiry.id.desc())):
        req = service.requirements(enq)
        rows.append({"enq": enq, "status": service.status(enq), "services": sorted(req.services),
                     "who": req.text("customer_name") or req.text("customer_email") or "",
                     "preview": " ".join(enq.text.split())[:90] or "(entered by form)"})
    pl = service.current_price_list(session, account.id)
    return page(request, "index.html", rows=rows, price_list=service.as_price_list(pl), form_fields=FORM_FIELDS)


@app.post("/enquiries")
async def enquiry_create(request: Request,
        account: Account = Depends(current_account), session: Session = Depends(get_session)):
    form = await request.form()
    values = {f: str(form.get(f, "")) for f in FORM_FIELDS}
    values["service"] = ",".join(str(v) for v in form.getlist("service"))
    try:
        enq = service.create_enquiry(session, account.id, str(form.get("text", "")), account.name, form=values)
    except service.QuoteError as exc:
        session.rollback()
        return back("/", err=str(exc))
    return back(f"/enquiries/{enq.id}", msg="Enquiry read. Check the highlighted values against the text.")


@app.get("/enquiries/{enquiry_id}")
def enquiry_page(request: Request, enquiry_id: int,
        account: Account = Depends(current_account), session: Session = Depends(get_session)):
    enq = enquiry_or_404(session, account, enquiry_id)
    req, result, quote = service.requirements(enq), service.priced(enq), enq.quote
    pl = service.as_price_list(quote.price_list)
    latest = service.current_price_list(session, account.id)
    adj = service.adjustments(quote)
    custom_index = {f"custom.{n}": n - 1 for n in range(1, len(adj.custom) + 1)}
    draft, draft_by_model = service.reply_draft(enq)
    return page(request, "enquiry.html", enq=enq, req=req, priced=result, quote=quote, pl=pl,
                status=service.status(enq), approved=service.is_approved(quote),
                pieces=highlighted(enq.text, req), number=service.quote_number(quote),
                newer_list=latest.version if latest.id != quote.price_list_id else None,
                overrides=adj.overrides, custom_index=custom_index, draft=draft, draft_by_model=draft_by_model,
                can_reword=config.MODEL_EXTRACTION and draft != NOTHING_YET,
                other_services=[s for s in pl.services if s.code not in req.services])


@app.get("/enquiries/{enquiry_id}/quote.pdf")
def enquiry_pdf(request: Request, enquiry_id: int,
        account: Account = Depends(current_account), session: Session = Depends(get_session)):
    enquiry_or_404(session, account, enquiry_id)
    try:
        number, data = service.quote_pdf(session, account.id, enquiry_id, account.name)
    except service.QuoteError as exc:
        raise HTTPException(409, str(exc)) from None
    return Response(data, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="quote-{number}.pdf"'})


# --- operator actions --------------------------------------------------------

@app.post("/enquiries/{enquiry_id}/field")
def set_field(request: Request, enquiry_id: int, name: str = Form(), value: str = Form(""),
        account: Account = Depends(current_account), session: Session = Depends(get_session)):
    return act(session, account, enquiry_id, f"{FIELD_LABELS.get(name, name)} saved.", service.set_field,
               name, value, account.name)


@app.post("/enquiries/{enquiry_id}/service")
def set_service(request: Request, enquiry_id: int, code: str = Form(), include: str = Form("yes"),
        account: Account = Depends(current_account), session: Session = Depends(get_session)):
    return act(session, account, enquiry_id, "Services updated.", service.set_service, code, include == "yes",
               account.name)


@app.post("/enquiries/{enquiry_id}/acknowledge")
def acknowledge(request: Request, enquiry_id: int, label: str = Form(),
        account: Account = Depends(current_account), session: Session = Depends(get_session)):
    return act(session, account, enquiry_id, f"{label} will be left out of the quote.", service.acknowledge_unsupported,
               label, account.name)


@app.post("/enquiries/{enquiry_id}/discount")
def discount(request: Request, enquiry_id: int, decision: str = Form(), percent: str = Form(""),
        account: Account = Depends(current_account), session: Session = Depends(get_session)):
    choice = "declined" if decision == "decline" else percent
    return act(session, account, enquiry_id, "Discount decision recorded.", service.decide_discount, choice,
               account.name)


@app.post("/enquiries/{enquiry_id}/override")
def override(request: Request, enquiry_id: int, code: str = Form(), quantity: str = Form(""),
             unit_price: str = Form(""),
        account: Account = Depends(current_account), session: Session = Depends(get_session)):
    return act(session, account, enquiry_id, "Line updated.", service.set_override, code, quantity, unit_price,
               account.name)


@app.post("/enquiries/{enquiry_id}/custom")
def custom_add(request: Request, enquiry_id: int, description: str = Form(""), amount: str = Form(""),
        account: Account = Depends(current_account), session: Session = Depends(get_session)):
    return act(session, account, enquiry_id, "Line added.", service.add_custom_line, description, amount,
               account.name)


@app.post("/enquiries/{enquiry_id}/custom/remove")
def custom_remove(request: Request, enquiry_id: int, index: int = Form(),
        account: Account = Depends(current_account), session: Session = Depends(get_session)):
    return act(session, account, enquiry_id, "Line removed.", service.remove_custom_line, index, account.name)


@app.post("/enquiries/{enquiry_id}/reprice")
def reprice(request: Request, enquiry_id: int,
        account: Account = Depends(current_account), session: Session = Depends(get_session)):
    return act(session, account, enquiry_id, "Repriced with the current price list.", service.reprice_with_current_list,
               account.name)


@app.post("/enquiries/{enquiry_id}/approve")
def approve(request: Request, enquiry_id: int,
        account: Account = Depends(current_account), session: Session = Depends(get_session)):
    return act(session, account, enquiry_id, "Quote approved. Nothing has been sent; download the PDF and send it yourself.",
               service.approve, account.name)


@app.post("/enquiries/{enquiry_id}/reword")
def reword(request: Request, enquiry_id: int,
        account: Account = Depends(current_account), session: Session = Depends(get_session)):
    return act(session, account, enquiry_id, "Reworded by the model. Figures and questions were checked; read the rest.",
               service.reword_reply, account.name)


@app.post("/enquiries/{enquiry_id}/standard-wording")
def standard_wording(request: Request, enquiry_id: int,
        account: Account = Depends(current_account), session: Session = Depends(get_session)):
    return act(session, account, enquiry_id, "Back to the standard wording.", service.use_standard_reply, account.name)


@app.get("/prices")
def prices(request: Request,
        account: Account = Depends(current_account), session: Session = Depends(get_session)):
    current = service.current_price_list(session, account.id)
    versions = session.scalars(select(PriceListVersion).where(PriceListVersion.account_id == account.id)
                               .order_by(PriceListVersion.version.desc())).all()
    return page(request, "prices.html", current=current, pl=service.as_price_list(current), versions=versions,
                raw=json.dumps(current.data, indent=2))


@app.post("/prices")
def prices_publish(request: Request, data: str = Form(), note: str = Form(""),
        account: Account = Depends(current_account), session: Session = Depends(get_session)):
    try:
        row = service.publish_price_list(session, account.id, data, account.name, note)
    except service.QuoteError as exc:
        session.rollback()
        return back("/prices", err=str(exc))
    return back("/prices", msg=f"Published version {row.version}. Existing quotes keep the version they were priced with.")


@app.post("/enquiries/{enquiry_id}/delete")
def enquiry_delete(enquiry_id: int, account: Account = Depends(current_account),
                   session: Session = Depends(get_session)):
    enq = enquiry_or_404(session, account, enquiry_id)
    number = enq.number
    service.delete_enquiry(session, account.id, enquiry_id)
    return back("/", msg=f"Deleted enquiry {number} with its quote and history.")


# --- accounts ----------------------------------------------------------------

@app.get("/login")
def login_page(request: Request, next: str = "/", session: Session = Depends(get_session)):
    if auth.account_for_token(session, request.cookies.get(COOKIE)):
        return RedirectResponse(safe_next(next), status_code=303)
    return page(request, "login.html", next=safe_next(next), email="")


@app.post("/login")
def login(request: Request, email: str = Form(""), password: str = Form(""), next: str = Form("/"),
          session: Session = Depends(get_session)):
    try:
        token = auth.log_in(session, email, password, client_ip(request))
    except auth.AuthError as exc:
        return page(request, "login.html", status_code=exc.status_code, flash=str(exc), flash_kind="err",
                    next=safe_next(next), email=email[:254])
    response = RedirectResponse(safe_next(next), status_code=303)
    set_session_cookie(request, response, token)
    return response


@app.get("/signup")
def signup_page(request: Request):
    return page(request, "signup.html", invite_needed=bool(config.INVITE_CODE), name="", email="",
                min_password=config.MIN_PASSWORD_CHARS)


@app.post("/signup")
def signup(request: Request, name: str = Form(""), email: str = Form(""), password: str = Form(""),
           invite: str = Form(""), session: Session = Depends(get_session)):
    try:
        auth.create_account(session, email, name, password, invite)
        token = auth.log_in(session, email, password, client_ip(request))
    except auth.AuthError as exc:
        return page(request, "signup.html", status_code=exc.status_code, flash=str(exc), flash_kind="err",
                    invite_needed=bool(config.INVITE_CODE), name=name[:80], email=email[:254],
                    min_password=config.MIN_PASSWORD_CHARS)
    response = back("/account", msg="Account created. It starts with a placeholder price list and no enquiries.")
    set_session_cookie(request, response, token)
    return response


@app.post("/logout")
def logout(request: Request, session: Session = Depends(get_session)):
    auth.log_out(session, request.cookies.get(COOKIE))
    response = back("/login", msg="You are logged out.")
    response.delete_cookie(COOKIE)
    return response


@app.get("/account")
def account_page(request: Request, account: Account = Depends(current_account),
                 session: Session = Depends(get_session)):
    return page(request, "account.html", usage=service.usage(session, account.id),
                sample=service.sample_available(), max_chars=config.MAX_ENQUIRY_CHARS)


@app.post("/account/sample")
def account_sample(account: Account = Depends(current_account), session: Session = Depends(get_session)):
    if not service.sample_available():
        raise HTTPException(404, "No sample data is installed")
    try:
        added = service.load_sample_data(session, account.id, account.name)
    except service.QuoteError as exc:
        return back("/account", err=str(exc))
    return back("/", msg=f"Added {added} sample enquiries.")


@app.post("/account/delete-data")
def account_delete_data(password: str = Form(""), account: Account = Depends(current_account),
                        session: Session = Depends(get_session)):
    try:
        auth.check_password(account, password)
    except auth.AuthError as exc:
        return back("/account", err=str(exc))
    removed = service.delete_all_data(session, account.id)
    return back("/account", msg=f"Deleted {removed['enquiries']} enquiry(ies) with their quotes, and "
                                f"{removed['price_lists']} price list version(s). Your account is still here.")


@app.post("/account/delete")
def account_delete(password: str = Form(""), account: Account = Depends(current_account),
                   session: Session = Depends(get_session)):
    try:
        auth.check_password(account, password)
    except auth.AuthError as exc:
        return back("/account", err=str(exc))
    service.delete_account(session, account.id)
    response = back("/login", msg="Your account and everything in it were deleted.")
    response.delete_cookie(COOKIE)
    return response


@app.get("/healthz")
def healthz(session: Session = Depends(get_session)):
    session.execute(select(1))
    return {"ok": True}
