"""The screens, over HTTP against PostgreSQL."""
import pytest
from fastapi.testclient import TestClient

from conftest import BY_ID


@pytest.fixture
def client(session):
    from quotedesk.app import app
    with TestClient(app) as c:
        c.post("/operator", data={"name": "Ana", "next": "/"})
        yield c


def paste(client, eid):
    return client.post("/enquiries", data={"text": BY_ID[eid]["text"]})


def test_enquiry_to_pdf_flow(client):
    r = paste(client, "E13")
    assert str(r.url).endswith("/enquiries/1?msg=Enquiry%20read.%20Check%20the%20highlighted%20values%20against%20the%20text.")
    html = r.text
    assert '<mark id="f-area_sqm" title="Floor area (m2)">200 sqm' in html         # evidence in the enquiry text
    assert 'href="#f-area_sqm">&ldquo;200 sqm&rdquo;' in html                      # and linked from the value
    assert "pest control, which is not on the price list" in html and "Needs information" in html
    assert "disabled" in html and "Incomplete." in html
    assert client.get("/enquiries/1/quote.pdf").status_code == 409                 # no PDF before approval

    html = client.post("/enquiries/1/acknowledge", data={"label": "Pest control"}).text
    assert "Ready for approval" in html and "Approve revision 2 at 187.00 AUD" in html
    html = client.post("/enquiries/1/approve").text
    assert "Nothing has been sent" in html and "approved by Ana at 187.00 AUD" in html
    assert "The total is $187.00 including GST of $17.00" in html                  # reply draft, from computed figures
    pdf = client.get("/enquiries/1/quote.pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    assert 'filename="quote-Q00001-r2.pdf"' in pdf.headers["content-disposition"]

    html = client.post("/enquiries/1/override", data={"code": "office_clean.area_sqm", "quantity": "210", "unit_price": ""}).text
    assert "Ready for approval" in html and "approval invalidated" in html and "revision 3" in html
    assert client.get("/enquiries/1/quote.pdf").status_code == 409                 # the edit withdrew the approval
    assert "Approved" not in client.get("/").text.split("<tbody>")[1]


def test_checklist_answers_and_discount_decision(client):
    html = paste(client, "E15").text
    assert "two answers for bedrooms: 3 or 4" in html and "two values in the text" in html
    html = client.post("/enquiries/1/field", data={"name": "bedrooms", "value": "4"}).text
    assert "typed by Ana" in html and "Nothing outstanding." in html and "233.75" in html
    html = client.post("/enquiries/1/field", data={"name": "bedrooms", "value": "lots"}).text
    assert "is not a valid value" in html and "233.75" in html

    paste(client, "E20")
    html = client.post("/enquiries/2/discount", data={"decision": "grant", "percent": "5"}).text
    assert "Discount granted by operator (5%)" in html and "198.55" in html
    html = client.post("/enquiries/2/custom", data={"description": "Stain treatment", "amount": "30"}).text
    assert "Stain treatment" in html and "229.90" in html      # (190 + 30) less 5%, plus GST
    html = client.post("/enquiries/2/custom/remove", data={"index": "0"}).text
    assert "Stain treatment: 30" in html and "198.55" in html  # still in the history, gone from the estimate


def test_form_entry_price_list_page_and_errors(client):
    r = client.post("/enquiries", data={"text": "", "service": ["window_clean", "oven_clean"], "windows": "8"})
    assert "Entered by form" in r.text and "170.50" in r.text and "entered on the form" in r.text
    assert "Paste the enquiry or fill in the form." in client.post("/enquiries", data={"text": ""}).text
    html = client.get("/prices").text
    assert "Price list, version 1" in html and "0.85 per m2" in html
    assert "The price list is not valid" in client.post("/prices", data={"data": "{}", "note": ""}).text
    assert client.get("/enquiries/99").status_code == 404 and client.get("/healthz").json() == {"ok": True}


def test_enquiry_text_is_escaped(client):
    html = client.post("/enquiries", data={"text": "<script>alert(1)</script> office clean 80 sqm"}).text
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html
