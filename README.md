# Quote desk

Paste a customer's enquiry. The app reads what they asked for, shows which
words each value came from, lists what is still missing, prices the job from
a versioned price list, and produces a PDF once a person approves it.

Built for one niche: a small cleaning business.

**Status: the seven build milestones are done and tested on invented data,
and milestone 8 added login, per-account data, deletion, input limits and
usage caps. It runs on this machine only and is not hosted anywhere. Two
things in the brief are not done, and both need something I do not have:**

- **The model has only read invented enquiries.** Milestone 9 added a model
  step that reads the enquiry (see [The model step](#the-model-step)). It is
  off by default. A pilot on 13 November 2026 read ten of the fixture
  enquiries correctly; no real customer email has been through it. Until it
  is switched on, enquiries are read by a pattern-based extractor. Milestone
  10 added a button that has the model reword the reply; the templates stay
  the default and the fallback.
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

The same run then checks the account boundary on the live server: a second
account sees no enquiries, the first account's enquiry and PDF answer 404 to
it, as do its attempts to edit, approve and delete that enquiry; a delete sent
from another site answers 403; and deleting both accounts ends their logins.

## Run it

Needs [uv](https://docs.astral.sh/uv/). Without `DATABASE_URL` the app starts
its own PostgreSQL from the `pgserver` package in `./.pgdata`.

```bash
uv sync
uv run uvicorn quotedesk.app:app --port 8812      # open http://127.0.0.1:8812 and create an account
uv run python scripts/demo.py http://127.0.0.1:8812   # two throwaway accounts, deleted at the end
uv run pytest                                     # 117 tests, on a real PostgreSQL
```

To try it by hand, create an account and press **Load the sample enquiries**
on the Account page.

`DATABASE_URL` must point at an empty database. There are no migrations: the
tables are created on first start, and a database from before accounts existed
is refused with a message. The bundled PostgreSQL keeps this version's data in
a database named `quotedesk_v2`; whatever an earlier version stored in
`.pgdata` is left alone and no longer shown.

## The screens

0. **Create an account / log in.** Everything below belongs to the account
   that is logged in, including its price list. The **Account** page shows
   usage against each limit, loads the sample enquiries, and deletes all data
   or the whole account (both ask for the password again).
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
| Nothing sends automatically | `test_service.py::test_the_code_has_no_way_to_send_anything` (no module imports a mail or HTTP-client library; only `model.py` may import the Anthropic SDK), `test_web.py::test_enquiry_to_pdf_flow` |
| Missing information blocks a final quote | `test_service.py::test_missing_information_blocks_approval_and_pdf_until_provided` |
| Code calculates prices | `test_domain.py::test_fixture_matches_hand_worked_expectation` (20 cases) |
| Every extracted value quotes the enquiry | `test_domain.py::test_every_extracted_value_quotes_the_enquiry`, `test_verify_drops_values_whose_quote_is_not_in_the_text` |
| An old quote keeps its prices when the list changes | `test_service.py::test_new_price_list_does_not_change_existing_quotes_until_repriced` |
| Nothing is readable or changeable without a login | `test_accounts.py::test_every_page_needs_a_login` |
| One account cannot read or change another's enquiries, quotes or PDFs | `test_accounts.py::test_another_account_sees_and_changes_none_of_it`, `test_service_refuses_another_accounts_enquiry` |
| Each account prices from its own price list and numbers its own quotes | `test_accounts.py::test_each_account_has_its_own_price_list_and_quote_numbers` |
| Deleting an enquiry, all data, or an account removes all of it, and only that account's | `test_accounts.py::test_delete_one_enquiry`, `test_delete_all_data_needs_the_password_and_leaves_the_account`, `test_delete_account_removes_everything_and_the_login` |
| Caps hold: enquiries held, new enquiries per day, price lists per day, sizes | `test_accounts.py::test_cap_on_enquiries_held`, `test_daily_cap_is_not_reset_by_deleting`, `test_caps_on_price_lists_and_extra_lines`, `test_oversized_and_unsized_requests_are_refused_unread` |
| Repeated wrong passwords lock the login for a while; logout and expiry end a session | `test_accounts.py::test_wrong_password_then_throttle`, `test_logout_and_expiry_end_the_session` |
| A form on another site cannot act with a visitor's login | `test_accounts.py::test_writes_from_another_site_are_refused` |

For the account tests I broke each rule on purpose, one at a time (enquiry
lookup, the price list an account reads, the home page list, the cross-site
check, the daily cap), and each time at least one test failed.

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
  no mail or HTTP client, and a test fails if one is imported. The one
  outbound call is in `model.py`: when the model step is switched on, the
  enquiry text goes to the Anthropic API to be read. Nothing goes to the
  customer.
- **The account is checked twice.** Enquiries and price lists carry an
  `account_id`; quotes, lines, approvals and history hang off their enquiry.
  The web layer answers 404 for another account's enquiry, the same as for one
  that does not exist, and every service function takes the account and checks
  it again.
- **The price list belongs to the account.** A new account starts with the
  placeholder as its own version 1, and publishing affects nobody else. Quote
  numbers count from 1 within an account, so they say nothing about how many
  other customers there are.
- **Sessions are random tokens kept server-side**, with only their SHA-256 in
  the database, so logout ends a session. Passwords are scrypt hashes from the
  standard library. Rejected alternative: a signed cookie, which cannot be
  revoked. No new dependency was added.
- **Cross-site writes are refused by where the request came from**
  (`Sec-Fetch-Site`, or `Origin` when that is absent), and the cookie is
  `SameSite=Lax`.
- **The daily cap is the future spending cap.** Every new enquiry is counted
  per account per day, and deleting does not give the count back. One enquiry
  is one read. Model calls have their own counter and cap
  (`MAX_MODEL_CALLS_PER_DAY`, 50 per account); past it, enquiries are read by
  patterns. An enquiry refused for its content uses none.
- **Evidence is checked by code.** `verify` drops any extracted value whose
  quoted words are not in the enquiry at the stated position, or whose value
  is not sane for the field, and tells the operator what was dropped.

## The model step

**Run against the real model once, on invented enquiries only.** The 12 tests
in `tests/test_model.py` use a stand-in client that returns fixed output. The
pilot below was run on 13 November 2026 with `claude-opus-5-5` at low effort:
all ten fixture enquiries were read as the fixtures expect, and the evidence
check dropped nothing. The patterns already read these ten correctly, so this
shows the step works end to end, not that the model is better on real email.

- **What it does.** With `QUOTEDESK_MODEL_EXTRACTION=1` and
  `ANTHROPIC_API_KEY` in the server's environment, a pasted enquiry is read by
  Claude (`claude-opus-5-5`, low effort, structured output) instead of the
  patterns. The call is made on the server; the browser never sees the key.
- **What the model may say.** A list of items: a service code, a field value,
  a request that is not offered, or a discount request, each with the words it
  was read from. No offsets and no prices.
- **What code checks.** `to_requirements` looks each quote up in the enquiry
  itself. An item is dropped, and listed for the operator, when its words are
  not in the enquiry, when a number is not in the words quoted, or when the
  service or field does not exist. Then `verify` runs as for the patterns.
- **When it fails.** A refusal, a timeout, an API error or a cut-off answer
  falls back to the patterns, and the enquiry's history says so. If a safety
  classifier declines a request, the API is asked to rerun it on another model
  (`fallbacks="default"`) before the app gives up on the model.
- **What it costs.** In the pilot, about $0.009 per enquiry (about 1,300
  tokens in and 120 to 240 out) and 2 to 6 seconds per call; ten enquiries
  came to an estimated $0.088. These are estimates from token counts at list
  price, not a bill. `scripts/model_pilot.py` reads ten fixture enquiries,
  prints tokens and estimated dollars per call, and stops at a spend limit
  (default $0.50):

  ```bash
  echo 'ANTHROPIC_API_KEY=...' > .env               # .env is git-ignored
  uv run --env-file .env python scripts/model_pilot.py
  QUOTEDESK_MODEL_EXTRACTION=1 uv run --env-file .env uvicorn quotedesk.app:app --port 8812
  ```

Known limits: the model call runs inside the database transaction that holds
the account's cap lock, so one account's enquiries queue behind a slow call
(up to the 30-second timeout). Enquiry text is customer data leaving the
machine; an operator would need to agree to that.

### Rewording the reply

The reply draft is written by templates from the computed figures, as before.
With the model step on, the enquiry page has a **Reword with the model**
button. It sends the enquiry and the standard reply to the model and asks for
the same reply in friendlier words. Nothing is reworded unless the operator
presses the button, and the result is still only text to copy.

- **What code checks** (`drafting.rewrite_problems`): every dollar amount is
  one the pricing code produced; every amount and the validity period from
  the standard reply are still there; no number appears that was not in the
  standard reply; each question for the customer is word for word; the text
  is not empty or over 2,000 characters. If any check fails, the standard
  reply stays and the history records why.
- **What code cannot check:** the words themselves. In the pilot the model
  added "sounds very manageable" and, on another run, "sounds like a great
  fit for us" about a customer's office. Neither is a figure, so neither is
  caught. The page labels model wording and tells the operator to read it.
  A promise written without digits ("we can start tomorrow") would also pass.
- **When the quote changes:** the model's wording is stored with a hash of
  the standard reply it was written from, and shown only while that reply is
  unchanged. An edit that withdraws the approval withdraws the wording too.
- **Pilot, 13 November 2026:** `scripts/reply_pilot.py` reworded four fixture
  replies (a quote, a missing value, a service not offered, a contradiction).
  All four passed the checks; about $0.006 and 3 seconds each, $0.025
  estimated in total. Whether the wording is better than the template is a
  judgement nobody but me has made, on four invented enquiries.
- **One switch for both.** `QUOTEDESK_MODEL_EXTRACTION=1` turns on reading
  and rewording, and both count against the same daily cap.

The first measurement to make with real enquiries: how many values does the
model get right that the patterns miss, and how many does the evidence check
have to drop.

## Dependencies checked before use

Pinned in `uv.lock`: fastapi 0.162.5 (MIT), uvicorn 0.54.0 (BSD-3-Clause),
sqlalchemy 2.1.8 (MIT), psycopg 3.3.9 (LGPL-3.0-only, used unmodified as a
library), pydantic 2.14.3 (MIT), jinja2 3.1.6 (BSD), python-multipart 0.0.33
(Apache-2.0), reportlab 5.1.0 (BSD) for the PDF, pgserver 0.1.4 (Apache-2.0)
for a bundled PostgreSQL, anthropic 2.8.0 (Apache-2.0 per its package
metadata) for the model step; it installs and imports, but no sample output
exists until a key is available. No existing quoting project is reused; the two
invoicing applications in the brief (InvoiceShelf, AGPL-3.0; Invoice Ninja,
source-available) were not needed for this scope and bring license duties.

## What this does not show

- **Real enquiries.** The extractor is a baseline tuned on 20 messages I
  wrote. Expect it to miss ordinary phrasings.
- **A real operator's rules.** Travel charges, access, pets, condition of the
  property and judgement calls are not modelled.
- **Real users.** Nobody has timed a quote or counted corrections.
- **A hosted deployment.** Everything ran on one machine over plain HTTP.
  HTTPS, a proxy, backups and monitoring are not set up, and nobody but me has
  reviewed the access control.
- **The rest of account handling.** No email verification, no password reset,
  no way to change a password, and one login per account (no teams or roles).
  A user who forgets the password loses the account.
- **Schema changes.** There are no migrations, so changing a table means a new
  empty database.
- **How anything looks.** The PDF's text and the pages' HTML are tested; this
  machine has no browser, so I have not seen a screen or the PDF by eye.

## Limits and settings for a hosted demo

All are environment variables; the defaults are in `quotedesk/config.py`.

| Variable | Default | Meaning |
|---|---|---|
| `QUOTEDESK_INVITE_CODE` | empty | When set, creating an account needs this code. **Set it before anyone else can reach the server**; without it signup is open. |
| `QUOTEDESK_MAX_ACCOUNTS` | 25 | Accounts that can exist |
| `QUOTEDESK_MAX_ENQUIRY_CHARS` | 20,000 | One pasted enquiry |
| `QUOTEDESK_MAX_REQUEST_BYTES` | 500 kB | One request, refused from its `Content-Length` before the body is read |
| `QUOTEDESK_MAX_PRICE_LIST_BYTES` | 100 kB | One published price list |
| `QUOTEDESK_MAX_CUSTOM_LINES` | 50 | Extra lines on one quote |
| `QUOTEDESK_MAX_ENQUIRIES` | 500 | Enquiries an account holds at one time |
| `QUOTEDESK_MAX_ENQUIRIES_PER_DAY` | 100 | New enquiries an account can add per UTC day |
| `QUOTEDESK_MAX_PRICE_LISTS_PER_DAY` | 20 | Price list versions an account can publish per UTC day |
| `QUOTEDESK_SESSION_DAYS` | 14 | How long a login lasts |
| `QUOTEDESK_COOKIE_SECURE` | off | `1` forces the `Secure` cookie flag. A request that arrives as HTTPS gets it anyway. |

A login is refused for 15 minutes after 5 wrong passwords for one email, or 30
from one address. Behind a proxy, run uvicorn with `--proxy-headers` so the
address and the HTTPS scheme are the visitor's, not the proxy's.

## Next steps

1. Get one operator's price list and 30 of their past enquiries with the
   quotes they actually sent; replace the fixtures.
2. Run the model pilot with an API key, then measure the model against the
   patterns on real enquiries. Add a model drafter behind `amounts_ok`.
3. Put it on a host with HTTPS and an invite code. This needs a choice of
   host and a PostgreSQL there; nothing in the code depends on which.
4. Password reset and email verification, which need a way to send email.
   That conflicts with "there is no send function" as tested today, so the
   test would have to allow one named module and nothing else.

## Layout

```
quotedesk/
  domain.py          requirements, price list, checklist, pricing, inputs hash (pure)
  extraction.py      pattern extractor and the evidence gate
  model.py           model reading of an enquiry and rewording of a reply; the only outbound calls
  drafting.py        reply templates and the checks a reworded reply must pass
  pdf.py             approved quote -> PDF
  models.py          tables: account, enquiry, price list version, quote, lines, approval, model reply, event
  auth.py            passwords, login sessions, login throttling
  config.py          limits and caps
  service.py         use cases, all per account
  app.py, web/       FastAPI routes, pages, stylesheet
  pricelist_v1.json  invented placeholder prices
fixtures/enquiries.json   20 enquiries with hand-worked expectations
scripts/demo.py           drives a running server through the flow with two accounts
scripts/model_pilot.py    reads ten fixtures with the real model; spends money, has a limit
scripts/reply_pilot.py    rewords four fixture replies with the real model; spends money, has a limit
tests/                    117 tests
```
