"""Bulletin dates, rounding, and the messy export formats."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from billing.engine import adjudicate
from billing.normalize import (
    Authorization,
    Visit,
    claim_from_row,
    load_claims,
    parse_date,
)


def _claim(**overrides):
    row = {
        "claim_id": "C1",
        "agency_id": "AG001",
        "payer": "PY01",
        "patient_id": "PT0001",
        "service_code": "S5125",
        "date_of_service": "2026-07-15",
        "submitted_date": "2026-07-16",
        "units": "4",
        "auth_id": "AUTH1",
        "modifier": "",
        "frequency_code": "1",
        "original_claim_id": "",
    }
    row.update(overrides)
    return claim_from_row(row, "test", 1, labeled=False)


def _auth(**overrides):
    values = dict(
        auth_id="AUTH1",
        patient_id="PT0001",
        payer="PY01",
        service_code="S5125",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 11, 30),
        units_authorized=100,
    )
    values.update(overrides)
    return Authorization(**values)


def _visit(start, end, status="verified", service="S5125", patient="PT0001"):
    return Visit(
        visit_id="V1",
        patient_id=patient,
        service_code=service,
        clock_in=start,
        clock_out=end,
        verification_status=status,
    )


def _minutes(day, minutes, **kwargs):
    start = datetime(day.year, day.month, day.day, 8, 0)
    return _visit(start, start + timedelta(minutes=minutes), **kwargs)


def _decide(claims, auths=None, visits=None):
    if auths is None:
        auths = [_auth()]
    if visits is None:
        visits = [_minutes(claims[0].date_of_service, 60)]
    return adjudicate(claims, auths, visits, consumes=lambda _claim, status: status == "paid")


def _one(claims, **kwargs):
    return _decide(claims, **kwargs)[0]


class NormalizeTests(unittest.TestCase):
    def test_slash_dates_are_month_first(self):
        self.assertEqual(parse_date("2026-07-09"), date(2026, 7, 9))
        self.assertEqual(parse_date("07/09/2026"), date(2026, 7, 9))
        self.assertEqual(parse_date("9/13/2026"), date(2026, 9, 13))
        self.assertEqual(parse_date("8/1/2026"), date(2026, 8, 1))

    def test_messy_claim_row(self):
        claim = claim_from_row(
            {
                "claim_id": " C9 ",
                "agency_id": " ag001 ",
                "payer": " Northwind Health Plan ",
                "patient_id": " pt0001 ",
                "service_code": "t1019 ",
                "date_of_service": "8/1/2026",
                "submitted_date": "09/15/2026",
                "units": " 8 ",
                "auth_id": " auth1 ",
                "modifier": " u3 ",
                "frequency_code": " 7 ",
                "original_claim_id": " C1 ",
            },
            "test",
            3,
            labeled=False,
        )
        self.assertEqual(claim.claim_id, "C9")
        self.assertEqual(claim.payer, "PY01")
        self.assertEqual(claim.patient_id, "PT0001")
        self.assertEqual(claim.service_code, "T1019")
        self.assertEqual(claim.date_of_service, date(2026, 8, 1))
        self.assertEqual(claim.submitted_date, date(2026, 9, 15))
        self.assertEqual(claim.units, 8)
        self.assertEqual(claim.auth_id, "AUTH1")
        self.assertEqual(claim.modifier, "U3")
        self.assertEqual(claim.frequency_code, "7")
        self.assertEqual(claim.original_claim_id, "C1")

    def test_payer_aliases(self):
        for raw, payer in (
            ("PY02", "PY02"),
            ("Harbor Medicaid", "PY02"),
            ("HARBOR MEDICAID PLAN", "PY02"),
            ("SUMMIT CARE", "PY03"),
            ("Summit Care Partners", "PY03"),
            ("NORTHWIND HP", "PY01"),
        ):
            claim = _claim(payer=raw)
            self.assertEqual(claim.payer, payer)

    def test_duplicate_history_rows_are_dropped(self):
        contents = (
            "claim_id,agency_id,payer,patient_id,service_code,date_of_service,"
            "submitted_date,units,auth_id,modifier,frequency_code,original_claim_id,"
            "billed_amount,status,paid_amount,denial_code,remark\n"
            "C1,AG001,PY01,PT0001,S5125,2026-07-15,2026-07-16,4,AUTH1,,1,,21.6,paid,21.6,,\n"
            "C1,AG001,PY01,PT0001,S5125,2026-07-15,2026-07-16,4,AUTH1,,1,,21.6,paid,21.6,,\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as handle:
            handle.write(contents)
            path = handle.name
        claims, dropped = load_claims(path, "history", labeled=True)
        self.assertEqual(dropped, 1)
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].status, "paid")


class BulletinTests(unittest.TestCase):
    def test_northwind_filing_limit_follows_service_date(self):
        # 30 calendar days through 31 July. Day 30 is paid, day 31 is D03.
        # Submitted date can fall after the 1 August bulletin; the service date governs.
        paid = _one([_claim(date_of_service="2026-07-31", submitted_date="2026-08-30")])
        self.assertEqual((paid.status, paid.denial_code), ("paid", ""))
        late = _one([_claim(date_of_service="2026-07-31", submitted_date="2026-08-31")])
        self.assertEqual((late.status, late.denial_code), ("denied", "D03"))

        # On and after 1 August the limit is 45 days. 1 August 2026 is a Saturday.
        paid = _one([_claim(date_of_service="2026-08-01", submitted_date="2026-09-15")])
        self.assertEqual((paid.status, paid.denial_code), ("paid", ""))
        late = _one([_claim(date_of_service="2026-08-01", submitted_date="2026-09-16")])
        self.assertEqual((late.status, late.denial_code), ("denied", "D03"))

    def test_harbor_weekend_modifier_follows_service_date(self):
        def harbor(service_day, modifier):
            claim = _claim(
                claim_id="H1",
                payer="Harbor Medicaid Plan",
                service_code="T1019",
                modifier=modifier,
                date_of_service=service_day,
                submitted_date="2026-11-01",
                units="4",
            )
            auth = _auth(payer="PY02", service_code="T1019")
            visit = _minutes(claim.date_of_service, 60, service="T1019")
            return _one([claim], auths=[auth], visits=[visit])

        # 29 August 2026 is a Saturday, still on U2. 30 August is a Sunday.
        self.assertEqual(harbor("2026-08-29", "U2").denial_code, "")
        self.assertEqual(harbor("2026-08-30", " u2 ").denial_code, "")
        self.assertEqual(harbor("2026-08-29", "U3").denial_code, "D04")
        # 5 September is a Saturday and 6 September a Sunday: U3. 7 September is a Monday: U2.
        self.assertEqual(harbor("2026-09-05", "U3").denial_code, "")
        self.assertEqual(harbor("2026-09-06", "u3").denial_code, "")
        self.assertEqual(harbor("2026-09-05", "U2").denial_code, "D04")
        self.assertEqual(harbor("2026-09-07", "U2").denial_code, "")
        self.assertEqual(harbor("2026-09-07", "U3").denial_code, "D04")

    def test_labels_on_the_claim_are_not_the_decision(self):
        row = _claim()
        labeled = claim_from_row(
            {
                "claim_id": row.claim_id,
                "agency_id": row.agency_id,
                "payer": row.payer,
                "patient_id": row.patient_id,
                "service_code": row.service_code,
                "date_of_service": "2026-07-15",
                "submitted_date": "2026-07-16",
                "units": "4",
                "auth_id": "AUTH1",
                "modifier": "",
                "frequency_code": "1",
                "original_claim_id": "",
                "status": "denied",
                "denial_code": "D01",
                "remark": "no",
            },
            "history",
            1,
            labeled=True,
        )
        decision = _one([labeled])
        self.assertEqual(decision.status, "paid")
        self.assertEqual(decision.denial_code, "")


class RoundingAndEvvTests(unittest.TestCase):
    def _payer_claim(self, payer, units, minutes, service="S5125"):
        claim = _claim(payer=payer, units=str(units), service_code=service)
        auth = _auth(payer=claim.payer, service_code=service)
        visit = _minutes(claim.date_of_service, minutes, service=service)
        return _one([claim], auths=[auth], visits=[visit])

    def test_eight_minute_rule_versus_complete_increments(self):
        # 22 minutes: one unit under both rules. 23 minutes: two under the
        # 8-minute rule, one under complete increments.
        self.assertEqual(self._payer_claim("PY01", 1, 22).status, "paid")
        self.assertEqual(self._payer_claim("PY02", 1, 22).status, "paid")
        self.assertEqual(self._payer_claim("PY01", 2, 22).denial_code, "D09")
        self.assertEqual(self._payer_claim("PY01", 2, 23).status, "paid")
        self.assertEqual(self._payer_claim("PY02", 2, 23).denial_code, "D09")
        self.assertEqual(self._payer_claim("PY03", 2, 23).denial_code, "D09")
        # A short verified visit is D09, not "no visit".
        self.assertEqual(self._payer_claim("PY01", 1, 7).denial_code, "D09")
        self.assertEqual(self._payer_claim("PY01", 1, 8).status, "paid")

    def test_only_verified_minutes_count_and_midnight_uses_clock_in(self):
        day = date(2026, 7, 15)
        claim = _claim()
        pending = _visit(
            datetime(2026, 7, 15, 8, 0),
            datetime(2026, 7, 15, 9, 0),
            status="manual_edit_pending",
        )
        rejected = _visit(
            datetime(2026, 7, 15, 10, 0),
            datetime(2026, 7, 15, 11, 0),
            status="rejected",
        )
        decision = _one([claim], visits=[pending, rejected])
        self.assertEqual(decision.denial_code, "D01")

        # 23:00 to 01:00 is 120 minutes on the clock-in date, eight units.
        overnight = _visit(datetime(2026, 7, 15, 23, 0), datetime(2026, 7, 16, 1, 0))
        paid = _one([_claim(units="8")], visits=[overnight])
        self.assertEqual(paid.status, "paid")
        next_day = _one(
            [_claim(claim_id="C2", date_of_service="2026-07-16", units="8")],
            visits=[overnight],
        )
        self.assertEqual(next_day.denial_code, "D01")

        # 10 + 10 minutes from two caregivers is one billable Harbor unit.
        first = _minutes(day, 10, service="S5125")
        second = Visit(
            "V2",
            "PT0001",
            "S5125",
            datetime(2026, 7, 15, 12, 0),
            datetime(2026, 7, 15, 12, 10),
            "verified",
        )
        harbor = _claim(payer="PY02", units="1")
        summed = _one([harbor], auths=[_auth(payer="PY02")], visits=[first, second])
        self.assertEqual(summed.status, "paid")

    def test_missing_visit_is_d01_even_though_units_also_exceed_zero(self):
        decision = _one([_claim()], visits=[])
        self.assertEqual(decision.denial_code, "D01")
        self.assertIn("D09", decision.fired)


class LedgerTests(unittest.TestCase):
    def test_submission_order_draws_auth_units_and_denied_claims_do_not(self):
        # Passed in id order, which is not submission order. Different service
        # dates so the second claim is not a duplicate.
        early = _claim(claim_id="C2", date_of_service="2026-07-15", submitted_date="2026-07-16", units="8")
        late = _claim(claim_id="C1", date_of_service="2026-07-18", submitted_date="2026-07-20", units="8")
        auths = [_auth(units_authorized=10)]
        visits = [
            _minutes(date(2026, 7, 15), 120),
            _minutes(date(2026, 7, 18), 120),
        ]
        first, second = _decide([late, early], auths=auths, visits=visits)
        by_id = {item.claim.claim_id: item for item in (first, second)}
        self.assertEqual(by_id["C2"].status, "paid")
        self.assertEqual(by_id["C1"].denial_code, "D02")

        # A claim denied for no EVV does not consume the 8 units.
        no_visit = _claim(claim_id="C0", date_of_service="2026-07-10", submitted_date="2026-07-11", units="8")
        later = _claim(claim_id="C3", date_of_service="2026-07-12", submitted_date="2026-07-13", units="8")
        decisions = _decide(
            [no_visit, later],
            auths=[_auth(units_authorized=8)],
            visits=[_minutes(date(2026, 7, 12), 120)],
        )
        by_id = {item.claim.claim_id: item for item in decisions}
        self.assertEqual(by_id["C0"].denial_code, "D01")
        self.assertEqual(by_id["C3"].status, "paid")

    def test_frequency_7_is_not_a_duplicate(self):
        original = _claim(claim_id="C1")
        correction = _claim(
            claim_id="C2",
            frequency_code="7",
            original_claim_id="C1",
            submitted_date="2026-07-20",
        )
        # Same date of service. The original is paid; the correction is a second
        # claim and would be D05 if frequency 7 were ignored. Auth has room.
        decisions = _decide([original, correction], auths=[_auth(units_authorized=20)])
        by_id = {item.claim.claim_id: item for item in decisions}
        self.assertEqual(by_id["C1"].status, "paid")
        self.assertEqual(by_id["C2"].status, "paid")
        self.assertIn("corrected_claim", by_id["C2"].review_reasons)
        third = _claim(claim_id="C3", submitted_date="2026-07-21")
        decisions = _decide(
            [original, correction, third],
            auths=[_auth(units_authorized=30)],
        )
        by_id = {item.claim.claim_id: item for item in decisions}
        self.assertEqual(by_id["C3"].denial_code, "D05")

    def test_harbor_daily_max_is_paid_units_across_services(self):
        personal = _claim(
            claim_id="P",
            payer="PY02",
            service_code="T1019",
            modifier="U2",
            units="20",
            date_of_service="2026-07-15",
            submitted_date="2026-07-16",
            auth_id="A1",
        )
        respite = _claim(
            claim_id="R",
            payer="PY02",
            service_code="T1005",
            units="13",
            date_of_service="2026-07-15",
            submitted_date="2026-07-17",
            auth_id="A2",
        )
        auths = [
            _auth(auth_id="A1", payer="PY02", service_code="T1019", units_authorized=40),
            _auth(auth_id="A2", payer="PY02", service_code="T1005", units_authorized=40),
        ]
        visits = [
            _minutes(date(2026, 7, 15), 300, service="T1019"),
            _minutes(date(2026, 7, 15), 300, service="T1005"),
        ]
        # 20 + 12 would be 32, which is allowed. 20 + 13 is over.
        under = _claim(
            claim_id="R",
            payer="PY02",
            service_code="T1005",
            units="12",
            date_of_service="2026-07-15",
            submitted_date="2026-07-17",
            auth_id="A2",
        )
        ok = _decide([personal, under], auths=auths, visits=visits)
        self.assertTrue(all(item.status == "paid" for item in ok))
        over = _decide([personal, respite], auths=auths, visits=visits)
        by_id = {item.claim.claim_id: item for item in over}
        self.assertEqual(by_id["P"].status, "paid")
        self.assertEqual(by_id["R"].denial_code, "D06")

    def test_summit_reports_auth_before_units_northwind_does_the_opposite(self):
        # 30 verified minutes support 2 units. Billing 4 with no auth number
        # trips both D09 and D07.
        def run(payer):
            claim = _claim(payer=payer, units="4", auth_id="")
            visit = _minutes(claim.date_of_service, 30)
            return _one([claim], auths=[_auth(payer=claim.payer)], visits=[visit])

        self.assertEqual(run("PY01").denial_code, "D09")
        self.assertEqual(run("Northwind HP").denial_code, "D09")
        self.assertEqual(run("PY03").denial_code, "D07")

    def test_partial_summit_hour_is_flagged_and_not_auto_denied(self):
        # 90 minutes is 6 complete units, which is not a whole hour.
        claim = _claim(payer="SUMMIT CARE", units="6")
        decision = _one(
            [claim],
            auths=[_auth(payer="PY03")],
            visits=[_minutes(claim.date_of_service, 90)],
        )
        self.assertEqual(decision.status, "paid")
        self.assertIn("summit_partial_hour", decision.review_reasons)


if __name__ == "__main__":
    unittest.main()
