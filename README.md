---
noteId: "bd2adc10e8ea11f1a8a6f5e4a61117b9"
tags: []

---

# How Claim Check works

Claim Check predicts whether a payer will accept or reject a home-care claim **before** it is submitted, explains which payer rule would cause a rejection, and uses Claude Haiku to suggest a fix the user can apply with one confirmed click.

It has two layers, and they are deliberately separate:

| Layer | Decides | How |
|---|---|---|
| **Rules engine** | Accept or reject, and which denial code | Plain, deterministic code written from the payer manuals. No AI. |
| **Suggested fix** | What to do about a rejection | Claude Haiku *proposes* one action as structured data. The app validates it against the real data, and the user confirms it. |

The prediction is never made by the model. The model only reads the engine's verdict and the evidence behind it.

---

## 1. Files

| File | Role |
|---|---|
| `index.html` | The whole dashboard: login, claim list, rules page, rule books, the rules engine (JavaScript), and the suggested-fix client. All data is embedded in the file. |
| `server.py` | Small local web server. Serves `index.html` and makes the one Anthropic API call. Holds the API key. |
| `predict_rules.py` | The same rules as a standalone Python script. Writes `predictions.csv` (unsubmitted claims) and `predictions_history.csv` (scorecard on history). |
| `claims_history.csv`, `claims_to_check.csv`, `authorizations.csv`, `evv_visits.csv` | The synthetic data pack. |
| `payers/*.md`, `billing_basics.md`, `denial_codes.md` | The rule books and denial codes. Copied verbatim into `index.html` for the Rule books page. |
| `requirements.txt` | Python dependency (`anthropic`). |

```
 browser (index.html)                         server.py                    Anthropic API
 ┌───────────────────────────┐   POST          ┌────────────────┐  HTTPS   ┌───────────────┐
 │ rules engine (JS)         │ /api/suggest-fix│ holds API key  │────────▶ │ claude-haiku  │
 │ builds claim context      │ ───────────────▶│ system prompt  │ ◀─────── │ -5-5          │
 │ validates + applies fix   │ ◀───────────────│ JSON schema    │ JSON     └───────────────┘
 └───────────────────────────┘  proposal       └────────────────┘
```

### Running it

```
.venv\Scripts\python server.py        # then open http://localhost:8000
```

`ANTHROPIC_API_KEY` must be set in the environment. The dashboard also opens directly as a file, and everything works except suggested fixes.

---

## 2. Data cleaning

The exports are inconsistent on purpose. Both the dashboard and `predict_rules.py` normalise them first:

| Problem in the data | Handling |
|---|---|
| Dates as `YYYY-MM-DD` **and** `MM/DD/YYYY` | Parsed as US order (the first field never exceeds 12 anywhere in the data). |
| Payer written as `PY01`, `Northwind Health Plan`, `NORTHWIND HP`, … | Mapped to `PY01` / `PY02` / `PY03` by matching the id or the name. |
| Service codes with trailing spaces (`"T1019 "`) | Trimmed. |
| Modifiers in mixed case or padded (`"u2"`, `" U1"`) | Trimmed and upper-cased. |
| 32 history rows that are exact repeats of another row | Dropped. This leaves 3,830 history claims. |

Result: **3,830 history claims + 546 unsubmitted = 4,376 claims.**

---

## 3. The rules engine

### 3.1 What it evaluates

For one claim it runs a list of checks. Each check returns pass or fail, a plain-English detail, and a pointer to the rule-book section it comes from. If any check fails the claim is predicted **rejected**. If several fail, the payer reports only one code (see 3.4).

### 3.2 State: claims are processed in submission order

Three rules depend on what happened to *other* claims. So all claims are sorted by `submitted_date` (ties broken by claim id) and processed one at a time, carrying running totals:

| Running state | Used by |
|---|---|
| Units already **paid** against each authorization | Authorization balance (D02) |
| Units already **paid** per payer, patient and date | Daily maximum (D06) |
| Count of earlier new claims per payer, patient, service and date | Duplicate (D05) |

How a claim updates that state depends on its outcome:

- **History claims** update the state with their **real** outcome (paid or denied). The payer's actual decisions are known.
- **Unsubmitted claims** update it with their **predicted** outcome.

