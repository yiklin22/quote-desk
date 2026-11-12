# Quote desk

Paste a customer's enquiry. The app reads what they asked for, shows which
words each value came from, lists what is still missing, prices the job from
a versioned price list, and produces a PDF once a person approves it.

Built for one niche: a small cleaning business.

**Status: the seven build milestones are done and tested on invented data.
Two things in the brief are not done, and both need something I do not have:**

- **No model is called anywhere.** Enquiries are read by a pattern-based
  extractor, and reply wording comes from fixed templates. The two gates a
  model's output would have to pass are built and tested (see
  [Where a model plugs in](#where-a-model-plugs-in)), but no model has run.
  That needs an API key and a spending decision.
- **The price list is invented.** The brief says to get one real operator's
  pricing rules. `pricelist_v1.json` is a placeholder I made up so the rest
  could be built. It is labelled fictional in the app and on the PDF.

## Result on the fixture set

20 invented enquiries. Expected totals were worked out by hand from the price
list before the code was run. Output of `scripts/demo.py` against a running
server ([docs/demo-run.txt](docs/demo-run.txt)):

| Kind of enquiry | Count | Result |
|---|---|---|
| Complete request | 8 | priced; all 8 totals equal the hand-worked figure |
| Missing area, bedrooms or bathrooms | 3 | blocked, with the question to ask |
| Asks for a service not offered | 3 | blocked until the operator agrees to leave it out; one has nothing quotable |
| Contradicts itself (two areas, two bedroom counts, two frequencies) | 3 | blocked until the operator picks one |
| Discounts (promo code; promo plus weekly; "25% off for cash") | 3 | two priced to the hand-worked figure; the cash request waits for a decision |

20 of 20 behaved as expected. The fixtures were written by me, alongside the
patterns that read them, so this shows the pricing and blocking rules work.
It says nothing about how often the extractor reads a real email correctly.

## Run it

Needs [uv](https://docs.astral.sh/uv/). Without `DATABASE_URL` the app starts
its own PostgreSQL from the `pgserver` package in `./.pgdata`.

```bash
uv sync
uv run uvicorn quotedesk.app:app --port 8812      # open http://127.0.0.1:8812
uv run python scripts/demo.py http://127.0.0.1:8812
uv run pytest                                     # 69 tests, on a real PostgreSQL
```

## The screens

1. **Enquiry**: paste an email, or fill in a form, or both (form values win).
2. **Clarification checklist**: each open item with its own control: type the
   answer, agree to leave a service out, or grant or decline a discount.
3. **Editable estimate**: every requirement, quantity and unit price can be
   changed, and extra lines added. Changed lines are marked "edited".
4. **Approval**: one button, enabled only when the checklist is clear. It
   names the revision and the total being approved.
5. **PDF**: available only for the approved revision.

Beside the estimate, the enquiry text is shown with the words behind each
value highlighted, and a reply draft that the operator copies into their own
email. The layout is one column on a phone, two from 1000 px.

## Acceptance claims and the tests behind them

| Claim | Test |
|---|---|
| Identical inputs reproduce totals | `test_domain.py::test_identical_inputs_reproduce_lines_totals_and_hash`, `test_service.py::test_same_enquiry_twice_gives_the_same_lines_and_total` |
| Edits invalidate a previous approval | `test_service.py::test_any_edit_invalidates_the_approval` (five kinds of edit), `test_changing_a_value_back_does_not_restore_the_approval` |
| Nothing sends automatically | `test_service.py::test_the_code_has_no_way_to_send_anything` (the package imports no mail, HTTP-client or model library), `test_web.py::test_enquiry_to_pdf_flow` |
| Missing information blocks a final quote | `test_service.py::test_missing_information_blocks_approval_and_pdf_until_provided` |
| Code calculates prices | `test_domain.py::test_fixture_matches_hand_worked_expectation` (20 cases) |
| Every extracted value quotes the enquiry | `test_domain.py::test_every_extracted_value_quotes_the_enquiry`, `test_verify_drops_values_whose_quote_is_not_in_the_text` |
| An old quote keeps its prices when the list changes | `test_service.py::test_new_price_list_does_not_change_existing_quotes_until_repriced` |

## Design decisions

- **Approval belongs to a revision, not to a quote.** The revision number goes
  up whenever anything a price depends on changes. A quote is approved only if
  its newest approval names the current revision. Changing a value and
  changing it back gives a new revision, so the old approval does not return.
  Rejected alternative: comparing content hashes, which would silently
  re-approve a quote that was edited twice.
- **A request is priced or it is blocked, never guessed.** A service with a
  missing or contradictory input produces no line. The estimate shows what can
  be priced, labelled incomplete, and cannot be approved.
- **A customer cannot give themselves a discount.** A code on the price list is
  applied by rule. "Can you do 25% off?" becomes a checklist item that an
  operator must grant or decline. Discounts do not stack; the largest applies.
- **Price lists are immutable versions.** Publishing prices adds a version. A
  quote records the version and the date it was priced with (promo expiry
  depends on the date), so it can be reproduced later.
- **There is no send function.** Not sending is not a setting. The package has
  no mail or HTTP client, and a test fails if one is imported.
- **Evidence is checked by code.** `verify` drops any extracted value whose
  quoted words are not in the enquiry at the stated position, or whose value
  is not sane for the field, and tells the operator what was dropped.

## Where a model plugs in

Nothing below has been run against a model.

- **Reading the enquiry.** Anything with `extract(text) -> Requirements`
  satisfies the `Extractor` protocol. Its output goes through `verify`, so a
  value the model invented, or a real quote with a wrong number, never reaches
  pricing. The test feeds `verify` three such fabricated values.
- **Wording the reply.** `amounts_ok(text, priced)` rejects a draft containing
  any money amount the pricing code did not produce. A model draft that fails
  falls back to the template.

The first measurement to make with a key: on real enquiries, how many values
does the model get right that the patterns miss, and how many does `verify`
have to drop.

## Dependencies checked before use

Pinned in `uv.lock`: fastapi 0.162.5 (MIT), uvicorn 0.54.0 (BSD-3-Clause),
sqlalchemy 2.1.8 (MIT), psycopg 3.3.9 (LGPL-3.0-only, used unmodified as a
library), pydantic 2.14.3 (MIT), jinja2 3.1.6 (BSD), python-multipart 0.0.33
(Apache-2.0), reportlab 5.1.0 (BSD) for the PDF, pgserver 0.1.4 (Apache-2.0)
for a bundled PostgreSQL. No existing quoting project is reused; the two
invoicing applications in the brief (InvoiceShelf, AGPL-3.0; Invoice Ninja,
source-available) were not needed for this scope and bring license duties.

## What this does not show

- **Real enquiries.** The extractor is a baseline tuned on 20 messages I
  wrote. Expect it to miss ordinary phrasings.
- **A real operator's rules.** Travel charges, access, pets, condition of the
  property and judgement calls are not modelled.
- **Real users.** Nobody has timed a quote or counted corrections.
- **Access control.** No login; the operator name is a cookie. Not for the
  internet as it is.
- **The PDF's look.** Its text is tested; I have not inspected it by eye.

## Next steps

1. Get one operator's price list and 30 of their past enquiries with the
   quotes they actually sent; replace the fixtures.
2. Add a model extractor and drafter behind the two gates, and measure them.
3. Login, per-account data, deletion and usage caps, then a hosted demo.

## Layout

```
quotedesk/
  domain.py          requirements, price list, checklist, pricing, inputs hash (pure)
  extraction.py      pattern extractor and the evidence gate
  drafting.py        reply templates and the amounts gate
  pdf.py             approved quote -> PDF
  models.py          tables: enquiry, price list version, quote, lines, approval, event
  service.py         use cases
  app.py, web/       FastAPI routes, pages, stylesheet
  pricelist_v1.json  invented placeholder prices
fixtures/enquiries.json   20 enquiries with hand-worked expectations
scripts/demo.py           drives a running server through the flow
tests/                    69 tests
```
