"""Rule-based claim outcome predictor, hardcoded from each payer's manual / bulletin.

PURPOSE
    Predict, for every claim, whether the payer will PAY or DENY it and, if denied,
    which denial code (D01..D10) it will report. Every rule below cites the document it
    comes from so it can be reviewed against the source text in /payers.

HOW TO REVIEW
    Search for "SOURCE:" to jump to the manual quote behind each rule.
    Search for "ASSUMPTION:" for anything I had to interpret or infer rather than read.
    Search for "DATA-DERIVED:" for the one rule that is NOT in any manual (Summit contract).

OUTPUTS
    predictions.csv          claim_id,status,denial_code   for claims_to_check.csv (README format)
    predictions_history.csv  same, for claims_history.csv, with the actual outcome alongside
                             (a scorecard for the rules is printed to the console)

Usage: python -I predict_rules.py [data_dir]
"""
import csv
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent

# =====================================================================================
# 1. PAYER RULE CONFIGURATION  (the part to review)
# =====================================================================================

# --- EVV unit rounding: how many billable units does a block of verified minutes support? ---
def units_from_minutes_8min_rule(minutes):
    # SOURCE (Northwind manual 4.3): "Northwind follows the 8-minute rule: after the last full
    # 15-minute unit, a remaining 8 minutes or more may be billed as one further unit;
    # 7 minutes or fewer may not."
    # verified
    full, rem = divmod(minutes, 15)
    return full + (1 if rem >= 8 else 0)


def units_from_minutes_whole_only(minutes):
    # SOURCE (Harbor billing guide, "Visit verification"): "Only complete 15-minute increments of
    # verified time may be billed. Partial units are not billable."
    # SOURCE (Summit handbook 9.1): "Bill in 15-minute units of verified time; partial units are
    # not billable, and units above verified time will be denied."
    return minutes // 15


def northwind_filing_limit_days(dos):
    # SOURCE (Northwind manual 6.1): "Claims must be received within 30 calendar days of the date of service."
    # SOURCE (Northwind bulletin 2026-07): "For dates of service on or after 1 August 2026, claims must
    #   be received within 45 calendar days of the date of service. The 30-day limit continues to
    #   apply to earlier dates of service."
    # The switch is keyed on the DATE OF SERVICE, exactly as the bulletin is worded (not the submission date).
    
    return 45 if dos >= date(2026, 8, 1) else 30


def harbor_required_modifier(service, dos):
    # SOURCE (Harbor billing guide, "Modifiers"): "Personal care (T1019) must be billed with
    #   modifier U2. Claims for T1019 without the correct modifier are denied."
    # SOURCE (Harbor notice 2026-08): "from dates of service on 1 September 2026, personal care (T1019)
    #   delivered on a Saturday or Sunday must be billed with modifier U3 instead of U2.
    #   Weekday T1019 continues to use U2."
    # ASSUMPTION: Harbor documents no modifier rule for T1005 / S5125, so those are never flagged.
    if service != "T1019":
        return None
    is_weekend = dos.weekday() >= 5  # Mon=0 ... Sat=5, Sun=6
    return "U3" if (is_weekend and dos >= date(2026, 9, 1)) else "U2"