So an unsubmitted claim sees the true authorization balance left after history, and an earlier unsubmitted claim that would be paid uses up balance for later ones.

### 3.3 The rules

Matching EVV to a claim: a visit belongs to the **date the caregiver clocked in**, even if it runs past midnight (`billing_basics.md`). A claim matches every visit with the same patient, service and clock-in date, and the minutes of several same-day visits are **added together**. Only visits with status `verified` count; `rejected` and `manual_edit_pending` do not.

| Code | Rule | Logic | Applies to |
|---|---|---|---|
| **D01** | Verified EVV required | Fails if no `verified` visit matches the claim. | All payers |
| **D09** | Units must fit verified EVV time | Verified minutes are converted to units, and the claim fails if it bills more. Only checked when D01 passes. **Northwind:** 8-minute rule, so after the last full 15 minutes, 8 or more leftover minutes count as one more unit. **Harbor, Summit:** whole 15-minute blocks only. | All payers |
| **D07** | Authorization number required | Fails if the claim has no `auth_id`. | All payers |
| **D08** | Authorization must match | Fails if the authorization does not exist, or is for a different patient, service or payer, or the date of service is outside its start and end dates. | All payers |
| **D02** | Units must fit the authorization balance | `units_authorized` minus units paid earlier on that authorization. Fails if the claim bills more. Only checked when D07 and D08 pass. | All payers |
| **D03** | Timely filing | Days from date of service to `submitted_date`. Fails if over the limit. **Northwind:** 30 days, or 45 days when the **date of service** is on or after 1 Aug 2026 (bulletin 2026-07). **Summit:** 60 days. **Harbor:** no limit. | Northwind, Summit |
| **D04** | Modifier | **Harbor, T1019 only:** must be `U2`, or `U3` on a Saturday or Sunday when the date of service is on or after 1 Sep 2026 (notice 2026-08). | Harbor |
| **D06** | Daily unit maximum | Harbor pays at most 32 units per patient per day across all services. Fails if units already paid that day plus this claim exceed 32. | Harbor |
| **D05** | Duplicate | A new claim (frequency `1`) fails if an earlier new claim exists for the same payer, patient, service and date of service, **whatever happened to the first one**. Corrected claims (frequency `7`) are never duplicates. | All payers |
| **D10** | Contract: whole hours | Units must be a multiple of 4 for dates of service on or after 1 Jul 2026. | Summit |
| **D99** | Other / manual review | **Never predicted.** No manual describes it and it looks random in the data. | – |

Where each rule comes from, with the quoted text, is in `predict_rules.py` (search for `SOURCE:`), and on the **Rule books** page in the dashboard.

### 3.4 Which code is reported when several rules fail

The manuals do not say. The order was read off the claims history by looking at claims that break two or more rules and seeing which code the payer actually reported:

```
Northwind, Harbor:  D01 > D09 > D07 > D08 > D04 > D10 > D05 > D06 > D02 > D03
Summit:             D01 > D07 > D08 > D10 > D09 > D04 > D05 > D06 > D02 > D03
```

Examples from the data: no EVV (D01) beats everything it appears with, and late filing (D03) loses to nearly everything. Northwind and Harbor report D09 ahead of D07/D08, while Summit reports them the other way round.

### 3.5 Two things that are inferred, not in a manual

1. **Summit D10 (whole hours).** Every Summit contract denial cites "Contract amendment 3: services must be billed in whole hours". The handbook only hints at it (§9.5 mentions contract terms). Non-whole-hour Summit claims were paid up to 30 June and denied from 6 July, so the rule is applied from 1 Jul 2026.
2. **The reporting order above.**

Both are labelled as inferred in the code and on the Rules page.

### 3.6 Accuracy

Measured against the 3,830 recorded outcomes with the default rules:

| Measure | Dashboard | `predict_rules.py` |
|---|---|---|
| Paid or denied correct | 3,768 (98.4%) | 3,765 (98.3%) |
| Exact denial code correct | 3,755 (98.0%) | 3,751 (97.9%) |

They differ slightly because the dashboard feeds the real outcome of history claims into the running state, while the script simulates everything from its own predictions. For the 546 unsubmitted claims the two give identical results, and 169 of them are predicted rejected.

