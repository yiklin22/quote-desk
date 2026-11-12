"""Drive a running server through the whole flow and print what happened.

Start the app first:  uv run uvicorn quotedesk.app:app --port 8812
Then:                 uv run python scripts/demo.py http://127.0.0.1:8812

It creates two throwaway accounts, runs the fixtures in the first, checks
that the second can see and change none of it, and deletes both at the end.
If the server needs an invite code, set QUOTEDESK_INVITE_CODE here too.
"""
import json
import os
import re
import secrets
import sys
from pathlib import Path

import httpx

FIXTURES = json.loads((Path(__file__).parent.parent / "fixtures" / "enquiries.json").read_text())


def status_of(html: str) -> str:
    return re.search(r'class="badge st-(\w+)"', html).group(1)


def total_of(html: str) -> str:
    return re.search(r'<tr class="sum total">.*?<td class="num">([\d.]+)</td>', html, re.S).group(1)


def signed_up(base: str, name: str) -> tuple[httpx.Client, str]:
    client = httpx.Client(base_url=base, follow_redirects=True, timeout=30)
    password = secrets.token_urlsafe(12)
    r = client.post("/signup", data={"name": name, "email": f"demo-{secrets.token_hex(4)}@example.test",
                                     "password": password, "invite": os.environ.get("QUOTEDESK_INVITE_CODE", "")})
    if "Account created" not in r.text:
        sys.exit(f"could not create an account for {name}: HTTP {r.status_code}")
    return client, password


def main(base: str) -> int:
    wrong = 0
    anonymous = httpx.get(base + "/", follow_redirects=False)
    print("without a login, the home page answers:", anonymous.status_code, "->", anonymous.headers.get("location"), "\n")
    c, password = signed_up(base, "demo")
    other, other_password = signed_up(base, "Second account")
    try:
        print(f"{'id':4} {'category':14} {'status':13} {'total':>8}  {'expected':>8}  checklist")
        ids = {}
        for e in FIXTURES["enquiries"]:
            r = c.post("/enquiries", data={"text": e["text"]})
            ids[e["id"]] = str(r.url).split("/enquiries/")[1].split("?")[0]
            status, total, want = status_of(r.text), total_of(r.text), e["expect"]
            open_items = len(re.findall(r'<article class="issue sev-high">', r.text))
            ok = (status == "ready" and total == want["total"]) if want["total"] else \
                (status in ("needs_info", "cannot_quote") and open_items == len(want["blockers"]))
            wrong += not ok
            print(f"{e['id']:4} {e['category']:14} {status:13} {total if want['total'] else '-':>8}  "
                  f"{want['total'] or '-':>8}  {open_items} blocking{'' if ok else '   <-- NOT AS EXPECTED'}")

        print("\nwalk-through with E13 (office clean plus pest control, which is not offered):")
        url = f"/enquiries/{ids['E13']}"
        print("  PDF before approval:      HTTP", c.get(url + "/quote.pdf").status_code)
        r = c.post(url + "/acknowledge", data={"label": "Pest control"})
        print("  after leaving it out:     ", status_of(r.text), total_of(r.text))
        r = c.post(url + "/approve")
        print("  after approval:           ", status_of(r.text))
        pdf = c.get(url + "/quote.pdf")
        print("  PDF after approval:       HTTP", pdf.status_code, len(pdf.content), "bytes")
        r = c.post(url + "/field", data={"name": "area_sqm", "value": "220"})
        print("  after editing the area:   ", status_of(r.text), total_of(r.text))
        print("  PDF after the edit:       HTTP", c.get(url + "/quote.pdf").status_code)
        steps_ok = pdf.status_code == 200 and status_of(r.text) == "ready"

        seen = [other.get(p).status_code for p in (url, url + "/quote.pdf")]
        tried = [other.post(url + p, data=d).status_code for p, d in (
            ("/field", {"name": "area_sqm", "value": "1"}), ("/approve", {}), ("/delete", {}))]
        empty = "No enquiries yet." in other.get("/").text
        print(f"\nsecond account: sees no enquiries: {empty}; the first account's enquiry and PDF answer {seen}; "
              f"editing, approving and deleting it answer {tried}")
        foreign = c.post(url + "/delete", headers={"Origin": "https://elsewhere.example"}).status_code
        still = total_of(c.get(url).text)
        print(f"a delete sent from another site answers {foreign}; the quote still totals {still}")
        isolated = empty and set(seen + tried) == {404} and foreign == 403 and still == total_of(r.text)
    finally:
        gone = [cl.post("/account/delete", data={"password": pw}).status_code
                for cl, pw in ((c, password), (other, other_password))]
        after = c.get("/", follow_redirects=False).status_code
        print(f"both demo accounts deleted: {gone}; the old login now answers {after}")
    print(f"\nfixtures as expected: {len(FIXTURES['enquiries']) - wrong}/{len(FIXTURES['enquiries'])}")
    return 1 if wrong or not steps_ok or not isolated or gone != [200, 200] or after != 303 else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8812"))
