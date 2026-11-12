"""The model reading step, tested with a stand-in client. No test calls a real model."""
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from sqlalchemy import select

from conftest import ACC, BY_ID, TODAY
from quotedesk import config, model, service
from quotedesk.domain import Adjustments, price_quote
from quotedesk.models import UsageCounter

E01 = BY_ID["E01"]["text"]
SERVICES = {"office_clean": "Office clean", "house_clean": "House clean"}


def item(kind, name, value, quote):
    return model.Item(kind=kind, name=name, value=value, quote=quote)


GOOD = model.Reading(items=[
    item("service", "office_clean", "office_clean", "office cleaned"),
    item("field", "area_sqm", "180", "180 sqm"),
    item("field", "frequency", "weekly", "weekly"),
    item("field", "customer_email", "priya@nairpartners.example", "priya@nairpartners.example"),
    item("field", "customer_name", "Priya Nair", "Priya Nair"),
])


class FakeClient:
    """Stands in for anthropic.Anthropic: records the request and returns a fixed reading."""

    def __init__(self, reading=GOOD, stop_reason="end_turn", error=None):
        self.reading, self.stop_reason, self.error, self.calls = reading, stop_reason, error, []
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self.parse))

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(parsed_output=self.reading, stop_reason=self.stop_reason,
                               usage=SimpleNamespace(input_tokens=700, output_tokens=200))


def test_a_correct_reading_is_cited_and_prices_like_the_fixture(price_list):
    req = model.to_requirements(E01, GOOD, set(SERVICES))
    assert not req.rejected_evidence and sorted(req.services) == ["office_clean"]
    for v in [*req.fields.values(), *req.services.values()]:
        assert E01[v.start:v.end] == v.quote
    assert str(price_quote(req, price_list, Adjustments(), TODAY).total) == BY_ID["E01"]["expect"]["total"]


def test_made_up_values_never_reach_pricing(price_list):
    reading = model.Reading(items=[
        item("service", "office_clean", "office_clean", "office cleaned"),
        item("field", "area_sqm", "250", "180 sqm"),                  # real words, wrong number
        item("field", "bathrooms", "2", "two bathrooms"),             # words that are not in the enquiry
        item("field", "frequency", "daily", "weekly"),                # not a frequency the price list knows
        item("field", "budget", "500", "quote"),                      # not a field
        item("service", "roof_clean", "roof_clean", "office"),        # not a service code
        item("discount_request", "discount", "discount", "20% off please"),
    ])
    req = model.to_requirements(E01, reading, set(SERVICES))
    assert req.fields == {} and req.discount_request is None and sorted(req.services) == ["office_clean"]
    assert sorted(req.rejected_evidence) == ["area_sqm", "bathrooms", "budget", "discount_request", "frequency",
                                             "service roof_clean"]
    priced = price_quote(req, price_list, Adjustments(), TODAY)
    assert not priced.can_approve                                     # the area is asked for, not guessed


def test_two_values_for_one_field_are_a_conflict_and_number_words_count():
    text = "Three bedroom house clean please. Actually it is 4 bedrooms, and 2 bathrooms."
    req = model.to_requirements(text, model.Reading(items=[
        item("service", "house_clean", "house_clean", "house clean"),
        item("field", "bedrooms", "3", "Three bedroom"),
        item("field", "bedrooms", "4", "4 bedrooms"),
        item("field", "bathrooms", "2", "2 bathrooms"),
    ]), set(SERVICES))
    assert sorted(v.value for v in req.conflicts["bedrooms"]) == ["3", "4"]
    assert req.fields["bathrooms"].value == "2" and "bedrooms" not in req.fields and not req.rejected_evidence


def test_the_request_carries_the_enquiry_as_data_and_asks_for_no_prices():
    client = FakeClient()
    extractor = model.ModelExtractor(SERVICES, client=client, model="some-model")
    req = extractor.extract(E01)
    call = client.calls[0]
    assert call["model"] == "some-model" and call["output_format"] is model.Reading
    assert call["messages"] == [{"role": "user", "content": f"<enquiry>\n{E01}\n</enquiry>"}]
    assert "office_clean: Office clean" in call["system"] and "$" not in call["system"]
    assert req.fields["area_sqm"].value == "180" and extractor.usage.output_tokens == 200


@pytest.mark.parametrize("client", [
    FakeClient(stop_reason="refusal"), FakeClient(stop_reason="max_tokens"),
    FakeClient(error=anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com"))),
    FakeClient(error=ValueError("output did not match the schema")),
], ids=["refusal", "max_tokens", "connection", "bad output"])
def test_every_model_failure_is_one_error_the_caller_can_fall_back_from(client):
    with pytest.raises(model.ModelError):
        model.ModelExtractor(SERVICES, client=client).extract(E01)


# --- wired into enquiry creation -------------------------------------------------

def calls_today(session):
    return session.scalar(select(UsageCounter.count).where(UsageCounter.account_id == ACC,
                                                           UsageCounter.kind == "model_calls")) or 0


def use_model(monkeypatch, client):
    monkeypatch.setattr(config, "MODEL_EXTRACTION", True)
    real = model.ModelExtractor
    monkeypatch.setattr(model, "ModelExtractor", lambda services: real(services, client=client))


def test_the_model_is_off_unless_switched_on(session, monkeypatch):
    monkeypatch.setattr(model, "ModelExtractor", lambda services: pytest.fail("the model was used"))
    enq = service.create_enquiry(session, ACC, E01, "ana", today=TODAY)
    assert enq.extractor == "RuleExtractor" and calls_today(session) == 0


def test_switched_on_the_model_reads_the_enquiry_and_the_call_is_counted(session, monkeypatch):
    client = FakeClient()
    use_model(monkeypatch, client)
    enq = service.create_enquiry(session, ACC, E01, "ana", today=TODAY)
    assert enq.extractor == "ModelExtractor" and len(client.calls) == 1 and calls_today(session) == 1
    assert str(enq.quote.total) == BY_ID["E01"]["expect"]["total"]
    assert service.requirements(enq).text("customer_name") == "Priya Nair"      # the patterns do not read names
    assert service.usage(session, ACC)["model_calls_today"] == 1


def test_a_failed_model_call_falls_back_to_patterns_and_says_so(session, monkeypatch):
    use_model(monkeypatch, FakeClient(stop_reason="refusal"))
    enq = service.create_enquiry(session, ACC, E01, "ana", today=TODAY)
    assert enq.extractor == "RuleExtractor" and str(enq.quote.total) == BY_ID["E01"]["expect"]["total"]
    assert "model reading failed" in enq.events[0].detail and calls_today(session) == 1


def test_past_the_daily_cap_no_model_call_is_made(session, monkeypatch):
    client = FakeClient()
    use_model(monkeypatch, client)
    monkeypatch.setattr(config, "MAX_MODEL_CALLS_PER_DAY", 1)
    service.create_enquiry(session, ACC, E01, "ana", today=TODAY)
    second = service.create_enquiry(session, ACC, E01, "ana", today=TODAY)
    assert len(client.calls) == 1 and calls_today(session) == 1
    assert second.extractor == "RuleExtractor" and "model daily limit reached" in second.events[0].detail
