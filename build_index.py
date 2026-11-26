"""Refresh the data inside index.html from the CSV and markdown files.

index.html carries its own copy of the data so it works offline. This script rebuilds that copy:

    python build_index.py

It rewrites exactly two lines of index.html and leaves all the code alone:
    const DATA = {...};   claims, authorizations, EVV visits and denial codes  (from the .csv files + denial_codes.md)
    const DOCS = {...};   the rule books shown on the Rule books page            (from payers/*.md + billing_basics.md)

Run it after changing any CSV or rule-book file. Reload the page afterwards.
Your browser keeps its own saved state (agency, edited rules, applied fixes, submitted flags);
see "Resetting the browser" in README.md if you want that cleared too.
"""
import csv
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PAGE = ROOT / "index.html"

# rule books shown on the Rule books page: id, title, group, file, how sections are split
DOCFILES = [
    ("PY01_manual",   "Northwind provider manual",  "Northwind", "payers/PY01_northwind_provider_manual.md", "section"),
    ("PY01_bulletin", "Northwind bulletin 2026-07", "Northwind", "payers/PY01_northwind_bulletin_2026-07.md", "section"),
    ("PY02_manual",   "Harbor provider manual",     "Harbor",    "payers/PY02_harbor_provider_manual.md", "section"),
    ("PY02_bulletin", "Harbor notice 2026-08",      "Harbor",    "payers/PY02_harbor_bulletin_2026-08.md", "section"),
    ("PY03_manual",   "Summit provider handbook",   "Summit",    "payers/PY03_summit_provider_manual.md", "para"),
    ("basics",        "Billing basics",             "General",   "billing_basics.md", "para"),
]


def read_csv(name):
    with open(ROOT / name, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def iso(s):
    """Exports mix YYYY-MM-DD and MM/DD/YYYY (US order: the first field never exceeds 12 in this data)."""
    s = s.strip()
    m = re.fullmatch(r"(\d\d)/(\d\d)/(\d{4})", s)
    return f"{m[3]}-{m[1]}-{m[2]}" if m else s


def payer(s):
    t = s.lower()
    if "py01" in t or "northwind" in t:
        return "PY01"
    if "py02" in t or "harbor" in t:
        return "PY02"
    if "py03" in t or "summit" in t:
        return "PY03"
    raise ValueError(f"unrecognised payer: {s!r}")


def build_data():
    claims, seen = [], set()
    for kind, name in (("history", "claims_history.csv"), ("to_check", "claims_to_check.csv")):
        for r in read_csv(name):
            key = tuple(r.items())
            if key in seen:                      # exact repeated rows in the export
                continue
            seen.add(key)
            claims.append(dict(
                id=r["claim_id"], set=kind, agency=r["agency_id"], payer=payer(r["payer"]), patient=r["patient_id"],
                service=r["service_code"].strip(), dos=iso(r["date_of_service"]), submitted=iso(r["submitted_date"]),
                units=int(r["units"]), auth=r["auth_id"].strip(), modifier=r["modifier"].strip().upper(),
                freq=r["frequency_code"].strip(), orig=r["original_claim_id"].strip(), billed=float(r["billed_amount"]),
                actualStatus=r.get("status", ""), actualCode=r.get("denial_code", "").strip(), remark=r.get("remark", "").strip()))
    auths = {r["auth_id"]: dict(patient=r["patient_id"], payer=r["payer_id"], service=r["service_code"],
                                start=r["start_date"], end=r["end_date"], units=int(r["units_authorized"]))
             for r in read_csv("authorizations.csv")}
    evv = []
    for r in read_csv("evv_visits.csv"):
        ci = datetime.strptime(r["clock_in"], "%Y-%m-%d %H:%M")
        co = datetime.strptime(r["clock_out"], "%Y-%m-%d %H:%M")
        evv.append(dict(p=r["patient_id"], s=r["service_code"], d=ci.date().isoformat(), i=r["clock_in"], o=r["clock_out"],
                        m=int((co - ci).total_seconds() // 60), st=r["verification_status"]))
    codes = []
    for line in (ROOT / "denial_codes.md").read_text(encoding="utf-8").splitlines():
        m = re.match(r"\|\s*(D\d\d)\s*\|\s*(.+?)\s*\|", line)
        if m:
            codes.append([m[1], m[2]])
    return dict(claims=claims, auths=auths, evv=evv, codes=codes)


def build_docs():
    docs = {}
    for did, title, group, f, mode in DOCFILES:
        text = (ROOT / f).read_text(encoding="utf-8").replace("\r\n", "\n")
        docs[did] = dict(title=title, group=group, file=f, mode=mode, text=text)
    return docs


def replace_line(page, prefix, value_js):
    """Swap the single line that starts with `prefix` (e.g. 'const DATA = ')."""
    lines = page.split("\n")
    hits = [i for i, l in enumerate(lines) if l.startswith(prefix)]
    if len(hits) != 1:
        sys.exit(f"index.html: expected exactly one line starting with {prefix!r}, found {len(hits)}")
    lines[hits[0]] = prefix + value_js + ";"
    return "\n".join(lines)


def main():
    data, docs = build_data(), build_docs()
    old = PAGE.read_text(encoding="utf-8")
    new = replace_line(old, "const DATA = ", json.dumps(data, separators=(",", ":")))
    new = replace_line(new, "const DOCS = ", json.dumps(docs, ensure_ascii=False).replace("</", "<\\/"))
    h = sum(c["set"] == "history" for c in data["claims"])
    print(f"{len(data['claims'])} claims ({h} history, {len(data['claims']) - h} to check), "
          f"{len(data['auths'])} authorizations, {len(data['evv'])} EVV visits, "
          f"{len(data['codes'])} denial codes, {len(docs)} rule books")
    if new == old:
        print("index.html already matches the CSV files. Nothing changed.")
        return
    PAGE.write_text(new, encoding="utf-8")
    print("index.html updated. Reload the page.")


if __name__ == "__main__":
    main()
