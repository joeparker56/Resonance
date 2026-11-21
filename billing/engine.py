"""Payer rules for one claim.

Each denial code is a rule from the provider manuals, the two 2026 bulletins,
or the Exchange4Health help article. The first matching code in PRECEDENCE is
the one reported. That order is the manual order where the manuals imply one,
and where they do not, it is the order history actually reports when two rules
fail together (see README).

Dates of service are compared with bulletin effective dates. A claim submitted
after a bulletin, for a service date before it, still uses the old rule.

Paid claims draw authorization units and Harbor daily units in submission
order (submitted date, then claim id). Denied claims do not. A frequency-7
correction is adjudicated on its own and is not a duplicate; a later
frequency-1 claim for the same member, service, date and payer is.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from billing.normalize import Authorization, Claim, Visit

# Bulletin 2026-07: Northwind 45-day filing for dates of service on or after this day.
NORTHWIND_LONG_FILING = date(2026, 8, 1)
# Bulletin 2026-08: Harbor weekend modifier U3 for dates of service on or after this day.
HARBOR_WEEKEND_MODIFIER = date(2026, 9, 1)
DAILY_UNIT_MAX = 32
# Four 15-minute units make one hour. Used only to flag Summit amendment 3.
HOUR_UNITS = 4

# First match wins. Codes absent from a payer's tuple are not that payer's rules.
PRECEDENCE: dict[str, tuple[str, ...]] = {
    # Northwind manual §4.2, §4.3, §4.5, §6.1, §6.4.
    # History: D09 is reported ahead of D07/D08/D02/D03/D05; D03 ahead of D05.
    "PY01": ("D01", "D09", "D08", "D07", "D02", "D03", "D05"),
    # Harbor guide: visit verification, modifiers, authorizations, duplicates,
    # daily maximum. History: D09 ahead of D04; D04, D05, D07 and D08 ahead of
    # D06; D06 ahead of D02. Harbor has no timely-filing limit.
    "PY02": ("D01", "D09", "D04", "D08", "D07", "D05", "D06", "D02"),
    # Summit §9.1–§9.4. History: D07/D08 are reported ahead of D09, unlike
    # Northwind and Harbor. Then D09 ahead of D02, D02 ahead of D03.
    "PY03": ("D01", "D08", "D07", "D09", "D02", "D03", "D05"),
}

REVIEW_DETAIL = {
    "summit_partial_hour": (
        "Summit units are not a whole hour (4 units = 1 hour). Denied claims "
        "with this shape carry an 'amendment 3' remark, but the same "
        "authorizations also pay partial hours, so the engine does not deny on it."
    ),
    "corrected_claim": (
        "Frequency code 7. Judged as its own claim and not as a duplicate of "
        "the original. History contains no corrected claims; every original "
        "pointed at from the unchecked file was itself denied, so a paid "
        "original being replaced is untested."
    ),
    "unknown_frequency": "Frequency code is neither 1 nor 7.",
}


@dataclass(frozen=True)
class Decision:
    claim: Claim
    status: str
    denial_code: str
    fired: tuple[str, ...]
    review_reasons: tuple[str, ...]
    auth_remaining: int | None


def billable_units(payer: str, minutes: int) -> int:
    """Units supported by verified minutes.

    Northwind manual §4.3, the 8-minute rule: a remainder of 8 minutes or more
    after complete 15-minute units bills as one more unit. That is
    (minutes + 7) // 15. Seven minutes or fewer do not.

    Harbor visit verification and Summit §9.1: only complete 15-minute
    increments. A partial unit is not billable.
    """
    if minutes <= 0:
        return 0
    if payer == "PY01":
        return (minutes + 7) // 15
    return minutes // 15


def timely_limit_days(payer: str, date_of_service: date) -> int | None:
    """Calendar days allowed between service and receipt, inclusive.

    None means the payer does not apply a limit (Harbor filing-limits section).
    Northwind manual §6.1 is 30 days. Bulletin 2026-07 makes it 45 days for
    dates of service on or after 1 August 2026; earlier service dates stay at
    30 even if the claim is submitted after the bulletin. Summit §9.3 is 60.
    """
    if payer == "PY01":
        if date_of_service >= NORTHWIND_LONG_FILING:
            return 45
        return 30
    if payer == "PY03":
        return 60
    return None


def required_modifier(payer: str, service_code: str, date_of_service: date) -> str | None:
    """Modifier Harbor requires, or None if the payer does not check one.

    Harbor modifiers section: T1019 is billed with U2. Bulletin 2026-08: for
    a Saturday or Sunday date of service on or after 1 September 2026, T1019
    is billed with U3 instead. Weekdays stay on U2. A weekend before 1 September
    stays on U2.
    """
    if payer != "PY02" or service_code != "T1019":
        return None
    weekend = date_of_service.weekday() >= 5
    if date_of_service >= HARBOR_WEEKEND_MODIFIER and weekend:
        return "U3"
    return "U2"


def verified_minutes(visits: list[Visit]) -> dict[tuple[str, str, date], int]:
    """Sum verified minutes by member, service and clock-in date.

    billing_basics.md: only verified visits count (manual_edit_pending and
    rejected do not), a visit belongs to the clock-in date when it crosses
    midnight, and visits to the same member for the same service on that date
    are added together. Claims do not name a caregiver, so minutes are pooled
    at member + service + clock-in date.
    """
    totals: dict[tuple[str, str, date], int] = defaultdict(int)
    for visit in visits:
        if visit.verification_status != "verified":
            continue
        minutes = int((visit.clock_out - visit.clock_in).total_seconds() // 60)
        key = (visit.patient_id, visit.service_code, visit.clock_in.date())
        totals[key] += minutes
    return totals


def _auth_problem(claim: Claim, auth: Authorization | None) -> str | None:
    if not claim.auth_id:
        return "D07"
    if auth is None:
        return "D08"
    if (
        auth.patient_id != claim.patient_id
        or auth.service_code != claim.service_code
        or auth.payer != claim.payer
        or not (auth.start_date <= claim.date_of_service <= auth.end_date)
    ):
        return "D08"
    return None


def _review_reasons(claim: Claim) -> tuple[str, ...]:
    reasons = []
    if claim.payer == "PY03" and claim.units % HOUR_UNITS != 0:
        reasons.append("summit_partial_hour")
    if claim.frequency_code == "7":
        reasons.append("corrected_claim")
    if claim.frequency_code not in {"1", "7"}:
        reasons.append("unknown_frequency")
    return tuple(reasons)


def adjudicate(claims, authorizations, visits, consumes) -> list[Decision]:
    """Decide every claim in submission order.

    `consumes(claim, predicted_status) -> bool` says whether this claim draws
    authorization units and Harbor daily units. Callers pass the engine's own
    prediction, or the recorded outcome when that outcome is already known.
    """
    auth_by_id = {auth.auth_id: auth for auth in authorizations}
    minutes = verified_minutes(visits)
    remaining = {auth.auth_id: auth.units_authorized for auth in authorizations}
    daily: dict[tuple[str, date], int] = defaultdict(int)
    submitted: set[tuple[str, str, str, date]] = set()
    decisions: list[Decision] = []

    ordered = sorted(claims, key=lambda claim: (claim.submitted_date, claim.claim_id))
    for claim in ordered:
        auth = auth_by_id.get(claim.auth_id) if claim.auth_id else None
        auth_problem = _auth_problem(claim, auth)
        auth_ok = auth is not None and auth_problem is None
        balance = remaining[auth.auth_id] if auth_ok else None

        matched: set[str] = set()
        dup_key = (claim.payer, claim.patient_id, claim.service_code, claim.date_of_service)
        if claim.frequency_code != "7" and dup_key in submitted:
            matched.add("D05")

        limit = timely_limit_days(claim.payer, claim.date_of_service)
        if limit is not None and (claim.submitted_date - claim.date_of_service).days > limit:
            matched.add("D03")

        if auth_problem:
            matched.add(auth_problem)

        required = required_modifier(claim.payer, claim.service_code, claim.date_of_service)
        if required is not None and claim.modifier != required:
            matched.add("D04")

        supported = minutes.get((claim.patient_id, claim.service_code, claim.date_of_service), 0)
        if supported <= 0:
            matched.add("D01")
        if claim.units > billable_units(claim.payer, supported):
            matched.add("D09")

        if claim.payer == "PY02" and daily[(claim.patient_id, claim.date_of_service)] + claim.units > DAILY_UNIT_MAX:
            matched.add("D06")

        if auth_ok and claim.units > remaining[auth.auth_id]:
            matched.add("D02")

        order = PRECEDENCE[claim.payer]
        unknown = matched - set(order)
        if unknown:
            raise RuntimeError(f"rule {sorted(unknown)} is not in the {claim.payer} order")
        fired = tuple(code for code in order if code in matched)
        denial_code = fired[0] if fired else ""
        status = "denied" if denial_code else "paid"

        decisions.append(
            Decision(
                claim=claim,
                status=status,
                denial_code=denial_code,
                fired=fired,
                review_reasons=_review_reasons(claim),
                auth_remaining=balance,
            )
        )

        submitted.add(dup_key)
        if consumes(claim, status):
            if auth_ok:
                remaining[auth.auth_id] -= claim.units
            if claim.payer == "PY02":
                daily[(claim.patient_id, claim.date_of_service)] += claim.units

    return decisions