PAYERS = {
    "PY01": {  # Northwind Health Plan
        "name": "Northwind",
        "units_from_minutes": units_from_minutes_8min_rule,
        # SOURCE (manual 4.2): EVV required for T1019, T1005 and S5125.
        # SOURCE (manual 4.5): "Every claim must carry the Northwind prior authorization number covering
        #   the member, service and date of service. Claims that exceed the units remaining on the
        #   authorization will be denied."
        "filing_limit_days": northwind_filing_limit_days,
        # SOURCE (manual 6.4): "Northwind denies any claim that duplicates one already received."
        "required_modifier": lambda service, dos: None,  # ASSUMPTION: none documented for Northwind
        "daily_max_units": None,                         # ASSUMPTION: none documented for Northwind
    },
    "PY02": {  # Harbor Medicaid Plan
        "name": "Harbor",
        "units_from_minutes": units_from_minutes_whole_only,
        "filing_limit_days": lambda dos: None,  # SOURCE: "Harbor does not apply a timely filing limit to in-state HCBS providers."
        "required_modifier": harbor_required_modifier,
        # SOURCE (Harbor "Daily maximum"): "A member may receive no more than 32 units per day across all
        #   HCBS services combined. Claims that would take the member's paid total for the day above
        #   32 units are denied."
        "daily_max_units": 32,
        # SOURCE (Harbor "Duplicates"): "Harbor checks every claim against its claims history and denies duplicates."
    },
    "PY03": {  # Summit Care Partners
        "name": "Summit",
        "units_from_minutes": units_from_minutes_whole_only,
        # SOURCE (handbook 9.3): "Claims must be received within 60 calendar days of the date of service."
        "filing_limit_days": lambda dos: 60,
        "required_modifier": lambda service, dos: None,  # ASSUMPTION: none documented for Summit
        "daily_max_units": None,                         # ASSUMPTION: none documented for Summit
        # SOURCE (handbook 9.1/9.2/9.4): EVV required, auth mandatory + balance, duplicates denied.
        # SOURCE (handbook 9.5): "Additional billing requirements may apply under your agency's contract
        #   with Summit."  <- hints at a hidden contract rule; see SUMMIT_WHOLE_HOUR_RULE below.
    },
}

# DATA-DERIVED: NOT in any manual. Summit's contract denials (D10) all carry the remark
# "Contract amendment 3: services must be billed in whole hours" / "Units not billable per amendment 3
# (hourly increments)". In claims_history.csv every Summit claim with units not divisible by 4 and a
# date of service on/after 2026-07-06 is denied D10, while non-whole-hour Summit claims were still
# paid up to DOS 2026-06-30. I therefore assume amendment 3 starts for DOS >= 2026-07-01.
# Set to None to switch this rule off and see how much of the score depends on it.
SUMMIT_WHOLE_HOUR_RULE = {"effective_dos": date(2026, 7, 1), "units_per_hour": 4}

# --- When one claim breaks several rules the payer reports ONE code. Earlier in the list wins. ---
# DATA-DERIVED: the manuals do not say what the order is, so this was read off claims_history.csv by
# looking at claims that break 2+ rules and seeing which code the payer actually reported.
# Findings (n = number of history claims showing it):
#   * D01 (no EVV) beats everything it co-occurs with (D02,D03,D04,D06,D07,D08,D10).
#   * D03 (late filing) loses to nearly every other code -> it is last. D02 (auth balance) is next-to-last.
#   * D06 (daily max) beats D02; D04 (modifier) beats D06; D05 (duplicate) beats D06.
#   * It is PAYER-SPECIFIC for the EVV-units rule (D09) vs the authorization rules (D07/D08):
#       Northwind & Harbor report D09 over D07/D08 (n=11, 0 exceptions).
#       Summit reports D07/D08 over D09 (n=6, 0 exceptions), and its contract denial D10 over D09 (n=12).
#   * D05 sits below D09/D10 (1 case each) but above D06 (4 cases); one lone case has D03 over D05.
#     The relationships D03>D05>D06>D02>D03 form a loop, so the one-off D03>D05 case is left unexplained.
PRECEDENCE = {
    "PY01": ["D01", "D09", "D07", "D08", "D04", "D10", "D05", "D06", "D02", "D03"],
    "PY02": ["D01", "D09", "D07", "D08", "D04", "D10", "D05", "D06", "D02", "D03"],
    "PY03": ["D01", "D07", "D08", "D10", "D09", "D04", "D05", "D06", "D02", "D03"],
}

# Rules for which the denial code is never predicted:
#   D99 "Other / referred for manual review" -- no manual describes it; it looks random in the data.

# =====================================================================================
# 2. DATA LOADING + CLEANING
# =====================================================================================

