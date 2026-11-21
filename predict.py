"""Write predictions.csv for claims_to_check.csv.

History claims keep the outcome the payer already reported, and they draw
authorization units in submission order. Unchecked claims are decided by the
rules and draw units only when the rules would pay them. Run from anywhere:

    python predict.py
"""

from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path

from billing.engine import REVIEW_DETAIL, adjudicate
from billing.normalize import load_authorizations, load_claims, load_visits

ROOT = Path(__file__).resolve().parent
VALID_CODES = {"", "D01", "D02", "D03", "D04", "D05", "D06", "D07", "D08", "D09", "D10", "D99"}


def _consumes(claim, predicted_status: str) -> bool:
    if claim.status:
        return claim.status == "paid"
    return predicted_status == "paid"


def main() -> None:
    history, dropped = load_claims(ROOT / "claims_history.csv", "history", labeled=True)
    pending, _ = load_claims(ROOT / "claims_to_check.csv", "pending", labeled=False)
    authorizations = load_authorizations(ROOT / "authorizations.csv")
    visits = load_visits(ROOT / "evv_visits.csv")
    decisions = adjudicate(history + pending, authorizations, visits, consumes=_consumes)
    by_id = {decision.claim.claim_id: decision for decision in decisions}

    ordered = sorted(pending, key=lambda claim: claim.row_number)
    if len(ordered) != len({claim.claim_id for claim in ordered}):
        raise SystemExit("claims_to_check.csv has duplicate claim ids")

    prediction_path = ROOT / "predictions.csv"
    review_path = ROOT / "needs_review.csv"
    with prediction_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["claim_id", "status", "denial_code"])
        writer.writeheader()
        for claim in ordered:
            decision = by_id[claim.claim_id]
            if decision.status not in {"paid", "denied"}:
                raise SystemExit(f"bad status on {claim.claim_id}")
            if decision.denial_code not in VALID_CODES:
                raise SystemExit(f"bad denial code on {claim.claim_id}")
            if decision.status == "paid" and decision.denial_code:
                raise SystemExit(f"paid claim {claim.claim_id} has a denial code")
            if decision.status == "denied" and not decision.denial_code:
                raise SystemExit(f"denied claim {claim.claim_id} has no denial code")
            writer.writerow(
                {
                    "claim_id": claim.claim_id,
                    "status": decision.status,
                    "denial_code": decision.denial_code,
                }
            )

    review_fields = ["claim_id", "status", "denial_code", "review_reasons", "detail", "rules_fired"]
    reviewed = 0
    with review_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=review_fields)
        writer.writeheader()
        for claim in ordered:
            decision = by_id[claim.claim_id]
            if not decision.review_reasons:
                continue
            reviewed += 1
            writer.writerow(
                {
                    "claim_id": claim.claim_id,
                    "status": decision.status,
                    "denial_code": decision.denial_code,
                    "review_reasons": "|".join(decision.review_reasons),
                    "detail": " ".join(REVIEW_DETAIL[reason] for reason in decision.review_reasons),
                    "rules_fired": "|".join(decision.fired),
                }
            )

    counts = Counter(by_id[claim.claim_id].denial_code or "paid" for claim in ordered)
    print(f"wrote {prediction_path.name}: {len(ordered)} claims")
    print(f"wrote {review_path.name}: {reviewed} claims")
    print(f"history duplicate rows ignored: {dropped}")
    print("predictions:", dict(counts))


if __name__ == "__main__":
    main()
