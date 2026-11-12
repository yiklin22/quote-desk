"""Turn enquiry text into requirements, each with the exact words it came from.

`RuleExtractor` is a keyword-and-pattern baseline. It was written against
the 20 fixture enquiries and will miss phrasings it has not seen. Any
extractor, rule-based or a model, goes through `verify`, which drops every
value whose quoted source is not present in the enquiry at the stated place.
"""
from __future__ import annotations

import re
from typing import Protocol

from .domain import FREQUENCIES, INT_FIELDS, Requirements, Value

WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
         "nine": 9, "ten": 10}
N = r"(\d{1,4}|one|two|three|four|five|six|seven|eight|nine|ten)"

FIELD_PATTERNS = {
    "area_sqm": rf"(\d{{1,5}}(?:\.\d+)?)\s*(?:sqm\b|sq\.?\s?m\b|m2\b|m²|square\s+met(?:re|er)s?\b)",
    "bedrooms": rf"\b{N}[\s-]*bed(?:room)?s?\b",
    "bathrooms": rf"\b{N}[\s-]*bath(?:room)?s?\b",
    "windows": rf"\b{N}\s+windows?\b",
    "carpet_rooms": rf"\bcarpet\w*(?:\s+\w+){{0,3}}?\s+(?:in|for)\s+{N}\s+(?:bed)?rooms?\b"
                    rf"|\b{N}\s+carpeted\s+(?:bed)?rooms?\b",
}
FREQUENCY_PATTERNS = {
    "one_off": r"\b(?:one[- ]off|once[- ]off|one[- ]time|just once)\b",
    "weekly": r"\b(?:weekly|every week|each week|once a week)\b",
    "fortnightly": r"\b(?:fortnightly|every two weeks|every second week|every fortnight)\b",
    "monthly": r"\b(?:monthly|every month|once a month)\b",
}
SERVICE_PATTERNS = {
    "office_clean": r"\boffices?\b[^.\n]{0,40}\bclean|\bclean\w*[^.\n]{0,40}\boffices?\b|\bcommercial clean",
    "end_of_lease": r"\bend[- ]of[- ]lease\b|\bbond clean|\bvacate clean|\bmove[- ]out clean|\bexit clean",
    "house_clean": r"\b(?:house|home|apartment|unit|flat)\s+clean|\bregular clean|\bdomestic clean"
                   r"|\bclean\w*\s+(?:of\s+)?(?:my|our|the)\s+(?:house|home|apartment|unit|flat)\b",
    "carpet_steam": r"\bcarpet\w*\b[^.\n]{0,30}\b(?:steam|clean)|\bsteam clean\w*[^.\n]{0,30}\bcarpet",
    "window_clean": r"\bwindows?\b[^.\n]{0,30}\bclean|\bclean\w*[^.\n]{0,30}\bwindows?\b",
    "oven_clean": r"\boven\b",
}
UNSUPPORTED_PATTERNS = {
    "Pressure washing": r"\bpressure[- ]wash\w*|\bhigh[- ]pressure clean\w*",
    "Pest control": r"\bpest control\b|\btermites?\b",
    "Gutter cleaning": r"\bgutters?\b",
    "Mould remediation": r"\bmou?ld\b",
    "Garden or lawn care": r"\blawns?\b|\bgardening\b|\bmowing\b",
    "Rubbish removal": r"\b(?:rubbish|junk) removal\b",
}
WEEKEND = r"\b(?:saturdays?|sundays?|weekends?)\b"
PROMO = r"(?i:promo\s+code|code|promo|coupon)[:\s]+([A-Z][A-Z0-9]{3,11})\b"
CONCESSION = r"\b(?:pensioner|seniors?\s+card|concession\s+card)\b"
DISCOUNT_ASK = (r"\b\d{1,2}\s?%\s*(?:off|discount)\b"
                r"|\b(?:do|offer|get|give)\s+(?:me\s+|us\s+)?(?:a|any)\s+(?:discount|better price|deal)\b")
EMAIL = r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"


class Extractor(Protocol):
    def extract(self, text: str) -> Requirements: ...


def _value(text: str, m: re.Match, value: str) -> Value:
    return Value(value=value, quote=text[m.start():m.end()], start=m.start(), end=m.end())


