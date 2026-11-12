"""Extraction, checklist and pricing, without a database."""
from datetime import date
from decimal import Decimal as D

import pytest

from conftest import BY_ID, ENQUIRIES, TODAY
from quotedesk.domain import Adjustments, CustomLine, Override, Requirements, Value, inputs_hash, price_quote
from quotedesk.drafting import amounts_ok, draft_reply
from quotedesk.extraction import RuleExtractor, verify


def read(eid):
    return RuleExtractor().extract(BY_ID[eid]["text"])


@pytest.mark.parametrize("e", ENQUIRIES, ids=lambda e: f"{e['id']}-{e['category']}")
def test_fixture_matches_hand_worked_expectation(e, price_list):
    req, want = RuleExtractor().extract(e["text"]), e["expect"]
    p = price_quote(req, price_list, Adjustments(), TODAY)
    assert sorted(req.services) == want["services"]
    assert {k: v.value for k, v in req.fields.items()} == want["fields"]
    assert sorted(req.conflicts) == want["conflicts"] and sorted(req.unsupported) == want["unsupported"]
    assert sorted(c.fingerprint for c in p.blockers) == want["blockers"]
    assert (str(p.total) if p.can_approve else None) == want["total"]


@pytest.mark.parametrize("e", ENQUIRIES, ids=lambda e: e["id"])
def test_every_extracted_value_quotes_the_enquiry(e):
    req = RuleExtractor().extract(e["text"])
    values = [*req.fields.values(), *req.services.values(), *req.unsupported.values(),
              *(v for vs in req.conflicts.values() for v in vs)]
    assert values and not req.rejected_evidence
    for v in values:
        assert e["text"][v.start:v.end] == v.quote and v.quote


def test_identical_inputs_reproduce_lines_totals_and_hash(price_list):
    for e in ENQUIRIES:
        runs = [price_quote(RuleExtractor().extract(e["text"]), price_list, Adjustments(), TODAY) for _ in range(3)]
        assert runs[0] == runs[1] == runs[2]
        hashes = {inputs_hash(RuleExtractor().extract(e["text"]), 1, Adjustments(), TODAY) for _ in range(3)}
        assert len(hashes) == 1


def test_hash_changes_with_anything_a_price_depends_on(price_list):
    req, adj = read("E01"), Adjustments()
    base = inputs_hash(req, 1, adj, TODAY)
    changed = req.model_copy(update={"fields": {**req.fields, "area_sqm": Value(value="181", source="operator")}})
    assert inputs_hash(changed, 1, adj, TODAY) != base
    assert inputs_hash(req, 2, adj, TODAY) != base
    assert inputs_hash(req, 1, adj, date(2027, 1, 1)) != base
    assert inputs_hash(req, 1, Adjustments(custom=[CustomLine(description="x", amount=D("1"))]), TODAY) != base
    # who typed a value and where it was found do not change a price
    same = req.model_copy(update={"fields": {**req.fields, "area_sqm": Value(value="180", source="operator", by="ana")}})
    assert inputs_hash(same, 1, adj, TODAY) == base


def test_missing_information_blocks_but_shows_what_can_be_priced(price_list):
    req = RuleExtractor().extract("Please quote an end of lease clean, 2 bathrooms, and clean the oven.")
    p = price_quote(req, price_list, Adjustments(), TODAY)
    assert [c.fingerprint for c in p.blockers] == ["MISSING_INFO:area_sqm"] and not p.can_approve
    assert [ln.code for ln in p.lines] == ["oven_clean.base"]      # the incomplete service is not guessed at


def test_discounts_do_not_stack_and_the_largest_applies(price_list):
    p = price_quote(read("E19"), price_list, Adjustments(), TODAY)
    (d,) = [ln for ln in p.lines if ln.kind == "discount"]
    assert d.description == "Weekly service discount (15%)" and d.amount == D("-38.25")


