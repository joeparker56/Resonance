"""Score the rules engine against claims_history.csv and write EVAL.md.

    python evaluate.py

Numbers in EVAL.md come from this run. The engine does not read a claim's own
outcome when it decides that claim. Ledgers differ only in which earlier
claims are treated as already paid.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from billing.engine import PRECEDENCE, adjudicate, billable_units
from billing.normalize import load_authorizations, load_claims, load_visits

ROOT = Path(__file__).resolve().parent
CUTOFF = date(2026, 10, 1)
CODES = ["PAID", "D01", "D02", "D03", "D04", "D05", "D06", "D07", "D08", "D09", "D10", "D99"]


def _actual(claim) -> str:
    if claim.status == "paid":
        return "PAID"
    return claim.denial_code or "PAID"


def _predicted(decision) -> str:
    if decision.status == "paid":
        return "PAID"
    return decision.denial_code


def _pct(numerator: int, denominator: int) -> str:
    if denominator == 0:
        return "n/a"
    return f"{numerator / denominator * 100:.2f}% ({numerator}/{denominator})"


def _metrics(decisions) -> dict:
    matrix = Counter()
    by_payer = defaultdict(lambda: {"n": 0, "status": 0, "exact": 0})
    for decision in decisions:
        actual = _actual(decision.claim)
        predicted = _predicted(decision)
        matrix[(actual, predicted)] += 1
        bucket = by_payer[decision.claim.payer]
        bucket["n"] += 1
        if (actual == "PAID") == (predicted == "PAID"):
            bucket["status"] += 1
        if actual == predicted:
            bucket["exact"] += 1
    n = sum(bucket["n"] for bucket in by_payer.values())
    status = sum(bucket["status"] for bucket in by_payer.values())
    exact = sum(bucket["exact"] for bucket in by_payer.values())
    return {"n": n, "status": status, "exact": exact, "matrix": matrix, "by_payer": by_payer}


def _code_rows(matrix: Counter) -> list[str]:
    lines = [
        "| code | support | precision | recall | tp | fp | fn |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for code in CODES:
        tp = matrix[(code, code)]
        support = sum(matrix[(code, other)] for other in CODES)
        predicted_n = sum(matrix[(other, code)] for other in CODES)
        fp = predicted_n - tp
        fn = support - tp
        if support == 0 and predicted_n == 0:
            continue
        precision = _pct(tp, tp + fp)
        recall = _pct(tp, support)
        lines.append(
            f"| {code} | {support} | {precision} | {recall} | {tp} | {fp} | {fn} |"
        )
    return lines


def _matrix_lines(matrix: Counter) -> list[str]:
    active = [code for code in CODES if any(matrix[(code, other)] or matrix[(other, code)] for other in CODES)]
    header = "| actual \\ predicted | " + " | ".join(active) + " |"
    sep = "|---|" + "|".join("---:" for _ in active) + "|"
    lines = [header, sep]
    for actual in active:
        cells = " | ".join(str(matrix[(actual, predicted)]) for predicted in active)
        lines.append(f"| {actual} | {cells} |")
    return lines


def _payer_lines(metrics: dict) -> list[str]:
    lines = [
        "| payer | claims | status accuracy | exact accuracy |",
        "|---|---:|---:|---:|",
        f"| all | {metrics['n']} | {_pct(metrics['status'], metrics['n'])} | {_pct(metrics['exact'], metrics['n'])} |",
    ]
    for payer in ("PY01", "PY02", "PY03"):
        bucket = metrics["by_payer"][payer]
        lines.append(
            f"| {payer} | {bucket['n']} | {_pct(bucket['status'], bucket['n'])} | {_pct(bucket['exact'], bucket['n'])} |"
        )
    return lines


def _examples(decisions, predicate, limit=5) -> list[str]:
    lines = []
    for decision in decisions:
        if not predicate(decision):
            continue
        claim = decision.claim
        lines.append(
            f"- `{claim.claim_id}` {claim.payer} {claim.agency_id} {claim.service_code} "
            f"{claim.units} units on {claim.date_of_service}, submitted {claim.submitted_date}. "
            f"Actual {_actual(claim)}, predicted {_predicted(decision)}"
            + (f" (rules fired: {', '.join(decision.fired)})" if decision.fired else " (no rule fired)")
            + (f". Remark: {claim.remark}" if claim.remark else ".")
        )
        if len(lines) >= limit:
            break
    return lines


def _contradictions(decisions) -> list[str]:
    found = []
    for decision in decisions:
        actual = _actual(decision.claim)
        if actual not in decision.fired:
            continue
        order = PRECEDENCE[decision.claim.payer]
        winner = next(code for code in order if code in decision.fired)
        if winner != actual:
            found.append(
                f"`{decision.claim.claim_id}` actual {actual}, table picks {winner}, fired {list(decision.fired)}"
            )
    return found


def _pair_counts(decisions) -> Counter:
    counts = Counter()
    for decision in decisions:
        actual = _actual(decision.claim)
        if actual not in decision.fired or len(decision.fired) < 2:
            continue
        for other in decision.fired:
            if other != actual:
                counts[(decision.claim.payer, actual, other)] += 1
    return counts


def _d02_gap_note(history_decisions, joint_decisions) -> list[str]:
    """History D02 rows the history-only ledger does not call D02.

    `auth_remaining` is the balance before the claim, counting paid history only.
    The row is D02 on the file when that balance is too high by at least
    `remaining - units + 1`. Earlier unchecked claims the joint ledger would pay
    are the missing drawdown.
    """
    joint_by_id = {decision.claim.claim_id: decision for decision in joint_decisions}
    lines = []
    covered = 0
    missed = [
        decision
        for decision in history_decisions
        if _actual(decision.claim) == "D02" and _predicted(decision) != "D02"
    ]
    for decision in missed:
        claim = decision.claim
        if decision.auth_remaining is None:
            lines.append(f"- `{claim.claim_id}` has no usable authorization balance to compare.")
            continue
        needed = decision.auth_remaining - claim.units + 1
        earlier = []
        for other in joint_by_id.values():
            if other.claim.source != "pending" or other.claim.auth_id != claim.auth_id:
                continue
            if (other.claim.submitted_date, other.claim.claim_id) >= (claim.submitted_date, claim.claim_id):
                continue
            if other.status == "paid":
                earlier.append(other.claim)
        earlier_units = sum(other.units for other in earlier)
        ids = ", ".join(other.claim_id for other in earlier) or "none"
        joint_code = _predicted(joint_by_id[claim.claim_id])
        if needed > 0 and earlier_units >= needed:
            covered += 1
            lines.append(
                f"- `{claim.claim_id}` has {decision.auth_remaining} units left on {claim.auth_id} "
                f"and bills {claim.units}. It needs {needed} more units already drawn. "
                f"Earlier unchecked claims the engine would pay ({ids}) total {earlier_units}. "
                f"On the joint ledger this claim is predicted {joint_code}."
            )
        else:
            lines.append(
                f"- `{claim.claim_id}` has {decision.auth_remaining} left on {claim.auth_id}, "
                f"bills {claim.units}, needs {needed} more units drawn. "
                f"Earlier unchecked paid units: {earlier_units} ({ids}). "
                f"Joint-ledger prediction: {joint_code}."
            )
    return [
        f"{covered} of {len(missed)} history D02 rows missed on the history-only ledger "
        "are covered by earlier unchecked claims the engine would pay."
    ] + lines


def _counterexample(history) -> str:
    paid = [
        claim
        for claim in history
        if claim.payer == "PY03" and claim.status == "paid" and claim.units % 4 != 0
    ]
    denied = [claim for claim in history if claim.denial_code == "D10"]
    paid_by_auth = defaultdict(list)
    for claim in paid:
        paid_by_auth[claim.auth_id].append(claim)
    for claim in denied:
        mates = paid_by_auth.get(claim.auth_id) or []
        if mates:
            other = mates[0]
            return (
                f"`{claim.claim_id}` bills {claim.units} units on {claim.date_of_service} "
                f"against {claim.auth_id} and is D10 ({claim.remark}). "
                f"`{other.claim_id}` bills {other.units} units on {other.date_of_service} "
                f"against the same authorization and is paid."
            )
    return "No shared authorization was found between a D10 row and a paid partial-hour row."


def _multi_visit_effect(history, visits) -> tuple[int, int]:
    by_key = defaultdict(list)
    for visit in visits:
        if visit.verification_status != "verified":
            continue
        minutes = int((visit.clock_out - visit.clock_in).total_seconds() // 60)
        by_key[(visit.patient_id, visit.service_code, visit.clock_in.date())].append(minutes)
    multi = {key: parts for key, parts in by_key.items() if len(parts) > 1}
    changed = 0
    for claim in history:
        parts = multi.get((claim.patient_id, claim.service_code, claim.date_of_service))
        if not parts:
            continue
        summed = billable_units(claim.payer, sum(parts))
        longest = billable_units(claim.payer, max(parts))
        if (claim.units > summed) != (claim.units > longest):
            changed += 1
    return len(multi), changed


def _section(title: str, metrics: dict) -> list[str]:
    return [f"### {title}", "", *_payer_lines(metrics), ""]


def build_report(history, pending, dropped, authorizations, visits) -> str:
    def consume_predicted(claim, status):
        return status == "paid"

    def consume_actual(claim, status):
        return claim.status == "paid"

    def consume_holdout(claim, status):
        if claim.submitted_date < CUTOFF:
            return claim.status == "paid"
        return status == "paid"

    def consume_joint(claim, status):
        if claim.status:
            return claim.status == "paid"
        return status == "paid"

    open_loop = adjudicate(history, authorizations, visits, consume_predicted)
    prior = adjudicate(history, authorizations, visits, consume_actual)
    holdout_run = adjudicate(history, authorizations, visits, consume_holdout)
    joint = adjudicate(history + pending, authorizations, visits, consume_joint)

    m_open = _metrics(open_loop)
    m_prior = _metrics(prior)
    holdout_decisions = [d for d in holdout_run if d.claim.submitted_date >= CUTOFF]
    early_decisions = [d for d in prior if d.claim.submitted_date < CUTOFF]
    m_hold = _metrics(holdout_decisions)
    m_joint = _metrics([d for d in joint if d.claim.source == "history"])

    prior_errors = [d for d in prior if _actual(d.claim) != _predicted(d)]
    d99 = [d for d in prior_errors if _actual(d.claim) == "D99"]
    d10 = [d for d in prior_errors if _actual(d.claim) == "D10"]
    d02 = [d for d in prior_errors if _actual(d.claim) == "D02"]
    accounted = {id(item) for item in d99 + d10 + d02}
    other = [d for d in prior_errors if id(d) not in accounted]

    # Co-fire checks use the known-prior ledger, including inside the holdout
    # window, so a wrong predicted balance is not mistaken for a precedence miss.
    prior_holdout = [d for d in prior if d.claim.submitted_date >= CUTOFF]
    early_pairs = _pair_counts(early_decisions)
    hold_pairs = _pair_counts(prior_holdout)
    early_conflicts = _contradictions(early_decisions)
    hold_conflicts = _contradictions(prior_holdout)

    pending_decisions = [d for d in joint if d.claim.source == "pending"]
    pending_counts = Counter(_predicted(d) for d in pending_decisions)
    review_counts = Counter(reason for d in pending_decisions for reason in d.review_reasons)

    lines = [
        "# Evaluation",
        "",
        "Generated by `python evaluate.py`. Counts below are from that run.",
        "",
        "## Setup",
        "",
        f"`claims_history.csv` has {len(history) + dropped} rows and {len(history)} distinct claim ids. "
        f"{dropped} extra rows are exact copies and are dropped so a paid claim is not drawn down twice. "
        f"`claims_to_check.csv` has {len(pending)} claims. Authorizations: {len(authorizations)}. "
        f"EVV visits: {len(visits)}.",
        "",
        "The engine never reads the claim it is deciding. Three ledgers are scored:",
        "",
        "- **Known priors.** Earlier history claims draw units from the outcome on the file. This is the ledger a biller has once those claims are adjudicated.",
        "- **Open loop.** Every history claim draws units only if the engine itself would pay it. A wrong pay or deny changes later balances.",
        f"- **Holdout.** Claims submitted before {CUTOFF.isoformat()} use the recorded outcome. Claims submitted on or after that date use the engine, and only those later claims are scored. "
        "The cut sits after both bulletin dates (1 August filing, 1 September weekend modifier), so the holdout still contains both versions of each rule.",
        "",
        "A fourth pass puts `claims_to_check.csv` on the same submission queue. That is the ledger `predict.py` uses. It is reported as a finding, not as the headline, because those claims have no outcome of their own.",
        "",
        "## Results",
        "",
        *_section("Known prior adjudications, full history", m_prior),
        *_section("Open-loop replay, full history", m_open),
        *_section(f"Holdout, submitted on or after {CUTOFF.isoformat()}", m_hold),
        *_section("History scored on the joint queue with unchecked claims", m_joint),
        "",
        "### Per denial code",
        "",
        "Known-prior ledger, full history. Precision and recall treat PAID as its own class. "
        "D10 and D99 are never predicted.",
        "",
        *_code_rows(m_prior["matrix"]),
        "",
        "### Confusion matrix",
        "",
        "Known-prior ledger. Rows are the code on the file, columns are the engine.",
        "",
        *_matrix_lines(m_prior["matrix"]),
        "",
        "### Holdout confusion matrix",
        "",
        *_matrix_lines(m_hold["matrix"]),
        "",
        "## Precedence check",
        "",
        "When two documented rules both match, the engine reports the earlier code in `PRECEDENCE`. "
        "A contradiction is a claim whose recorded code is one of the matched rules but not the earliest.",
        "",
        f"Before {CUTOFF.isoformat()}: {len(early_conflicts)} contradictions on {len(early_decisions)} claims. "
        f"Holdout: {len(hold_conflicts)} contradictions on {len(holdout_decisions)} claims.",
        "",
    ]
    if early_conflicts or hold_conflicts:
        lines.append("Contradictions:")
        lines.extend(f"- {item}" for item in early_conflicts + hold_conflicts)
        lines.append("")
    lines.append("Co-fires on the pre-October slice (recorded code, other code that also matched, count):")
    lines.append("")
    if not early_pairs:
        lines.append("None.")
    else:
        for (payer, winner, loser), count in sorted(early_pairs.items(), key=lambda item: (-item[1], item[0])):
            lines.append(f"- {payer}: {winner} over {loser}, {count}")
    lines.append("")
    lines.append("Co-fires on the holdout:")
    lines.append("")
    if not hold_pairs:
        lines.append("None.")
    else:
        for (payer, winner, loser), count in sorted(hold_pairs.items(), key=lambda item: (-item[1], item[0])):
            lines.append(f"- {payer}: {winner} over {loser}, {count}")
    lines.append("")

    lines.extend(
        [
            "## Error buckets",
            "",
            "Buckets are the misses on the known-prior ledger. Claim ids are from this run.",
            "",
            f"### D99, manual review ({len(d99)} claims)",
            "",
            "D99 is 'other / referred for manual review'. "
            f"{sum(1 for d in d99 if _predicted(d) == 'PAID')} of these match no documented rule: "
            "a verified visit, a valid authorization, and units the visit supports. "
            f"The other {sum(1 for d in d99 if _predicted(d) != 'PAID')} also trip a documented rule "
            "(see the confusion matrix) and the file still says D99. "
            "Remarks are 'Denied - contact payer', 'Under review - see payer correspondence', "
            "or, on some Summit rows, the amendment 3 sentence that also appears on D10. "
            "Nothing in the manuals picks the clean rows out, and they are spread across all three payers, "
            "so the engine leaves a clean row as paid and does not invent a filter. They cannot be flagged in advance.",
            "",
            *_examples(d99, lambda _d: True),
            "",
            f"### D10, Summit amendment 3 ({len(d10)} claims)",
            "",
            "Every D10 remark says services must be billed in whole hours (4 units). Every D10 row bills a "
            "non-multiple of 4. The same shape is also paid. Applying the remark as a hard rule would deny those "
            "paid claims, so partial hours are flagged in `needs_review.csv` and not auto-denied. "
            "Where a D10 row also exceeds verified time or repeats a claim, the engine reports that documented code.",
            "",
            _counterexample(history),
            "",
            *_examples(d10, lambda _d: True),
            "",
            f"### Authorization balance ({len(d02)} claims)",
            "",
            "D02 is units above the authorization balance, drawn in submission order from paid claims only "
            "(billing_basics.md, each manual's authorization section). On a history-only ledger some recorded "
            "D02 rows still have units left. Earlier rows in `claims_to_check.csv` sit on those authorizations.",
            "",
            *_d02_gap_note(prior, joint),
            "",
            f"### Other ({len(other)} claims)",
            "",
        ]
    )
    if other:
        lines.extend(_examples(other, lambda _d: True, limit=10))
    else:
        lines.append("No remaining misses.")
    lines.append("")

    prior_by_id = {d.claim.claim_id: d for d in prior}
    cascade = []
    for decision in open_loop:
        known = prior_by_id[decision.claim.claim_id]
        if _predicted(decision) != _predicted(known):
            cascade.append(decision)
    lines.extend(
        [
            "## Where the ledger changes the code",
            "",
            f"Open-loop decisions differ from the known-prior decision on {len(cascade)} history claims. "
            "Those are balance errors that follow from an earlier D99 or D10 row the engine treated as paid, "
            "or from a real denial it missed, so a later claim sees the wrong balance.",
            "",
        ]
    )
    if cascade:
        lines.extend(_examples(cascade, lambda _d: True, limit=8))
    else:
        lines.append("None.")
    lines.append("")
    lines.extend(
        [
            "## Unchecked claims",
            "",
            "No labels. Counts are the joint-ledger decisions written to `predictions.csv`.",
            "",
            "| prediction | claims |",
            "|---|---:|",
        ]
    )
    for code in CODES:
        if pending_counts[code]:
            lines.append(f"| {code} | {pending_counts[code]} |")
    lines.extend(
        [
            "",
            f"Flagged for review: {sum(review_counts.values())} reason hits on "
            f"{sum(1 for d in pending_decisions if d.review_reasons)} claims "
            f"({dict(review_counts)}).",
            "",
        ]
    )
    regressions = []
    joint_by_id = {d.claim.claim_id: d for d in joint}
    for decision in prior:
        other = joint_by_id[decision.claim.claim_id]
        if _predicted(decision) == _actual(decision.claim) and _predicted(other) != _actual(other.claim):
            regressions.append(other)
    multi_days, multi_changed = _multi_visit_effect(history, visits)
    max_units = max(claim.units for claim in history)
    collateral = (
        f"Putting the unchecked claims on the queue fixes the {len(d02)} D02 rows above and "
        f"changes {len(regressions)} history "
        f"{'claim' if len(regressions) == 1 else 'claims'} "
        "that the known-prior ledger had right."
    )
    if len(regressions) == 1:
        collateral += (
            " That claim is paid on the file. An earlier unchecked claim on the same authorization "
            "is one the engine would pay, so the balance drops. If the unchecked claim were a "
            "manual denial, the file's balance would not have lost those units."
        )
    lines.extend(["### Collateral change on the joint queue", "", collateral, ""])
    if regressions:
        lines.extend(_examples(regressions, lambda _d: True, limit=5))
    else:
        lines.append("None.")
    lines.extend(
        [
            "",
            "## Findings left out of the rules",
            "",
            f"- D99 accounts for {len(d99)} of the known-prior misses. The manuals define the code and do not say when it is used. Encoding it would mean memorising claim ids.",
            f"- D10 accounts for {len(d10)} misses. Section 9.5 of the Summit handbook says additional contract terms may apply, and the remarks name amendment 3, but history pays partial-hour claims on authorizations that also produce D10. The correction claims in the unchecked file do round those units down to a multiple of 4, which is consistent with the remark and still does not say which partial hours are denied.",
            f"- {multi_days} member-days have more than one verified visit. Summing those minutes versus keeping the longest visit would change the D09 decision on {multi_changed} history claims. The engine sums them because the help article says same-day visits are added and the claim does not name a caregiver.",
            f"- History units are already 15-minute units (largest billed value {max_units}). No row stored minutes in the units column.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    history, dropped = load_claims(ROOT / "claims_history.csv", "history", labeled=True)
    pending, _ = load_claims(ROOT / "claims_to_check.csv", "pending", labeled=False)
    authorizations = load_authorizations(ROOT / "authorizations.csv")
    visits = load_visits(ROOT / "evv_visits.csv")
    report = build_report(history, pending, dropped, authorizations, visits)
    path = ROOT / "EVAL.md"
    path.write_text(report)
    print(f"wrote {path}")
    # Print the results tables so a rerun is easy to compare.
    for line in report.splitlines():
        if line.startswith("| all") or line.startswith("| PY") or line.startswith("### "):
            print(line)


if __name__ == "__main__":
    main()
