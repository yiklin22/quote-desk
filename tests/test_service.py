"""The acceptance claims, on PostgreSQL."""
import ast
from decimal import Decimal as D
from pathlib import Path

import pytest
from pdfminer.high_level import extract_text
from sqlalchemy import func, select

from conftest import ACC, BY_ID, ENQUIRIES, ROOT, TODAY
from quotedesk import service
from quotedesk.models import Approval, Enquiry, QuoteLine


def make(session, eid, **kw):
    return service.create_enquiry(session, ACC, BY_ID[eid]["text"], "ana", today=TODAY, **kw)


def test_all_fixtures_persist_with_expected_totals_and_status(session):
    for e in ENQUIRIES:
        enq = make(session, e["id"])
        want = e["expect"]
        if want["total"]:
            assert enq.quote.total == D(want["total"]) and service.status(enq) == "ready"
        else:
            assert service.status(enq) in ("needs_info", "cannot_quote")
            with pytest.raises(service.QuoteError):
                service.approve(session, ACC, enq.id, "ana")
        assert enq.quote.revision == 1 and enq.quote.price_list.version == 1
    assert session.scalar(select(func.count()).select_from(Approval)) == 0


def test_same_enquiry_twice_gives_the_same_lines_and_total(session):
    a, b = make(session, "E08"), make(session, "E08")
    lines = lambda q: [(ln.code, ln.quantity, ln.unit_price, ln.amount) for ln in q.lines]
    assert lines(a.quote) == lines(b.quote) and a.quote.total == b.quote.total == D("452.10")
    assert a.quote.inputs_hash == b.quote.inputs_hash


def test_missing_information_blocks_approval_and_pdf_until_provided(session):
    enq = make(session, "E09")
    for blocked in (lambda: service.approve(session, ACC, enq.id, "ana"), lambda: service.quote_pdf(session, ACC, enq.id, "ana")):
        with pytest.raises(service.QuoteError):
            blocked()
    service.set_field(session, ACC, enq.id, "area_sqm", "140", "ana")
    assert service.status(enq) == "ready" and enq.quote.total == D("112.20") and enq.quote.revision == 2
    assert service.requirements(enq).fields["area_sqm"].source == "operator"
    service.approve(session, ACC, enq.id, "ana")
    assert service.status(enq) == "approved"


def test_any_edit_invalidates_the_approval(session):
    edits = [
        lambda i: service.set_field(session, ACC, i, "bathrooms", "3", "ben"),
        lambda i: service.set_override(session, ACC, i, "end_of_lease.area_sqm", "", "4.00", "ben"),
        lambda i: service.add_custom_line(session, ACC, i, "Balcony", "40", "ben"),
        lambda i: service.set_service(session, ACC, i, "oven_clean", True, "ben"),
        lambda i: service.set_field(session, ACC, i, "weekend", "yes", "ben"),
    ]
    for edit in edits:
        enq = make(session, "E02")
        service.approve(session, ACC, enq.id, "ana")
        number, _ = service.quote_pdf(session, ACC, enq.id, "ana")
        assert number.endswith("-r1")
        edit(enq.id)
        assert service.status(enq) == "ready" and enq.quote.revision == 2
        assert "approval_invalidated" in [e.action for e in enq.events]
        with pytest.raises(service.QuoteError):
            service.quote_pdf(session, ACC, enq.id, "ana")
        assert enq.quote.approvals[-1].revision == 1          # the old approval is kept as history


def test_changing_a_value_back_does_not_restore_the_approval(session):
    enq = make(session, "E02")
    service.approve(session, ACC, enq.id, "ana")
    service.set_field(session, ACC, enq.id, "bathrooms", "3", "ben")
    service.set_field(session, ACC, enq.id, "bathrooms", "2", "ben")
    assert enq.quote.total == D("537.90") and enq.quote.revision == 3 and service.status(enq) == "ready"


def test_a_no_op_edit_keeps_the_approval(session):
    enq = make(session, "E02")
    service.approve(session, ACC, enq.id, "ana")
    service.set_field(session, ACC, enq.id, "bathrooms", "2", "ben")
    assert service.status(enq) == "approved" and enq.quote.revision == 1


def test_new_price_list_does_not_change_existing_quotes_until_repriced(session, price_list):
    enq = make(session, "E05")
    service.approve(session, ACC, enq.id, "ana")
    data = price_list.model_dump_json().replace('"windows":"9.50"', '"windows":"10.00"')
    assert data != price_list.model_dump_json()
    v2 = service.publish_price_list(session, ACC, data, "ana", "Window price rise")
    assert v2.version == 2 and enq.quote.price_list.version == 1 and enq.quote.total == D("300.96")
    assert service.status(enq) == "approved"
    assert make(session, "E05").quote.total == D("316.80")       # new enquiries use version 2
    service.reprice_with_current_list(session, ACC, enq.id, "ana")
    assert enq.quote.total == D("316.80") and enq.quote.price_list.version == 2 and service.status(enq) == "ready"
    with pytest.raises(service.QuoteError):
        service.publish_price_list(session, ACC, '{"business_name": "x", "services": []}', "ana")
    with pytest.raises(service.QuoteError):
        service.publish_price_list(session, ACC, "not json", "ana")


