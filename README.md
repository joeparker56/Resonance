# Exchange4Health claims data pack (synthetic)

All data here is made up. It describes 12 home care agencies billing three payers between June and November 2026.

## Your goal
For each claim in `claims_to_check.csv`, work out whether the payer will pay or deny it and, if denied, which denial code the payer will report. How you build that, and how far you go beyond it, is up to you.

If you produce predictions, a `predictions.csv` with `claim_id,status,denial_code` lets us compare approaches on the day.

## Files
| File | What it is |
|---|---|
| `claims_history.csv` | About 3,800 claims already adjudicated, with status, denial code and the payer's remark. Exported from the billing system as-is. |
| `claims_to_check.csv` | About 550 claims not yet submitted. Same columns, no outcome. |
| `authorizations.csv` | Each payer authorization: who, which service, the dates it covers and the units approved. |
| `evv_visits.csv` | Electronic Visit Verification records: one row per visit, with clock-in, clock-out and verification status. |
| `billing_basics.md` | Exchange4Health's help article on how claims, EVV and authorizations fit together. |
| `denial_codes.md` | The denial codes payers use. |
| `payers/` | Each payer's provider manual excerpt, plus any provider bulletins they've issued this year. |

## Things to know
- One unit is 15 minutes. `billed_amount` is units multiplied by a rate per service code.
- `submitted_date` is when a claim was, or will be, submitted. There is no "today": judge each claim on the dates it carries.
- When a claim breaks more than one rule, the payer reports one code only.
- The data comes from real-world-style exports, so expect some inconsistency in formats and naming.

## Prototype

A rules engine in `billing/` decides pay versus deny, and the denial code, from the manuals and the two bulletins. It does not fit a model. `EVAL.md` is produced by `python evaluate.py`.

```bash
python3 predict.py          # writes predictions.csv and needs_review.csv
python3 evaluate.py         # writes EVAL.md from a fresh run
python3 -m unittest discover -s tests
```

Standard library only. Run the commands from the repo root (the scripts also find the data next to themselves).

`predictions.csv` has one row per `claims_to_check.csv` claim: `claim_id,status,denial_code`. Status is `paid` or `denied`. The denial code is blank when the claim is paid. `needs_review.csv` is the queue for a biller: Summit claims whose units are not a whole hour, and frequency-7 corrections. The engine still fills in a code for those rows.

History rows that repeat a claim id verbatim are ignored after the first copy. Payer display names (`Northwind Health Plan`, `Harbor Medicaid`, `SUMMIT CARE`, and the rest) become PY01, PY02, PY03. Slash dates are month/day/year. Modifiers and service codes are stripped and upper-cased. Units are integer 15-minute units.

Checked claims are decided on one queue with the history. A history claim draws units only if it was paid. An unchecked claim draws units only if the rules would pay it. Order is submitted date, then claim id.

## Rules

| Code | When it fires | Source |
|---|---|---|
| D01 | No verified EVV minutes for the member, service and clock-in date. `manual_edit_pending` and `rejected` do not count. A visit that crosses midnight stays on the clock-in date. Same-day visits are added. | Northwind §4.2, Harbor "Visit verification", Summit §9.1, `billing_basics.md` |
| D09 | Billed units exceed the minutes. Northwind uses the 8-minute rule: `(minutes + 7) // 15`. Harbor and Summit bill only complete 15-minute increments: `minutes // 15`. | Northwind §4.3, Harbor visit verification, Summit §9.1 |
| D07 | Authorization number is blank. | Northwind §4.5, Harbor "Authorizations", Summit §9.2 |
| D08 | The number is unknown, or it is not this member, service, payer and date. Start and end dates count. | Same sections |
| D02 | Units exceed what is left on that authorization. Paid claims draw units down in submission order. Denied claims do not. | Same sections, plus `billing_basics.md` |
| D04 | Harbor T1019 modifier is wrong. U2, except Saturday and Sunday dates of service on or after 1 September 2026, which require U3. | Harbor "Modifiers", bulletin 2026-08 |
| D03 | Northwind: more than 30 calendar days after the service date, or more than 45 if the service date is on or after 1 August 2026. Summit: more than 60. Day 30, 45 or 60 is still on time. Harbor: no limit. | Northwind §6.1, bulletin 2026-07, Summit §9.3, Harbor "Filing limits" |
| D05 | Frequency 1, and the same payer, member, service and date of service was already submitted. A frequency-7 correction is not a duplicate. The original still blocks a later frequency-1 claim. | `billing_basics.md`, Northwind §6.4, Harbor "Duplicates", Summit §9.4 |
| D06 | Harbor only. Paid units for the member that day, across services, plus this claim, go over 32. | Harbor "Daily maximum" |

Bulletin dates are dates of service. A claim submitted in September for a July service still uses the old Northwind limit and the old Harbor modifier.

When several of these fire, one code is reported. History is consistent with this order (first hit wins):

- PY01: D01, D09, D08, D07, D02, D03, D05
- PY02: D01, D09, D04, D08, D07, D05, D06, D02
- PY03: D01, D08, D07, D09, D02, D03, D05

Northwind and Harbor report D09 ahead of a bad authorization. Summit reports D07 or D08 ahead of D09. Harbor reports the daily maximum only after the other Harbor codes, and reports D06 ahead of an exhausted authorization. The co-fire counts are in `EVAL.md`. Pairs that never occur together (a missing number cannot also be the wrong number) are ordered with the surrounding codes and do not change a result.

## Not turned into a rule

Summit §9.5 says a contract may add billing rules. D10 remarks name "amendment 3" and whole hours, and the frequency-7 corrections of those claims round units down to a multiple of 4. The same authorizations also pay partial hours, so the engine does not deny on the hour boundary. It flags the partial-hour Summit claims for review.

D99 has no pattern in the manuals. Those claims look payable. They stay misses, and they are not predictable enough to flag one by one.

Eight member-days have two verified visits. None of them change a unit count whether the minutes are summed or the longer visit is kept. The engine sums them.
