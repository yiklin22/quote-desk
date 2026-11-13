"""Pilot: have the real model reword the standard reply for a few fixture enquiries and run the checks.

THIS SPENDS MONEY on the API key in the environment. It stops before any
call once the estimated spend reaches --max-usd.

    uv run --env-file .env python scripts/reply_pilot.py            # 4 replies, stops at $0.25
"""
import argparse
import json
import os
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from model_pilot import USD_PER_MTOK                                   # noqa: E402
from quotedesk import config, model                                    # noqa: E402
from quotedesk.domain import Adjustments, PriceList, price_quote       # noqa: E402
from quotedesk.drafting import NOTHING_YET, draft_reply, rewrite_problems   # noqa: E402
from quotedesk.extraction import RuleExtractor                         # noqa: E402

DEFAULT_IDS = "E01,E09,E13,E15"      # a quote, a missing value, a service not offered, a contradiction


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ids", default=DEFAULT_IDS)
    parser.add_argument("--max-usd", type=float, default=0.25)
    args = parser.parse_args()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set. Put it in .env and run with: uv run --env-file .env python scripts/reply_pilot.py")
        return 2
    usd_in, usd_out = USD_PER_MTOK[config.MODEL]
    fixtures = json.loads((ROOT / "fixtures" / "enquiries.json").read_text())
    by_id = {e["id"]: e for e in fixtures["enquiries"]}
    priced_on = date.fromisoformat(fixtures["priced_on"])
    pl = PriceList.model_validate_json((ROOT / "quotedesk" / "pricelist_v1.json").read_text())
    writer = model.ReplyWriter()

    spent, usable, done = 0.0, 0, 0
    print(f"model {config.MODEL}, effort {config.MODEL_EFFORT}, spend limit ${args.max_usd:.2f}")
    for eid in args.ids.split(","):
        if spent >= args.max_usd:
            print(f"\nStopped before {eid}: estimated spend ${spent:.4f} reached the limit.")
            break
        text = by_id[eid]["text"]
        req = RuleExtractor().extract(text)
        req = req.model_copy(update={"acknowledged": list(req.unsupported)})      # as if the operator agreed to leave them out
        priced = price_quote(req, pl, Adjustments(), priced_on)
        standard = draft_reply(pl, req, priced, approved=priced.can_approve)      # as if a complete quote were approved
        if standard == NOTHING_YET:
            print(f"\n{eid}: nothing to reword")
            continue
        started, done = time.monotonic(), done + 1
        try:
            reworded = writer.write(text, standard)
        except model.ModelError as exc:
            print(f"\n{eid} MODEL FAILED ({exc}); the app would keep the standard reply")
            continue
        took, u = time.monotonic() - started, writer.usage
        cost = (u.input_tokens * usd_in + u.output_tokens * usd_out) / 1_000_000
        spent += cost
        problems = rewrite_problems(reworded, standard, priced)
        usable += not problems
        print(f"\n=== {eid} {by_id[eid]['category']}: {'USABLE' if not problems else 'REJECTED: ' + '; '.join(problems)}"
              f"   {u.input_tokens} in {u.output_tokens} out  ${cost:.4f}  {took:.1f}s")
        print(f"--- standard\n{standard}\n--- reworded\n{reworded}")
    print(f"\n{usable} of {done} passed the checks. Estimated spend ${spent:.4f}, from token counts at list price.")
    print("The checks cover amounts, numbers and questions. Whether the wording is better, or promises "
          "something, is for a person to judge by reading the pairs above.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
