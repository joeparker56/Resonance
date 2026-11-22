"""Trace each claim to its EVV visit(s).

Matching rule (billing_basics.md): a visit belongs to the date the caregiver clocked in,
and multiple visits for the same patient + service + day are added together.
So a claim matches every visit with the same patient, service and clock-in date.

Outputs (written next to the input CSVs):
  claim_evv_trace.csv  one row per claim: match summary and units-vs-EVV comparison
  claim_evv_links.csv  one row per (claim, visit) pair, with the visit's full detail

Usage: python trace_evv.py [data_dir]
"""
import csv
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent
FMT = "%Y-%m-%d %H:%M"


def read(name):
    with open(DIR / name, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def norm_date(s):
    """Claims mix YYYY-MM-DD and MM/DD/YYYY."""
    s = s.strip()
    m = re.fullmatch(r"(\d\d)/(\d\d)/(\d{4})", s)
    return f"{m[3]}-{m[1]}-{m[2]}" if m else s


def norm_payer(s):
    t = s.lower()
    for pid, key in (("PY01", "northwind"), ("PY02", "harbor"), ("PY03", "summit")):
        if pid.lower() in t or key in t:
            return pid
    return s.strip()


def supported_units(payer, minutes):
    """Payer rounding: Northwind 8-minute rule; Harbor/Summit whole 15-min increments only."""
    full, rem = divmod(minutes, 15)
    return full + (1 if payer == "PY01" and rem >= 8 else 0)


# ---- index EVV visits by (patient, service, clock-in date) ----
visits = {}
by_key = defaultdict(list)
by_ps = defaultdict(list)
for v in read("evv_visits.csv"):
    ci, co = datetime.strptime(v["clock_in"], FMT), datetime.strptime(v["clock_out"], FMT)
    v["minutes"] = int((co - ci).total_seconds() // 60)
    v["in_date"] = ci.date().isoformat()
    v["crosses_midnight"] = ci.date() != co.date()
    visits[v["visit_id"]] = v
    by_key[(v["patient_id"], v["service_code"], v["in_date"])].append(v)
    by_ps[(v["patient_id"], v["service_code"])].append(v)

# ---- load claims (history + to-check), dropping exact duplicate rows ----
claims, seen = [], set()
for src, name in (("history", "claims_history.csv"), ("to_check", "claims_to_check.csv")):
    for r in read(name):
        key = tuple(r.items())
        if key in seen:
            continue
        seen.add(key)
        r["set"] = src
        r["payer"] = norm_payer(r["payer"])
        r["service_code"] = r["service_code"].strip()
        r["date_of_service"] = norm_date(r["date_of_service"])
        r["units"] = int(r["units"])
        claims.append(r)


def nearest_other_day(c):
    """For a claim with no same-day visit, find the closest visit for the same patient+service."""
    dos = datetime.strptime(c["date_of_service"], "%Y-%m-%d").date()
    best = None
    for v in by_ps.get((c["patient_id"], c["service_code"]), []):
        gap = abs((datetime.strptime(v["in_date"], "%Y-%m-%d").date() - dos).days)
        if gap <= 3 and (best is None or gap < best[0]):
            best = (gap, v)
    return best


trace_rows, link_rows = [], []
for c in claims:
    matched = by_key.get((c["patient_id"], c["service_code"], c["date_of_service"]), [])
    ver = [v for v in matched if v["verification_status"] == "verified"]
    all_min = sum(v["minutes"] for v in matched)
    ver_min = sum(v["minutes"] for v in ver)
    sup = supported_units(c["payer"], ver_min)
    if not matched:
        outcome = "no_visit_found"
    elif not ver:
        outcome = "no_verified_visit"
    elif c["units"] > sup:
        outcome = "units_exceed_verified_time"
    else:
        outcome = "ok"
    near = nearest_other_day(c) if not matched else None
    trace_rows.append({
        "claim_id": c["claim_id"], "set": c["set"], "payer": c["payer"],
        "patient_id": c["patient_id"], "service_code": c["service_code"],
        "date_of_service": c["date_of_service"], "units_billed": c["units"],
        "billed_minutes": c["units"] * 15,
        "status": c.get("status", ""), "denial_code": c.get("denial_code", ""),
        "visits_matched": len(matched),
        "visit_ids": ";".join(v["visit_id"] for v in matched),
        "visit_statuses": ";".join(v["verification_status"] for v in matched),
        "verified_visits": len(ver), "all_visit_minutes": all_min, "verified_minutes": ver_min,
        "units_supported_by_evv": sup, "unit_gap": c["units"] - sup,
        "evv_check": outcome,
        "nearest_visit_other_day": (f"{near[1]['visit_id']} on {near[1]['in_date']} ({near[0]}d off, {near[1]['verification_status']})"
                                    if near else ""),
    })
    for v in matched:
        link_rows.append({
            "claim_id": c["claim_id"], "visit_id": v["visit_id"], "caregiver_id": v["caregiver_id"],
            "patient_id": v["patient_id"], "service_code": v["service_code"],
            "clock_in": v["clock_in"], "clock_out": v["clock_out"], "minutes": v["minutes"],
            "crosses_midnight": v["crosses_midnight"], "verification_status": v["verification_status"],
        })


def write(name, rows):
    with open(DIR / name, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {name}: {len(rows)} rows")


write("claim_evv_trace.csv", trace_rows)
write("claim_evv_links.csv", link_rows)

# ---- quick summary: how does the EVV check line up with recorded outcomes? ----
print("\nevv_check vs recorded outcome (history only):")
tab = defaultdict(lambda: defaultdict(int))
for r in trace_rows:
    if r["set"] == "history":
        tab[r["evv_check"]][r["denial_code"] or "paid"] += 1
for k in sorted(tab):
    print(f"  {k:28s}", dict(sorted(tab[k].items(), key=lambda x: -x[1])))
print("\nto_check:", dict(sorted(
    {k: sum(1 for r in trace_rows if r["set"] == "to_check" and r["evv_check"] == k)
     for k in {r["evv_check"] for r in trace_rows}}.items())))