def read(name):
    with open(DIR / name, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def norm_date(s):
    """Exports mix YYYY-MM-DD and MM/DD/YYYY (US order; verified: the first field never exceeds 12)."""
    s = s.strip()
    m = re.fullmatch(r"(\d\d)/(\d\d)/(\d{4})", s)
    return date(int(m[3]), int(m[1]), int(m[2])) if m else date.fromisoformat(s)


def norm_payer(s):
    """Exports use payer ids and several spellings of the names."""
    t = s.lower()
    for pid, key in (("PY01", "northwind"), ("PY02", "harbor"), ("PY03", "summit")):
        if pid.lower() in t or key in t:
            return pid
    raise ValueError(f"unknown payer {s!r}")


def load_claims():
    claims, seen = [], set()
    for src, name in (("history", "claims_history.csv"), ("to_check", "claims_to_check.csv")):
        for r in read(name):
            key = tuple(r.items())
            if key in seen:  # 32 history rows are exact repeats of another row (export artefact)
                continue
            seen.add(key)
            claims.append({
                "set": src,
                "id": r["claim_id"],
                "payer": norm_payer(r["payer"]),
                "patient": r["patient_id"],
                "service": r["service_code"].strip(),           # some have trailing spaces
                "dos": norm_date(r["date_of_service"]),
                "submitted": norm_date(r["submitted_date"]),
                "units": int(r["units"]),
                "auth": r["auth_id"].strip(),
                "modifier": r["modifier"].strip().upper(),      # some are lower-case / padded
                "freq": r["frequency_code"].strip(),
                "actual_status": r.get("status", ""),
                "actual_code": r.get("denial_code", "").strip(),
            })
    return claims


def load_auths():
    return {r["auth_id"]: {"patient": r["patient_id"], "payer": r["payer_id"], "service": r["service_code"],
                           "start": date.fromisoformat(r["start_date"]), "end": date.fromisoformat(r["end_date"]),
                           "units": int(r["units_authorized"])} for r in read("authorizations.csv")}


def load_evv():
    """Index visits by (patient, service, clock-in date).
    SOURCE (billing_basics.md): "A visit belongs to the date the caregiver clocked in, even when it runs
    past midnight. If a caregiver visits the same patient for the same service more than once in a day,
    payers add the visits together."
    """
    idx = defaultdict(list)
    for v in read("evv_visits.csv"):
        ci = datetime.strptime(v["clock_in"], "%Y-%m-%d %H:%M")
        co = datetime.strptime(v["clock_out"], "%Y-%m-%d %H:%M")
        idx[(v["patient_id"], v["service_code"], ci.date())].append(
            {"status": v["verification_status"], "minutes": int((co - ci).total_seconds() // 60)})
    return idx


# =====================================================================================
# 3. THE RULES  (each returns a denial code if violated, else None)
# =====================================================================================

def check_evv_exists(c, ctx):
    # D01 "No verified EVV visit for the date of service"
    # SOURCE: every payer requires a VERIFIED EVV visit for the member, service and date of service.
    # billing_basics.md: "Payers treat only verified visits as evidence that care took place" -- so
    # `manual_edit_pending` and `rejected` visits do not count.
    visits = ctx["evv"].get((c["patient"], c["service"], c["dos"]), [])
    verified = [v for v in visits if v["status"] == "verified"]
    return "D01" if not verified else None


def check_evv_units(c, ctx):
    # D09 "Billed units exceed the EVV-verified time"
    # SOURCE: Northwind 4.3, Harbor "Visit verification", Summit 9.1 -- units above what verified time
    # supports are denied. Verified minutes of all same-day visits are added, then payer rounding applies.
    visits = ctx["evv"].get((c["patient"], c["service"], c["dos"]), [])
    verified_minutes = sum(v["minutes"] for v in visits if v["status"] == "verified")
    if verified_minutes == 0:
        return None  # nothing verified -> that is D01's job, not D09's
    supported = PAYERS[c["payer"]]["units_from_minutes"](verified_minutes)
    return "D09" if c["units"] > supported else None


def check_auth_present(c, ctx):
    # D07 "Prior authorization number missing"
    # SOURCE: Northwind 4.5 ("Every claim must carry the ... prior authorization number"),
    # Summit 9.2 ("mandatory on every claim"), Harbor "Authorizations" ("must reference the member's
    # current Harbor authorization").
    return "D07" if not c["auth"] else None


def check_auth_valid(c, ctx):
    # D08 "Authorization not valid for this patient, service or date of service"
    # SOURCE: the authorization must be "for the member and service billed and cover the date of
    # service" (Summit 9.2; Northwind 4.5; Harbor "current ... authorization for the service billed").
    # ASSUMPTION: an auth issued by a different payer, or an unknown auth number, is also invalid.
    if not c["auth"]:
        return None  # missing auth is D07's job
    a = ctx["auths"].get(c["auth"])
    if a is None:
        return "D08"
    ok = (a["patient"] == c["patient"] and a["service"] == c["service"] and a["payer"] == c["payer"]
          and a["start"] <= c["dos"] <= a["end"])
    return None if ok else "D08"


def check_auth_balance(c, ctx):
    # D02 "Units exceed the authorization's remaining units"
    # SOURCE: Northwind 4.5, Harbor "Authorizations" ("Units are drawn down as claims are paid; claims
    # above the remaining balance are denied"), Summit 9.2 ("Paid units are deducted").
    # billing_basics.md: units are drawn down "in the order claims are submitted".
    # Only evaluated when the auth is otherwise valid (otherwise D08/D07 apply).
    if check_auth_present(c, ctx) or check_auth_valid(c, ctx):
        return None
    remaining = ctx["auths"][c["auth"]]["units"] - ctx["auth_used"][c["auth"]]
    return "D02" if c["units"] > remaining else None


def check_timely_filing(c, ctx):
    # D03 "Claim received after the payer's timely filing limit"
    # SOURCE: see PAYERS[...]["filing_limit_days"]. Days are counted from date of service to submission.
    # ASSUMPTION: "within N days" means days_elapsed <= N is OK, so > N is late.
    limit = PAYERS[c["payer"]]["filing_limit_days"](c["dos"])
    if limit is None:
        return None
    return "D03" if (c["submitted"] - c["dos"]).days > limit else None


def check_modifier(c, ctx):
    # D04 "Missing or invalid modifier"
    # SOURCE: Harbor "Modifiers" + notice 2026-08 (see harbor_required_modifier). Other payers: no rule.
    need = PAYERS[c["payer"]]["required_modifier"](c["service"], c["dos"])
    return "D04" if (need and c["modifier"] != need) else None


def check_daily_max(c, ctx):
    # D06 "Daily unit maximum exceeded"
    # SOURCE: Harbor "Daily maximum" (32 units/day, all HCBS services combined, counted on PAID claims).
    # ASSUMPTION: "paid total for the day" = units of Harbor claims for the same member and date of
    # service that were paid earlier in submission order.
    cap = PAYERS[c["payer"]]["daily_max_units"]
    if cap is None:
        return None
    already = ctx["daily_paid"][(c["payer"], c["patient"], c["dos"])]
    return "D06" if already + c["units"] > cap else None


def check_duplicate(c, ctx):
    # D05 "Duplicate claim"
    # SOURCE: billing_basics.md: "A new claim (frequency code 1) for the same patient, service, date of
    # service and payer as one already submitted is a duplicate, whatever happened to the first one."
    # Corrected claims (frequency code 7) are "processed in [their] own right" and never duplicates.
    if c["freq"] != "1":
        return None
    key = (c["payer"], c["patient"], c["service"], c["dos"])
    return "D05" if ctx["seen_new_claims"][key] > 0 else None


def check_summit_contract(c, ctx):
    # D10 "Payer contract-specific denial"  -- DATA-DERIVED (see SUMMIT_WHOLE_HOUR_RULE above).
    r = SUMMIT_WHOLE_HOUR_RULE
    if r and c["payer"] == "PY03" and c["dos"] >= r["effective_dos"] and c["units"] % r["units_per_hour"] != 0:
        return "D10"
    return None


RULES = [check_duplicate, check_evv_exists, check_evv_units, check_auth_present, check_auth_valid,
         check_modifier, check_summit_contract, check_auth_balance, check_daily_max, check_timely_filing]


def predict(c, ctx):
    """Return (status, reported_code, all_codes_violated)."""
    fired = [code for rule in RULES if (code := rule(c, ctx))]
    if not fired:
        return "paid", "", []
    reported = min(fired, key=PRECEDENCE[c["payer"]].index)  # payer reports a single code
    return "denied", reported, fired


# =====================================================================================
# 4. RUN IN SUBMISSION ORDER (auth balances, daily totals and duplicates depend on order)
# =====================================================================================

def run(use_actual_history):
    """Process every claim in submission order, keeping running state.

    use_actual_history=False -> pure simulation: state is built from our own predictions
                                (honest scorecard on history, no peeking at outcomes).
    use_actual_history=True  -> history claims update state with their REAL outcome, so that
                                to-check claims see the true auth balances / daily totals / prior claims.
    """
    claims = sorted(load_claims(), key=lambda c: (c["submitted"], c["id"]))
    ctx = {"evv": load_evv(), "auths": load_auths(),
           "auth_used": Counter(), "daily_paid": Counter(), "seen_new_claims": Counter()}
    results = {}
    for c in claims:
        status, code, fired = predict(c, ctx)
        results[c["id"]] = (c, status, code, fired)
        # update running state with whatever we treat as the claim's outcome
        if use_actual_history and c["set"] == "history":
            paid = c["actual_status"] == "paid"
        else:
            paid = status == "paid"
        if paid:
            ctx["auth_used"][c["auth"]] += c["units"]
            ctx["daily_paid"][(c["payer"], c["patient"], c["dos"])] += c["units"]
        if c["freq"] == "1":  # a duplicate is judged against ANY earlier new claim, paid or not
            ctx["seen_new_claims"][(c["payer"], c["patient"], c["service"], c["dos"])] += 1
    return results


def write_csv(path, header, rows):
    with open(DIR / path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    print(f"wrote {path}: {len(rows)} rows")


def main():
    sim = run(use_actual_history=False)    # for scoring history
    live = run(use_actual_history=True)    # for predicting claims_to_check

    # ---- history scorecard ----
    hist = [v for v in sim.values() if v[0]["set"] == "history"]
    status_ok = sum(1 for c, s, code, f in hist if s == c["actual_status"])
    exact_ok = sum(1 for c, s, code, f in hist if (s, code) == (c["actual_status"], c["actual_code"]))
    tp = sum(1 for c, s, *_ in hist if s == "denied" and c["actual_status"] == "denied")
    fp = sum(1 for c, s, *_ in hist if s == "denied" and c["actual_status"] == "paid")
    fn = sum(1 for c, s, *_ in hist if s == "paid" and c["actual_status"] == "denied")
    print(f"\nHISTORY ({len(hist)} claims, predictions built from rules only)")
    print(f"  paid/denied correct : {status_ok}/{len(hist)} ({100*status_ok/len(hist):.1f}%)")
    print(f"  exact code correct  : {exact_ok}/{len(hist)} ({100*exact_ok/len(hist):.1f}%)")
    print(f"  denied: caught {tp}, missed {fn}, false alarms {fp}")
    print("  per actual denial code  (caught = we predicted that exact code):")
    by = defaultdict(lambda: [0, 0, Counter()])
    for c, s, code, f in hist:
        if c["actual_status"] == "denied":
            r = by[c["actual_code"]]
            r[0] += 1
            r[1] += code == c["actual_code"]
            r[2][code or "paid"] += 1
    for k in sorted(by):
        n, ok, miss = by[k]
        wrong = {m: v for m, v in miss.items() if m != k}
        print(f"    {k}: {ok}/{n}" + (f"   mis-predicted as {wrong}" if wrong else ""))

    write_csv("predictions_history.csv",
              ["claim_id", "predicted_status", "predicted_code", "actual_status", "actual_code", "all_rules_violated"],
              [[c["id"], s, code, c["actual_status"], c["actual_code"], ";".join(f)] for c, s, code, f in sorted(hist, key=lambda x: x[0]["id"])])

    # ---- predictions for claims still to be submitted ----
    todo = sorted((v for v in live.values() if v[0]["set"] == "to_check"), key=lambda x: x[0]["id"])
    write_csv("predictions.csv", ["claim_id", "status", "denial_code"], [[c["id"], s, code] for c, s, code, f in todo])
    print("  to_check:", dict(Counter(code or "paid" for c, s, code, f in todo)))


if __name__ == "__main__":
    main()
