"""Login, per-account data, deletion, input limits and usage caps."""
from datetime import timedelta
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from conftest import ACC, BY_ID, EMAIL, PASSWORD, TODAY, add_account, log_in
from quotedesk import auth, config, service
from quotedesk.models import (Account, Approval, Enquiry, Event, LoginSession, PriceListVersion, Quote, QuoteLine,
                              UsageCounter, now)

WRITES = [("/enquiries/1/field", {"name": "windows", "value": "2"}), ("/enquiries/1/approve", {}),
          ("/enquiries/1/service", {"code": "oven_clean"}), ("/enquiries/1/override", {"code": "window_clean.windows", "unit_price": "1"}),
          ("/enquiries/1/custom", {"description": "x", "amount": "1"}), ("/enquiries/1/custom/remove", {"index": "0"}),
          ("/enquiries/1/acknowledge", {"label": "x"}), ("/enquiries/1/discount", {"decision": "decline"}),
          ("/enquiries/1/reprice", {}), ("/enquiries/1/delete", {})]


def count(session, table, account_id=None):
    query = select(func.count()).select_from(table)
    return session.scalar(query if account_id is None else query.where(table.account_id == account_id))


def make(session, eid, account_id=ACC):
    return service.create_enquiry(session, account_id, BY_ID[eid]["text"], "ana", today=TODAY)


@pytest.fixture
def approved(session):
    """Ana's account holds E05 (24 windows, 300.96), approved."""
    enq = make(session, "E05")
    service.approve(session, ACC, enq.id, "ana")
    return enq


@pytest.fixture
def other(session):
    """A second browser, logged in as Ben."""
    from quotedesk.app import app
    add_account(session, "ben@example.test", "Ben")
    with TestClient(app) as c:
        yield log_in(c, "ben@example.test")


def state(session):
    session.expire_all()
    q = session.scalar(select(Quote))
    return q.revision, q.total, len(q.approvals), count(session, Event), count(session, PriceListVersion)


# --- login -------------------------------------------------------------------

def test_password_is_stored_hashed_and_checked(session):
    account = auth.create_account(session, " New@Example.Test ", "New", "a long enough password")
    assert account.email == "new@example.test" and account.password_hash.startswith("scrypt$")
    assert auth.verify_password("a long enough password", account.password_hash)
    assert not auth.verify_password("a long enough passwor", account.password_hash)
    assert not auth.verify_password("x", "not-a-hash")


