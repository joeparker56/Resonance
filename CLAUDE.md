---
noteId: "7e603a60e9a411f188f969fc0f1460d0"
tags: []

---

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A take-home exercise for Resonance (the "Exchange4Health" case study). It predicts whether a home-care claim will be accepted or rejected by a payer **before** submission, names the denial code (D01–D10), and uses Claude Haiku to suggest a one-click fix. All data is synthetic: 3 payers (PY01 Northwind, PY02 Harbor, PY03 Summit), 12 agencies, June–Nov 2026.

`HOW_IT_WORKS.md` is the detailed write-up of the rules and the API call (keep it in step when rules change). `README.md` is the author's system-design overview with screenshots. Neither is a command reference.

## Commands

No build step, tests or linter. Everything except `server.py` is standard library only (the global Anaconda Python has a broken numpy/pandas combination, so don't reach for pandas).

```
.venv\Scripts\python server.py          # dashboard + AI fixes at http://localhost:8000 (needs ANTHROPIC_API_KEY in the environment)
python predict_rules.py .               # Python rules engine: writes predictions.csv and predictions_history.csv, prints a scorecard
python build_index.py                   # re-embed the CSV/markdown data into index.html (run after editing any CSV or rule-book .md)
```

`index.html` also opens straight from disk; only suggested fixes need `server.py`. Env overrides for the server: `PORT`, `CLAIM_FIX_MODEL` (default `claude-haiku-5-5`). The venv is created with `python -m venv .venv` then `pip install -r requirements.txt`.

**Sanity check after any rules change:** `predict_rules.py` with the manual defaults should report 3,765/3,830 paid-or-denied and 3,751/3,830 exact on history, and write 377 paid + 169 denied to `predictions.csv`. The dashboard's default rules must reproduce `predictions.csv` exactly (546/546). The remaining misses are D99 denials, which are unpredictable noise (~2.2% of claims, no pattern in any feature).

## Architecture

Two layers, deliberately separate: a **deterministic rules engine** makes every accept/reject call, and the **LLM only proposes a fix** as structured data that the app validates and the user confirms. Never let the model decide the prediction.

### The rules engine exists twice

- `predict_rules.py`: fixed rules in Python, source of `predictions.csv`.
- `index.html`: the same logic in JS, but **config-driven**: `DEFAULT_RULES` holds per-payer settings and `RULES` is the editable copy (Rules page, saved to `localStorage`). `checks(c, ctx)` reads `RULES`; `evaluate()` sorts failures by `RULES.precedence[payer]` and the first one is the single reported code.

They are kept in sync by hand. Change one, change the other, then run the sanity check above.

Behaviour that spans several functions:
- `runAll()` processes **all claims in submission order** (`submitted`, then claim id) carrying running state: units paid per authorization (D02), units paid per payer/patient/date (D06), and earlier new claims per payer/patient/service/date (D05). **History claims feed the state with their real outcome; unsubmitted/new claims feed it with their predicted outcome.** Anything that changes a claim must call `runAll()` afterwards.
- EVV matches a claim on patient + service + **clock-in date**; same-day visits are summed; only `verified` visits count. Payer rounding differs (Northwind 8-minute rule, others whole 15-minute blocks).
- Two things are **inferred from the history, not in any manual**: Summit's whole-hour contract rule (D10, units multiple of 4, DOS from 2026-07-01) and the per-payer order in which a payer reports one code when several rules fail. Both are labelled as such in the code and UI.
- D99 is never predicted.

### index.html is a single-file SPA

~1,100 lines, but two lines are huge: `const DATA = {...};` (claims, authorizations, EVV, denial codes) and `const DOCS = {...};` (rule-book text). They are generated, so don't hand-edit them and avoid printing them: use `Grep` with `-n` and `cut -c1-200`, or `Read` with `offset`/`limit`. `build_index.py` replaces exactly those two lines and leaves the code alone.

Hash routes: `#/login`, `#/` (claims), `#/rules`, `#/books` and `#/rulebooks/<doc>/<urlencoded text>`. The rule-book link `q` must be a substring that appears in the target section's text in `DOCS` (`SRC` maps every rule to `{doc, q, label}`), so renaming a heading in `payers/*.md` can break a link.

Browser state lives in `localStorage` only: `cc_agency`, `cc_rules`, `cc_fixes`, `submitted`. Nothing is ever written back to the CSVs. Claims created with "+ New Claim" are not persisted.

### Suggested-fix flow (index.html + server.py)

1. Selecting an unsubmitted, predicted-rejected claim calls `loadFix()` → `fixContext()` builds the evidence (claim, failed rules, active payer rules, rule-book excerpts, EVV, **unclaimed** nearby EVV, authorizations with remaining units, related claims, codes). Results are cached by the context JSON.
2. `POST /api/suggest-fix` → `server.py` calls the Messages API (`claude-haiku-5-5`, `output_config` JSON schema with all fields required, `effort: low`) and returns the proposal.
3. `validateFix()` checks the proposal against real data before any button appears (e.g. units can only go down, an authorization must exist and match patient/service/payer/date, a new date needs a verified unclaimed visit). The confirm box shows changes computed by the app, not the model's wording.
4. `recordFix()` / `applyFixCore()` mutate the claim in place, push a snapshot for undo, and persist the instruction (not the claim) to `cc_fixes`. On load, saved fixes are replayed over the pristine embedded data. Manual "Delete claim" and the AI delete share this path.

Constraints when touching the API call: Haiku 5.5 rejects non-default sampling parameters (don't send `temperature`), and the structured-output schema is used instead of forced `tool_choice`. The key stays in the server's environment; `server.py` binds to 127.0.0.1, checks the `Host` header, and sends no CORS headers on purpose, so don't add them.

## Data quirks (handled in `predict_rules.py` and `build_index.py`)

Dates are mixed `YYYY-MM-DD` and `MM/DD/YYYY` (US order); payer names appear as ids and several spellings; service codes can have trailing spaces; modifiers vary in case; 32 history rows are exact duplicates and are dropped. Cleaned totals: 3,830 history + 546 to-check = 4,376 claims.