Nearly all remaining errors are the 73 **D99** denials, which have no documentable rule. The Summit rule and the reporting order were tuned on this same history, so treat these figures as optimistic.

---

## 4. The Rules page: editing the rules

The rules are data, not hard-coded branches. `DEFAULT_RULES` in `index.html` holds one block per payer, for example:

```js
PY01: { evvUnits: {on:true, rounding:'8min'},
        filing:   {on:true, days:30, changeFrom:'2026-08-01', changeDays:45},
        modifier: {on:false, service:'T1019', weekday:'U2', weekend:'', weekendFrom:''},
        dailyMax: {on:false, units:32}, ... }
```

The Rules page edits a copy of this:

- Each rule has an on/off switch and its own settings (rounding method, filing limit and the date it changes, required modifier and weekend modifier, daily cap, whole-hour term).
- The "which code is reported" order is editable per payer.
- Every change re-runs all 4,376 claims at once and updates three figures against the manual defaults: how often paid or rejected matches the recorded outcome, how often the exact code matches, and how many unsubmitted claims are predicted rejected.
- Changed rules show an **edited** badge. **Reset to manual defaults** restores everything.

Edits are saved in the browser (`localStorage`, key `cc_rules`) and also change what the suggested-fix model is told about the payer's rules (see 5.2).

`predict_rules.py` does not read these edits. It always uses the manual defaults.

---

## 5. Suggested fix: how the API call works

### 5.1 When it happens

When a user selects a claim that is **predicted rejected** and **not yet submitted**, the card shows "Suggested fix:". After a 450 ms pause (so arrowing through the list does not fire a request per row) the page builds a context and posts it to the server. Accepted claims and already-submitted history claims never trigger a call. Results are cached for the session, keyed on the full context, so any change to the claim or the rules produces a fresh request.

### 5.2 What the model is given

`fixContext()` in `index.html` assembles everything the model may know, from the same data the engine uses:

| Section | Contents |
|---|---|
| `claim` | All claim fields in plain terms (payer name, service name, weekday, minutes billed). |
| `predicted_rejection` | The code that would be reported and every rule that failed, each with its detail text. |
| `active_rules_for_this_payer` | The payer's enabled rules and their current settings, including any edits from the Rules page. |
| `rule_book_excerpts` | The actual text of the manual sections behind each failed rule. |
| `evv_on_date_of_service` | Visits that day (start, end, minutes, status), verified minutes, and the units they support under the payer's rounding. |
| `unclaimed_evv_nearby` | Visits for the same patient and service within 7 days that **no claim covers**, with the units each would support. |
| `authorizations_for_patient` | Every authorization with its dates, units authorized, units used by earlier paid claims, units remaining, and whether it matches the claim's service, payer and date. |
| `related_claims` | Other claims for the same patient and date, and any corrected-claim links, with their outcomes. |
| `billing_codes` | Service codes, denial codes and the modifiers in use. |

This is about 3,500 tokens per claim.

### 5.3 The request

`server.py` forwards the context to the Anthropic Messages API using the official Python SDK:

| Setting | Value | Why |
|---|---|---|
| Model | `claude-haiku-5-5` (override with `CLAIM_FIX_MODEL`) | Fast and cheap enough to run per claim. |
| `max_tokens` | 4096 | Room for the answer plus a little reasoning. |
| `output_config.effort` | `low` | The task is bounded and the evidence is supplied. |
| `output_config.format` | JSON schema | The reply is guaranteed to be parseable structured data. |
| `system` | The instructions in `server.py` (`SYSTEM`) | See below. |
| Sampling parameters | none | Haiku 5.5 rejects non-default `temperature`. |
| SDK retries / timeout | 2 / 60 s | SDK defaults for 429 and 5xx. |

The **system prompt** tells the model it is a billing assistant, lists the allowed actions and exactly when each applies, and sets hard rules: use only facts in the context, never invent IDs, dates or units, never misrepresent care that was not delivered (if there is no EVV support, do not change units or dates to fit), and fix the highest-priority broken rule where one action can clear it. It also fixes the writing format: a summary under about 110 characters naming the specific values, short reasoning, and a 2 to 4 word button label.

The **response schema** (all fields required, unused ones are `0` or `""`):