def _number(m: re.Match) -> str:
    raw = next(g for g in m.groups() if g)
    return str(WORDS.get(raw.lower(), raw))


class RuleExtractor:
    def extract(self, text: str) -> Requirements:
        fields: dict[str, Value] = {}
        conflicts: dict[str, list[Value]] = {}

        def put(name: str, found: list[Value]) -> None:
            distinct: dict[str, Value] = {}
            for v in found:
                distinct.setdefault(v.value, v)
            if len(distinct) == 1:
                fields[name] = next(iter(distinct.values()))
            elif len(distinct) > 1:
                conflicts[name] = list(distinct.values())

        for name, pattern in FIELD_PATTERNS.items():
            put(name, [_value(text, m, _number(m)) for m in re.finditer(pattern, text, re.I)])
        put("frequency", [_value(text, m, freq) for freq, pattern in FREQUENCY_PATTERNS.items()
                          for m in re.finditer(pattern, text, re.I)])
        for name, pattern, value in (("weekend", WEEKEND, "yes"), ("concession", CONCESSION, "pensioner")):
            if m := re.search(pattern, text, re.I):
                fields[name] = _value(text, m, value)
        if m := re.search(PROMO, text):
            fields["promo_code"] = Value(value=m.group(1), quote=m.group(1), start=m.start(1), end=m.end(1))
        if m := re.search(EMAIL, text):
            fields["customer_email"] = _value(text, m, m.group(0))

        services = {code: _value(text, m, code) for code, pattern in SERVICE_PATTERNS.items()
                    if (m := re.search(pattern, text, re.I))}
        if "end_of_lease" in services:
            services.pop("house_clean", None)       # an exit clean replaces a regular clean of the same home
        unsupported = {label: _value(text, m, label) for label, pattern in UNSUPPORTED_PATTERNS.items()
                       if (m := re.search(pattern, text, re.I))}
        ask = re.search(DISCOUNT_ASK, text, re.I)
        return verify(text, Requirements(
            services=services, unsupported=unsupported, fields=fields, conflicts=conflicts,
            discount_request=_value(text, ask, ask.group(0)) if ask else None))


def _valid(name: str, v: Value) -> bool:
    if name in INT_FIELDS:
        return v.value.isdigit() and 0 < int(v.value) < 1000
    if name == "area_sqm":
        return bool(re.fullmatch(r"\d{1,5}(\.\d+)?", v.value)) and float(v.value) > 0
    if name == "frequency":
        return v.value in FREQUENCIES
    return bool(v.value.strip())


def _grounded(text: str, v: Value) -> bool:
    """True when the quoted words really are in the enquiry where claimed."""
    if v.source != "extracted":
        return True
    return (v.quote is not None and v.start is not None and v.end is not None
            and 0 <= v.start < v.end <= len(text) and text[v.start:v.end] == v.quote)


def verify(text: str, req: Requirements) -> Requirements:
    """Drop every extracted value that is not backed by the enquiry text or
    is not a sane value for its field, and record what was dropped. This is
    the gate a model's output must pass before it can affect a price."""
    rejected = list(req.rejected_evidence)

    def keep(name: str, v: Value, check_value: bool = True) -> bool:
        ok = _grounded(text, v) and (not check_value or _valid(name, v))
        if not ok and name not in rejected:
            rejected.append(name)
        return ok

    fields = {k: v for k, v in req.fields.items() if keep(k, v)}
    conflicts = {}
    for name, values in req.conflicts.items():
        good = [v for v in values if keep(name, v)]
        if len({v.value for v in good}) > 1:
            conflicts[name] = good
        elif good:
            fields.setdefault(name, good[0])
    request = req.discount_request
    if request is not None and not keep("discount_request", request, check_value=False):
        request = None
    return req.model_copy(update={
        "services": {k: v for k, v in req.services.items() if keep(f"service {k}", v, check_value=False)},
        "unsupported": {k: v for k, v in req.unsupported.items() if keep(f"unsupported {k}", v, check_value=False)},
        "fields": fields, "conflicts": conflicts, "discount_request": request, "rejected_evidence": rejected})
