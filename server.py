"""Local server for the Claim Check dashboard.

Serves index.html and exposes ONE API route that asks Claude Haiku for a suggested fix:

    POST /api/suggest-fix     body: {"context": {...claim, EVV, authorizations, rules...}}
    GET  /api/status          -> {"configured": true|false, "model": "..."}

Why a server at all: the Anthropic API key must never be placed in a web page. The key stays
here, read from the ANTHROPIC_API_KEY environment variable (or an `ant auth login` profile).

Run it:
    .venv\\Scripts\\python server.py          (then open http://localhost:8000)

The model only PROPOSES a fix as structured data. It never changes anything: the dashboard
validates the proposal against the real data, shows it, and asks the user to confirm.
"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import anthropic

ROOT = Path(__file__).resolve().parent
MODEL = os.environ.get("CLAIM_FIX_MODEL", "claude-haiku-5-5")
PORT = int(os.environ.get("PORT", "8000"))
MAX_BODY = 256 * 1024

ACTIONS = ["delete_claim", "change_units", "change_auth", "change_modifier",
           "change_date_of_service", "hold_claim", "no_fix"]

# Structured output: every field is required (the model fills unused ones with 0 / "").
SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ACTIONS},
        "summary": {"type": "string"},
        "reasoning": {"type": "string"},
        "button_label": {"type": "string"},
        "units": {"type": "integer"},
        "auth_id": {"type": "string"},
        "modifier": {"type": "string"},
        "date_of_service": {"type": "string"},
        "note": {"type": "string"},
    },
    "required": ["action", "summary", "reasoning", "button_label", "units", "auth_id",
                 "modifier", "date_of_service", "note"],
    "additionalProperties": False,
}

SYSTEM = """You are a billing assistant for a home-care agency. A claim has not been submitted yet, and \
a rules engine predicts the payer will reject it. You are given the claim, the rules that were broken, \
the relevant payer rule-book text, the EVV (electronic visit verification) records, the patient's \
authorizations, related claims, and billing codes. Propose exactly ONE fix, chosen from these actions:

- delete_claim: the claim duplicates an earlier claim for the same patient, service, date of service and payer \
(D05). Deleting is only right when the other claim is the one that should stand.
- change_units: set the billed units (field "units"). Use it when units exceed what the verified EVV time supports \
(D09), the authorization's remaining units (D02), or the daily maximum (D06). Pick the largest number that every \
limit in the context allows, never more than the claim already bills, and never less than 1.
- change_auth: set the authorization number (field "auth_id"). Use it when the number is missing (D07) or does not \
fit (D08). Only choose an authorization listed under authorizations_for_patient that matches the claim's service and \
payer and covers the date of service. Never invent an ID.
- change_modifier: set the modifier (field "modifier", for example "U2") when the payer's rule requires a different one (D04).
- change_date_of_service: move the claim to the date of a real, unclaimed, verified EVV visit (field "date_of_service", \
YYYY-MM-DD) when the claim has no verified visit (D01) and unclaimed_evv_nearby lists a verified visit for the same \
patient and service that no claim covers. Only pick a date from that list. If it is not clearly the same visit, prefer hold_claim.
- hold_claim: nothing can be fixed inside the app and a person must act first, for example an EVV record is pending \
approval or was rejected, or the claim is already past the filing deadline. Put what the person should do in "note".
- no_fix: no legitimate fix exists.

Rules you must follow:
- Use only facts in the context. Do not invent IDs, dates, minutes or units.
- Never suggest anything that misrepresents care that was not delivered. If there is no EVV support, do not change units or dates to make it fit.
- If several rules are broken, fix the one that is reported first when a single action can clear it; otherwise choose the \
action that clears the most, or hold_claim.
- "summary" is one plain-English sentence under 110 characters saying what to do, with the specific values (for example \
"Reduce billed units from 9 to 6 to match the verified visit."). "reasoning" is one or two short sentences with the evidence. \
"button_label" is 2 to 4 words naming the action (for example "Reduce units to 6").
- Fill every field. Use 0 for units, "" for the text fields that your chosen action does not use."""


def build_client():
    return anthropic.Anthropic(max_retries=2, timeout=60.0)


CLIENT = None


def credentials_present():
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
                or os.environ.get("ANTHROPIC_PROFILE") or os.environ.get("ANTHROPIC_FEDERATION_RULE_ID"))


def suggest_fix(context):
    global CLIENT
    if CLIENT is None:
        CLIENT = build_client()
    resp = CLIENT.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=SYSTEM,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": "Claim context (JSON):\n" + json.dumps(context, indent=1)}],
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("The model declined to answer this request.")
    text = next((b.text for b in resp.content if b.type == "text"), "")
    if resp.stop_reason == "max_tokens" or not text:
        raise RuntimeError("The model's answer was cut off. Try again.")
    out = json.loads(text)
    out["_usage"] = {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens}
    out["_model"] = resp.model
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "ClaimCheck/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _host_ok(self):
        # Only answer requests addressed to this machine (guards against DNS rebinding).
        host = (self.headers.get("Host") or "").lower()
        return host in (f"localhost:{self.server.server_port}", f"127.0.0.1:{self.server.server_port}")

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "forbidden host"})
        if self.path in ("/", "/index.html"):
            return self._send(200, (ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
        if self.path == "/api/status":
            return self._send(200, {"configured": credentials_present(), "model": MODEL})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._host_ok():
            return self._send(403, {"error": "forbidden host"})
        if self.path != "/api/suggest-fix":
            return self._send(404, {"error": "not found"})
        if not credentials_present():
            return self._send(503, {"error": "No Anthropic credentials. Set ANTHROPIC_API_KEY before starting server.py."})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY:
                return self._send(413, {"error": "request body missing or too large"})
            context = json.loads(self.rfile.read(length)).get("context")
            if not isinstance(context, dict):
                return self._send(400, {"error": "body must be {\"context\": {...}}"})
            return self._send(200, suggest_fix(context))
        except anthropic.AuthenticationError:
            return self._send(401, {"error": "The Anthropic API key was rejected."})
        except anthropic.RateLimitError:
            return self._send(429, {"error": "Rate limited by the Anthropic API. Try again shortly."})
        except anthropic.APIConnectionError:
            return self._send(502, {"error": "Could not reach the Anthropic API."})
        except anthropic.APIStatusError as e:
            return self._send(502, {"error": f"Anthropic API error ({e.status_code}): {e.message}"})
        except (json.JSONDecodeError, RuntimeError) as e:
            return self._send(502, {"error": str(e)})
        except Exception as e:  # never leak a traceback to the page
            sys.stderr.write(f"unexpected error: {e!r}\n")
            return self._send(500, {"error": "Unexpected server error."})


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Claim Check running at http://localhost:{PORT}  (model: {MODEL})")
    if not credentials_present():
        print("Warning: ANTHROPIC_API_KEY is not set. The dashboard works, but suggested fixes are unavailable.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
