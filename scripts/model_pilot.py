"""Pilot: read a few fixture enquiries with the real model and compare with the hand-worked expectations.

THIS SPENDS MONEY on the API key in the environment. It stops before any
call once the estimated spend reaches --max-usd.

    uv run --env-file .env python scripts/model_pilot.py            # 10 enquiries, stops at $0.50
    uv run --env-file .env python scripts/model_pilot.py --ids E01,E15 --max-usd 0.10
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

from quotedesk import config, model                                   # noqa: E402
from quotedesk.domain import Adjustments, PriceList, price_quote      # noqa: E402

USD_PER_MTOK = {"claude-opus-5-5": (4.00, 20.00), "claude-sonnet-5-5": (2.00, 10.00),
                "claude-haiku-5-5": (0.10, 0.50)}                     # input, output; list prices, November 2026
DEFAULT_IDS = "E01,E02,E05,E08,E09,E13,E14,E15,E16,E20"               # two or more of each kind of enquiry


def differences(req, priced, want) -> list[str]:
    """Where the model's reading leads somewhere other than the fixture expects."""
    fields = {k: v.value for k, v in req.fields.items() if k != "customer_name"}    # fixtures do not list names
    wanted_fields = {k: v for k, v in want["fields"].items() if k != "customer_name"}
    checks = [
        ("services", sorted(req.services), want["services"]),
        ("fields", fields, wanted_fields),
        ("conflicts", sorted(req.conflicts), want["conflicts"]),
        ("number of unsupported requests", len(req.unsupported), len(want["unsupported"])),   # labels are free text
        ("blockers", sorted(c.code for c in priced.blockers), sorted(b.split(":")[0] for b in want["blockers"])),
        ("total", str(priced.total) if priced.can_approve else None, want["total"]),
    ]
    return [f"{name}: got {got!r}, expected {expected!r}" for name, got, expected in checks if got != expected]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ids", default=DEFAULT_IDS)
    parser.add_argument("--max-usd", type=float, default=0.50)
    args = parser.parse_args()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set. Put it in .env and run with: uv run --env-file .env python scripts/model_pilot.py")
        return 2
    if config.MODEL not in USD_PER_MTOK:
        print(f"No price on file for {config.MODEL}; add it to USD_PER_MTOK so the spend limit works.")
        return 2
    usd_in, usd_out = USD_PER_MTOK[config.MODEL]

    fixtures = json.loads((ROOT / "fixtures" / "enquiries.json").read_text())
    by_id = {e["id"]: e for e in fixtures["enquiries"]}
    priced_on = date.fromisoformat(fixtures["priced_on"])
    price_list = PriceList.model_validate_json((ROOT / "quotedesk" / "pricelist_v1.json").read_text())
    extractor = model.ModelExtractor({s.code: s.name for s in price_list.services})

    spent, agreed, done, dropped = 0.0, 0, 0, 0
    print(f"model {config.MODEL}, effort {config.MODEL_EFFORT}, spend limit ${args.max_usd:.2f}\n")
    for eid in args.ids.split(","):
        if spent >= args.max_usd:
            print(f"Stopped before {eid}: estimated spend ${spent:.4f} reached the limit.")
            break
        e, started = by_id[eid], time.monotonic()
        try:
            req = extractor.extract(e["text"])
        except model.ModelError as exc:
            print(f"{eid} {e['category']:<14} MODEL FAILED ({exc}); the app would read this one by patterns")
            done += 1
            continue
        took, u = time.monotonic() - started, extractor.usage
        cost = (u.input_tokens * usd_in + u.output_tokens * usd_out) / 1_000_000
        spent, done = spent + cost, done + 1
        diffs = differences(req, price_quote(req, price_list, Adjustments(), priced_on), e["expect"])
        agreed += not diffs
        dropped += len(req.rejected_evidence)
        print(f"{eid} {e['category']:<14} {'same as expected' if not diffs else 'DIFFERENT':<17}"
              f"{u.input_tokens:>6} in {u.output_tokens:>5} out  ${cost:.4f}  {took:4.1f}s")
        for line in diffs:
            print(f"      {line}")
        if req.rejected_evidence:
            print(f"      dropped by the evidence check: {', '.join(req.rejected_evidence)}")
    print(f"\n{agreed} of {done} read as the fixture expects. {dropped} value(s) dropped by the evidence check.")
    print(f"Estimated spend ${spent:.4f}, from token counts at list price; the Console shows what was billed.")
    print("These are 20 invented enquiries the patterns already read correctly, so this checks that the model "
          "step works and what a call costs. It does not show the model is better on real email.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