def test_expired_and_unknown_promo_codes_are_not_applied_and_say_so(price_list):
    p = price_quote(read("E18"), price_list, Adjustments(), date(2027, 1, 1))
    assert p.discount == 0 and p.total == D("214.50")
    assert [(c.code, c.blocking) for c in p.checklist] == [("PROMO_INVALID", False)]
    req = RuleExtractor().extract("One-off house clean, 2 bedrooms, 1 bathroom. Promo code FREEBIE99.")
    p = price_quote(req, price_list, Adjustments(), TODAY)
    assert p.discount == 0 and "FREEBIE99 is not in the price list" in p.checklist[0].question


def test_customer_discount_request_is_never_applied_without_a_decision(price_list):
    req = read("E20")
    p = price_quote(req, price_list, Adjustments(), TODAY)
    assert p.discount == 0 and not p.can_approve
    declined = price_quote(req.model_copy(update={"discount_decision": "declined"}), price_list, Adjustments(), TODAY)
    assert declined.can_approve and declined.total == D("209.00")
    granted = price_quote(req.model_copy(update={"discount_decision": "5"}), price_list, Adjustments(), TODAY)
    assert granted.discount == D("9.50") and granted.total == D("198.55")


def test_acknowledging_an_unsupported_service_unblocks_and_lists_the_exclusion(price_list):
    req = read("E13")
    acked = req.model_copy(update={"acknowledged": ["Pest control"]})
    p = price_quote(acked, price_list, Adjustments(), TODAY)
    assert p.can_approve and p.total == D("187.00") and p.excluded == ["Pest control"]


def test_overrides_and_custom_lines_are_marked_manual(price_list):
    adj = Adjustments(overrides={"office_clean.area_sqm": Override(unit_price=D("0.80"))},
                      custom=[CustomLine(description="Key collection", amount=D("15"))])
    p = price_quote(read("E13").model_copy(update={"acknowledged": ["Pest control"]}), price_list, adj, TODAY)
    assert [(ln.code, ln.amount, ln.manual) for ln in p.lines] == [
        ("office_clean.area_sqm", D("160.00"), True), ("custom.1", D("15.00"), True)]
    assert p.total == D("192.50")


def test_minimum_charge(price_list):
    p = price_quote(read("E06"), price_list, Adjustments(), TODAY)
    assert [(ln.code, ln.amount) for ln in p.lines] == [("office_clean.area_sqm", D("76.50")), ("office_clean.minimum", D("43.50"))]


def test_verify_drops_values_whose_quote_is_not_in_the_text():
    text = "We need our office cleaned. It is 80 sqm."
    made_up = Requirements(
        services={"office_clean": Value(value="office_clean", quote="office cleaned", start=12, end=26)},
        fields={"area_sqm": Value(value="800", quote="800 sqm", start=34, end=41),        # text says 80
                "bathrooms": Value(value="2", quote="2 bathrooms", start=0, end=11),      # not in the text at all
                "windows": Value(value="lots", quote="office", start=12, end=18)})        # real quote, bad value
    checked = verify(text, made_up)
    assert list(checked.services) == ["office_clean"] and checked.fields == {}
    assert sorted(checked.rejected_evidence) == ["area_sqm", "bathrooms", "windows"]


def test_verify_keeps_operator_and_form_values():
    req = Requirements(fields={"area_sqm": Value(value="80", source="operator", by="ana")})
    assert verify("anything", req).fields["area_sqm"].value == "80"


def test_draft_asks_the_questions_or_states_only_computed_amounts(price_list):
    req = read("E15")
    p = price_quote(req, price_list, Adjustments(), TODAY)
    text = draft_reply(price_list, req, p, approved=False)
    assert "Which is right?" in text and "$" not in text
    req = read("E13").model_copy(update={"acknowledged": ["Pest control"]})
    p = price_quote(req, price_list, Adjustments(), TODAY)
    assert "No customer wording yet" in draft_reply(price_list, req, p, approved=False)
    text = draft_reply(price_list, req, p, approved=True)
    assert "$187.00" in text and "we don't offer pest control" in text and amounts_ok(text, p)
    assert not amounts_ok("The total is $180.00, as a special price.", p)
    req = read("E12")
    assert "don't offer pressure washing" in draft_reply(price_list, req, price_quote(req, price_list, Adjustments(), TODAY), False)