def test_contradiction_unsupported_and_discount_flows(session):
    enq = make(session, "E15")
    service.set_field(session, ACC, enq.id, "bedrooms", "4", "ana")
    assert service.status(enq) == "ready" and enq.quote.total == D("233.75")
    enq = make(session, "E14")
    service.acknowledge_unsupported(session, ACC, enq.id, "Gutter cleaning", "ana")
    assert service.status(enq) == "ready" and enq.quote.total == D("125.40")
    enq = make(session, "E20")
    with pytest.raises(service.QuoteError):
        service.decide_discount(session, ACC, enq.id, "80", "ana")
    service.decide_discount(session, ACC, enq.id, "declined", "ana")
    assert enq.quote.total == D("209.00")
    enq = make(session, "E12")
    service.acknowledge_unsupported(session, ACC, enq.id, "Pressure washing", "ana")
    assert service.status(enq) == "cannot_quote"


def test_form_values_override_text_and_form_only_enquiries_work(session):
    enq = service.create_enquiry(session, ACC, BY_ID["E16"]["text"], "ana", form={"area_sqm": "200"}, today=TODAY)
    assert service.status(enq) == "ready" and service.requirements(enq).fields["area_sqm"].source == "form"
    enq = service.create_enquiry(session, ACC, "", "ana", form={"service": "window_clean", "windows": "8"}, today=TODAY)
    assert enq.channel == "form" and enq.quote.total == D("83.60")
    for bad in ({"windows": "many"}, {"service": "moon_clean"}, {"nonsense": "1"}):
        with pytest.raises(service.QuoteError):
            service.create_enquiry(session, ACC, "", "ana", form=bad, today=TODAY)
    with pytest.raises(service.QuoteError):
        service.create_enquiry(session, ACC, "  ", "ana")


def test_invalid_operator_input_changes_nothing(session):
    enq = make(session, "E02")
    for bad in (lambda: service.set_field(session, ACC, enq.id, "bathrooms", "two-ish", "ana"),
                lambda: service.set_override(session, ACC, enq.id, "end_of_lease.area_sqm", "x", "", "ana"),
                lambda: service.set_override(session, ACC, enq.id, "no.such.line", "1", "1", "ana"),
                lambda: service.add_custom_line(session, ACC, enq.id, "", "5", "ana"),
                lambda: service.add_custom_line(session, ACC, enq.id, "Extra", "NaN", "ana"),
                lambda: service.remove_custom_line(session, ACC, enq.id, 3, "ana")):
        with pytest.raises(service.QuoteError):
            bad()
    session.rollback()
    assert enq.quote.revision == 1 and enq.quote.total == D("537.90")


def test_pdf_states_the_approved_figures(session, tmp_path):
    enq = make(session, "E13")
    service.acknowledge_unsupported(session, ACC, enq.id, "Pest control", "ana")
    service.approve(session, ACC, enq.id, "ana")
    number, data = service.quote_pdf(session, ACC, enq.id, "ana")
    path = tmp_path / "q.pdf"
    path.write_bytes(data)
    text = " ".join(extract_text(str(path)).split())
    for expected in (number, "170.00", "17.00", "187.00", "Pest control", "price list version 1", "Approved by ana"):
        assert expected in text
    assert number == "Q00001-r2"


def test_stored_lines_equal_recomputed_lines(session):
    for e in ENQUIRIES:
        enq = make(session, e["id"])
        stored = [(ln.code, ln.amount) for ln in session.scalars(
            select(QuoteLine).where(QuoteLine.quote_id == enq.quote.id).order_by(QuoteLine.position))]
        assert stored == [(ln.code, ln.amount) for ln in service.priced(enq).lines]
    assert session.scalar(select(func.count()).select_from(Enquiry)) == 20


def test_the_code_has_no_way_to_send_anything():
    """Nothing sends automatically, because nothing can send at all."""
    banned = {"smtplib", "email", "imaplib", "poplib", "httpx", "requests", "aiohttp", "socket", "boto3",
              "twilio", "sendgrid", "anthropic", "openai", "urllib.request", "http.client", "subprocess"}
    for path in (ROOT / "quotedesk").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                [node.module or ""] if isinstance(node, ast.ImportFrom) and node.level == 0 else []
            for name in names:
                assert name not in banned and name.split(".")[0] not in banned, f"{path.name} imports {name}"