def test_every_page_needs_a_login(approved, session, anon):
    before = state(session)
    for path in ("/", "/prices", "/account", "/enquiries/1", "/enquiries/1/quote.pdf"):
        r = anon.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login?next="), path
    for path, data in WRITES + [("/enquiries", {"text": BY_ID["E01"]["text"]}), ("/prices", {"data": "{}"}),
                                ("/account/delete", {"password": PASSWORD}), ("/account/sample", {})]:
        r = anon.post(path, data=data, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login"), path
    assert state(session) == before and count(session, Enquiry) == 1 and count(session, Account) == 1
    assert anon.get("/healthz").json() == {"ok": True} and anon.get("/static/app.css").status_code == 200


def test_signup_starts_with_its_own_placeholder_price_list(approved, session, anon):
    r = anon.post("/signup", data={"name": "Cy", "email": "cy@example.test", "password": "another long one"})
    assert "Account created" in r.text and "cy@example.test" in r.text
    assert "No enquiries yet." in anon.get("/").text
    assert "Price list, version 1" in anon.get("/prices").text
    assert sorted(session.scalars(select(PriceListVersion.account_id))) == [1, 2]
    row = session.scalar(select(LoginSession))
    assert row.token_hash != anon.cookies["session"] and len(row.token_hash) == 64


def test_signup_refusals(session, anon, monkeypatch):
    def signup(**over):
        return anon.post("/signup", data={"name": "Cy", "email": "cy@example.test", "password": "another long one", **over})
    assert "Use a password of 10" in signup(password="short").text
    assert "Enter an email address" in signup(email="not-an-email").text
    assert "already exists" in signup(email=EMAIL.upper()).text
    monkeypatch.setattr(config, "INVITE_CODE", "let-me-in")
    assert "invite code is not right" in signup(invite="guess").text
    monkeypatch.setattr(config, "MAX_ACCOUNTS", 1)
    assert "reached its limit on accounts" in signup(invite="let-me-in").text and count(session, Account) == 1
    monkeypatch.setattr(config, "MAX_ACCOUNTS", 2)
    assert "Account created" in signup(invite="let-me-in").text


def test_wrong_password_then_throttle(session, anon, monkeypatch):
    monkeypatch.setattr(config, "MAX_LOGIN_FAILURES_PER_EMAIL", 3)
    for _ in range(3):
        r = anon.post("/login", data={"email": EMAIL, "password": "wrong"})
        assert r.status_code == 400 and "do not match" in r.text
    r = anon.post("/login", data={"email": EMAIL, "password": PASSWORD})         # the right one, too late
    assert r.status_code == 429 and "Too many failed attempts" in r.text
    assert anon.get("/", follow_redirects=False).status_code == 303


def test_logout_and_expiry_end_the_session(session, client):
    assert client.get("/account").status_code == 200
    session.execute(update(LoginSession).values(expires_at=now() - timedelta(seconds=1)))
    session.commit()
    assert client.get("/account", follow_redirects=False).status_code == 303
    token = log_in(client).cookies["session"]
    assert "You are logged out." in client.post("/logout").text
    client.cookies.set("session", token)                                         # a copied cookie is dead too
    assert client.get("/account", follow_redirects=False).status_code == 303
    for target in ("//evil.example", "https://evil.example"):
        r = client.post("/login", data={"email": EMAIL, "password": PASSWORD, "next": target}, follow_redirects=False)
        assert r.headers["location"] == "/"


# --- isolation ---------------------------------------------------------------

def test_another_account_sees_and_changes_none_of_it(approved, session, other):
    before = state(session)
    assert "No enquiries yet." in other.get("/").text
    assert other.get("/enquiries/1").status_code == 404 and other.get("/enquiries/1/quote.pdf").status_code == 404
    for path, data in WRITES:
        assert other.post(path, data=data).status_code == 404, path
    assert state(session)[:4] == before[:4] and count(session, Enquiry) == 1


def test_service_refuses_another_accounts_enquiry(approved, session):
    ben = add_account(session, "ben@example.test", "Ben").id
    calls = [lambda: service.get_enquiry(session, ben, 1), lambda: service.set_field(session, ben, 1, "windows", "2", "ben"),
             lambda: service.set_service(session, ben, 1, "oven_clean", True, "ben"),
             lambda: service.set_override(session, ben, 1, "window_clean.windows", "", "1", "ben"),
             lambda: service.add_custom_line(session, ben, 1, "x", "1", "ben"),
             lambda: service.reprice_with_current_list(session, ben, 1, "ben"),
             lambda: service.approve(session, ben, 1, "ben"), lambda: service.quote_pdf(session, ben, 1, "ben"),
             lambda: service.delete_enquiry(session, ben, 1)]
    for call in calls:
        with pytest.raises(LookupError):
            call()


def test_each_account_has_its_own_price_list_and_quote_numbers(approved, session, other, client, price_list):
    dearer = price_list.model_dump_json().replace('"windows":"9.50"', '"windows":"10.00"')
    assert "Published version 2" in other.post("/prices", data={"data": dearer, "note": "Ben's prices"}).text
    assert "Price list, version 1" in client.get("/prices").text and "Ben&#39;s prices" not in client.get("/prices").text
    # The same enquiry is priced from each account's own list, and numbered from 1 in each account.
    ben_html = other.post("/enquiries", data={"text": BY_ID["E05"]["text"]}).text
    assert "316.80" in ben_html and "quote Q00001-r1" in ben_html
    ana_html = client.post("/enquiries", data={"text": BY_ID["E05"]["text"]}).text
    assert "300.96" in ana_html and "quote Q00002-r1" in ana_html
    assert approved.quote.total == D("300.96") and service.status(approved) == "approved"
    # Repricing Ana's quote uses Ana's newest list, which is still version 1.
    service.reprice_with_current_list(session, ACC, approved.id, "ana")
    assert approved.quote.price_list.version == 1 and approved.quote.price_list.account_id == ACC


# --- deletion ----------------------------------------------------------------

def test_delete_one_enquiry(approved, session, client):
    make(session, "E01")
    assert "Deleted enquiry 1 with its quote and history." in client.post("/enquiries/1/delete").text
    assert client.get("/enquiries/1").status_code == 404 and count(session, Enquiry) == 1
    assert count(session, Approval) == 0 and session.scalars(select(Quote.enquiry_id)).all() == [2]
    assert make(session, "E02").number == 3                                       # a number is not reused


def test_delete_all_data_needs_the_password_and_leaves_the_account(approved, session, other, client):
    other.post("/enquiries", data={"text": BY_ID["E01"]["text"]})
    r = client.post("/account/delete-data", data={"password": "wrong"})
    assert "nothing was deleted" in r.text and count(session, Enquiry, ACC) == 1
    r = client.post("/account/delete-data", data={"password": PASSWORD})
    assert "Deleted 1 enquiry(ies) with their quotes, and 1 price list version(s)" in r.text
    assert count(session, Enquiry, ACC) == 0 and count(session, PriceListVersion, ACC) == 0
    assert count(session, Enquiry) == 1 and count(session, Quote) == 1 and count(session, QuoteLine) > 0   # Ben's
    assert "Price list, version 1" in client.get("/prices").text                  # back to the placeholder


def test_delete_account_removes_everything_and_the_login(approved, session, other, client):
    other.post("/enquiries", data={"text": BY_ID["E01"]["text"]})
    token = client.cookies["session"]
    assert "nothing was deleted" in client.post("/account/delete", data={"password": ""}).text
    assert "Your account and everything in it were deleted." in client.post("/account/delete", data={"password": PASSWORD}).text
    for table in (Enquiry, PriceListVersion, UsageCounter, LoginSession):
        assert count(session, table, ACC) == 0, table.__tablename__
    assert count(session, Approval) == 0 and count(session, Quote) == 1
    assert session.scalars(select(Account.email)).all() == ["ben@example.test"]
    client.cookies.set("session", token)
    assert client.get("/", follow_redirects=False).status_code == 303
    assert client.post("/login", data={"email": EMAIL, "password": PASSWORD}).status_code == 400
    assert "office" in other.get("/enquiries/2").text


def test_sample_data_goes_into_the_callers_account_only(session, other, client):
    assert "Added 20 sample enquiries." in client.post("/account/sample").text
    assert count(session, Enquiry, ACC) == 20 and count(session, Enquiry) == 20
    assert "No enquiries yet." in other.get("/").text


# --- limits and caps ---------------------------------------------------------

def test_cap_on_enquiries_held(session, monkeypatch):
    monkeypatch.setattr(config, "MAX_ENQUIRIES", 2)
    first = make(session, "E01")
    make(session, "E02")
    with pytest.raises(service.QuoteError, match="already holds 2 enquiries"):
        make(session, "E03")
    session.rollback()
    service.delete_enquiry(session, ACC, first.id)                               # deleting makes room
    assert make(session, "E03").number == 3


def test_daily_cap_is_not_reset_by_deleting(session, monkeypatch):
    monkeypatch.setattr(config, "MAX_ENQUIRIES_PER_DAY", 2)
    make(session, "E01")
    make(session, "E02")
    service.delete_all_data(session, ACC)
    assert service.usage(session, ACC)["enquiries"] == 0 and service.usage(session, ACC)["enquiries_today"] == 2
    with pytest.raises(service.QuoteError, match="new enquiries a day"):
        make(session, "E03")
    session.rollback()
    ben = add_account(session, "ben@example.test", "Ben").id                      # another account has its own allowance
    assert make(session, "E03", ben).number == 1
    session.execute(update(UsageCounter).values(day=UsageCounter.day - timedelta(days=1)))
    session.commit()                                                              # the next day
    assert make(session, "E03").quote.total == D(BY_ID["E03"]["expect"]["total"])


def test_an_enquiry_refused_for_its_content_uses_no_allowance(session):
    for text in ("", "x" * (config.MAX_ENQUIRY_CHARS + 1)):
        with pytest.raises(service.QuoteError):
            service.create_enquiry(session, ACC, text, "ana")
        session.rollback()
    assert service.usage(session, ACC)["enquiries_today"] == 0


def test_caps_on_price_lists_and_extra_lines(session, price_list, monkeypatch):
    data = price_list.model_dump_json()
    monkeypatch.setattr(config, "MAX_PRICE_LISTS_PER_DAY", 1)
    assert service.publish_price_list(session, ACC, data, "ana").version == 2
    with pytest.raises(service.QuoteError, match="price list versions a day"):
        service.publish_price_list(session, ACC, data, "ana")
    monkeypatch.setattr(config, "MAX_PRICE_LIST_BYTES", 100)
    with pytest.raises(service.QuoteError, match="larger than"):
        service.publish_price_list(session, ACC, data, "ana")
    assert count(session, PriceListVersion) == 2
    monkeypatch.setattr(config, "MAX_CUSTOM_LINES", 1)
    enq = make(session, "E01")
    service.add_custom_line(session, ACC, enq.id, "One", "5", "ana")
    with pytest.raises(service.QuoteError, match="at most 1 extra lines"):
        service.add_custom_line(session, ACC, enq.id, "Two", "5", "ana")


def test_caps_show_as_messages_and_on_the_account_page(session, client, monkeypatch):
    monkeypatch.setattr(config, "MAX_ENQUIRIES_PER_DAY", 1)
    client.post("/enquiries", data={"text": BY_ID["E01"]["text"]})
    r = client.post("/enquiries", data={"text": BY_ID["E02"]["text"]})
    assert "The limit of 1 new enquiries a day has been reached." in r.text and count(session, Enquiry) == 1
    html = client.get("/account").text
    assert "<td>New enquiries today (UTC)</td><td class=\"num\">1</td><td class=\"num\">1</td>" in html
    assert "The limit of 1 new enquiries a day" in client.post("/account/sample").text


def test_oversized_and_unsized_requests_are_refused_unread(session, client, monkeypatch):
    monkeypatch.setattr(config, "MAX_REQUEST_BYTES", 2_000)
    r = client.post("/enquiries", data={"text": "office clean 80 sqm " * 200})
    assert r.status_code == 413 and count(session, Enquiry) == 0
    assert client.post("/logout", content=iter([b"a", b"b"])).status_code == 411  # chunked, no length
    assert client.get("/account").status_code == 200


# --- requests from other sites ----------------------------------------------

def test_writes_from_another_site_are_refused(approved, session, client):
    before = state(session)
    for headers in ({"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"},
                    {"Sec-Fetch-Site": "same-site", "Origin": "http://testserver"}):
        assert client.post("/enquiries/1/delete", headers=headers).status_code == 403, headers
        assert client.post("/enquiries/1/field", data={"name": "windows", "value": "2"}, headers=headers).status_code == 403
    assert state(session) == before
    for headers in ({"Origin": "http://testserver"}, {"Sec-Fetch-Site": "same-origin"}):
        assert client.post("/enquiries/1/field", data={"name": "windows", "value": "24"}, headers=headers).status_code == 200


def test_responses_carry_security_headers(session, client):
    r = client.get("/account")
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["cache-control"] == "no-store"
    assert r.headers["x-frame-options"] == "DENY" and "frame-ancestors 'none'" in r.headers["content-security-policy"]
    cookie = client.post("/login", data={"email": EMAIL, "password": PASSWORD}, follow_redirects=False).headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Secure" not in cookie
    with TestClient(client.app, base_url="https://testserver") as https:
        cookie = https.post("/login", data={"email": EMAIL, "password": PASSWORD}, follow_redirects=False).headers["set-cookie"]
        assert "Secure" in cookie