```json
{ "action": "delete_claim | change_units | change_auth | change_modifier | change_date_of_service | hold_claim | no_fix",
  "summary": "...", "reasoning": "...", "button_label": "...",
  "units": 0, "auth_id": "", "modifier": "", "date_of_service": "", "note": "" }
```

`server.py` also checks `stop_reason` (a refusal or a cut-off answer becomes an error) and returns the parsed JSON plus token usage. Errors from the API are mapped to clean messages (401 bad key, 429 rate limited, 502 unreachable), and stack traces are never sent to the page.

### 5.4 The model proposes, the app decides

The reply is only text. Before a button appears, `validateFix()` checks it against the real data:

| Action | Applied only if | Effect |
|---|---|---|
| `delete_claim` | Claim is not yet submitted. | Removes the claim from the list. |
| `change_units` | New value is a whole number ≥ 1 and **lower** than the current units. Units can only be reduced. | Sets units and rescales the billed amount per unit. |
| `change_auth` | The authorization exists **and** matches the claim's patient, service and payer **and** covers the date of service. | Sets the authorization number. |
| `change_modifier` | Value is `U1`, `U2`, `U3` or blank, and differs from the current one. | Sets the modifier. |
| `change_date_of_service` | A **verified** EVV visit exists for that patient and service on that date, it is within 14 days, and no other claim already covers it. | Sets the date of service. |
| `hold_claim` | A note is given. | Marks the claim on hold. Submit then asks for confirmation. |
| `no_fix` | – | Text only, no button. |

If a suggestion fails validation, it is shown as text with the reason it cannot be applied automatically, and no button is offered.

### 5.5 Confirm, apply, undo

1. The user clicks the button (the model's label, for example "Reduce units to 36").
2. A confirm box shows **what will change, computed by the app**, such as `Billed units · C020321   38 → 36`. The model's wording is not trusted for this. Deletes use a red button.
3. On confirm, the claim is changed, the whole engine re-runs, and a toast reports the new prediction ("now predicted to be accepted" or "still predicted to be rejected (D09)").
4. **Undo** is available from the toast and from the claim card. It restores the snapshot taken before the change, including a deleted claim.

Applied fixes are saved in `localStorage` as small instructions (`cc_fixes`: claim id, action, values) and replayed on top of the original data when the page loads. The original data is never modified.

### 5.6 Security

- The API key never reaches the browser. It lives in the server's environment.
- The server listens on `127.0.0.1` only, and refuses any request whose `Host` header is not `localhost` or `127.0.0.1` on its own port (protects against DNS rebinding).
- There is no CORS header, so other websites in your browser cannot call the endpoint and spend your credits.
- Request bodies are limited to 256 KB.
- The model can only choose from a fixed list of actions, and the app validates the values. It cannot run anything.

### 5.7 Cost and speed

Roughly 3,400 input tokens and 200 to 400 output tokens per claim. At Haiku 5.5 prices ($0.10 / $0.50 per million tokens) that is about **$0.0005 per suggestion**, and it takes 4 to 7 seconds.

---

## 6. What is stored where

| Key (`localStorage`) | Holds |
|---|---|
| `cc_agency` | The agency chosen at login. |
| `cc_rules` | Edits made on the Rules page. |
| `cc_fixes` | Fixes applied from suggestions. |
| `submitted` | Which claims have been marked submitted. |

Claims prepared with **+ New Claim** exist only until the page is refreshed. Nothing is ever written back to the CSV files.

---

## 7. Known limits and assumptions

- **D99 is unpredictable.** It accounts for most of the misses.
- **Inferred rules.** The Summit whole-hour term and the code-reporting order come from patterns in the history, not from any manual.
- **Tuned on the data it is scored on.** The accuracy figures are optimistic until tested on claims the rules have not seen.
- **Only documented modifier and daily-max rules.** Modifier rules exist only for Harbor T1019, and daily-max only for Harbor. Other payers have none documented, though both can be switched on from the Rules page.
- **Date moves do not re-check units.** Moving a date of service to a different visit can change how many units that visit supports. The toast reports the new prediction after applying.
- **Unsubmitted claims are assumed to be submitted in order** of their `submitted_date` when computing authorization balances and daily totals.
- **Login is a demo.** Picking an agency filters the list. There is no password.
