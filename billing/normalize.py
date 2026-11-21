"""Normalise exported claims, authorizations and EVV rows at the load boundary.

Downstream code sees payer ids (PY01, PY02, PY03), ISO dates, upper-cased
tokens and integer units. Slash dates are month/day/year, which is what the
paid Northwind claims require: a June 9 service submitted as 07/09/2026 is
paid, so that string is 9 July, not 7 September.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

PAYER_ALIASES = {
    "PY01": "PY01",
    "NORTHWIND HP": "PY01",
    "NORTHWIND HEALTH PLAN": "PY01",
    "PY02": "PY02",
    "HARBOR MEDICAID PLAN": "PY02",
    "HARBOR MEDICAID": "PY02",
    "PY03": "PY03",
    "SUMMIT CARE": "PY03",
    "SUMMIT CARE PARTNERS": "PY03",
}

DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y")
CLOCK_FORMATS = ("%Y-%m-%d %H:%M", "%m/%d/%Y %H:%M")
KNOWN_EVV_STATUS = {"verified", "rejected", "manual_edit_pending"}


def parse_date(value: str) -> date:
    text = value.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognised date: {value!r}")


def parse_clock(value: str) -> datetime:
    text = value.strip()
    for fmt in CLOCK_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError(f"unrecognised clock time: {value!r}")


def parse_units(value: str) -> int:
    text = value.strip().replace(",", "")
    if not text:
        raise ValueError("blank units")
    number = float(text)
    if not number.is_integer():
        raise ValueError(f"fractional units: {value!r}")
    return int(number)


def normalize_payer(value: str) -> str:
    key = value.strip().upper()
    try:
        return PAYER_ALIASES[key]
    except KeyError as exc:
        raise ValueError(f"unknown payer: {value!r}") from exc


def _token(value: str) -> str:
    return value.strip().upper()


@dataclass(frozen=True)
class Claim:
    claim_id: str
    agency_id: str
    payer: str
    patient_id: str
    service_code: str
    date_of_service: date
    submitted_date: date
    units: int
    auth_id: str
    modifier: str
    frequency_code: str
    original_claim_id: str
    source: str
    status: str
    denial_code: str
    remark: str
    row_number: int


@dataclass(frozen=True)
class Authorization:
    auth_id: str
    patient_id: str
    payer: str
    service_code: str
    start_date: date
    end_date: date
    units_authorized: int


@dataclass(frozen=True)
class Visit:
    visit_id: str
    patient_id: str
    service_code: str
    clock_in: datetime
    clock_out: datetime
    verification_status: str


def claim_from_row(row: dict, source: str, row_number: int, labeled: bool) -> Claim:
    status = ""
    denial_code = ""
    remark = ""
    if labeled:
        status = row.get("status", "").strip().lower()
        denial_code = row.get("denial_code", "").strip().upper()
        remark = row.get("remark", "").strip()
    return Claim(
        claim_id=row["claim_id"].strip(),
        agency_id=_token(row["agency_id"]),
        payer=normalize_payer(row["payer"]),
        patient_id=_token(row["patient_id"]),
        service_code=_token(row["service_code"]),
        date_of_service=parse_date(row["date_of_service"]),
        submitted_date=parse_date(row["submitted_date"]),
        units=parse_units(row["units"]),
        auth_id=_token(row.get("auth_id", "")),
        modifier=_token(row.get("modifier", "")),
        frequency_code=row.get("frequency_code", "").strip(),
        original_claim_id=row.get("original_claim_id", "").strip(),
        source=source,
        status=status,
        denial_code=denial_code,
        remark=remark,
        row_number=row_number,
    )


def _read_rows(path: Path | str) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def load_claims(path: Path | str, source: str, labeled: bool) -> tuple[list[Claim], int]:
    """Load claims, dropping exact duplicate claim ids.

    The history export repeats 32 rows verbatim. Counting both copies would
    draw the same paid units down twice. Conflicting copies raise.
    """
    claims: list[Claim] = []
    seen: dict[str, dict] = {}
    dropped = 0
    for row_number, row in enumerate(_read_rows(path), start=1):
        claim_id = row["claim_id"].strip()
        if claim_id in seen:
            if row != seen[claim_id]:
                raise ValueError(f"claim {claim_id} is duplicated with different values")
            dropped += 1
            continue
        seen[claim_id] = row
        claims.append(claim_from_row(row, source, row_number, labeled))
    return claims, dropped


def load_authorizations(path: Path | str) -> list[Authorization]:
    auths = []
    for row in _read_rows(path):
        auths.append(
            Authorization(
                auth_id=_token(row["auth_id"]),
                patient_id=_token(row["patient_id"]),
                payer=normalize_payer(row["payer_id"]),
                service_code=_token(row["service_code"]),
                start_date=parse_date(row["start_date"]),
                end_date=parse_date(row["end_date"]),
                units_authorized=parse_units(row["units_authorized"]),
            )
        )
    return auths


def load_visits(path: Path | str) -> list[Visit]:
    visits = []
    for row in _read_rows(path):
        status = row["verification_status"].strip().lower()
        if status not in KNOWN_EVV_STATUS:
            raise ValueError(f"unknown EVV status on {row.get('visit_id')}: {status!r}")
        clock_in = parse_clock(row["clock_in"])
        clock_out = parse_clock(row["clock_out"])
        if clock_out < clock_in:
            raise ValueError(f"clock-out before clock-in on {row.get('visit_id')}")
        visits.append(
            Visit(
                visit_id=row["visit_id"].strip(),
                patient_id=_token(row["patient_id"]),
                service_code=_token(row["service_code"]),
                clock_in=clock_in,
                clock_out=clock_out,
                verification_status=status,
            )
        )
    return visits
