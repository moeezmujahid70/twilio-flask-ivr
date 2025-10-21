import os
from datetime import datetime
from flask import Flask, request, Response
from twilio.twiml.voice_response import VoiceResponse, Gather
import pytz
import json
import requests
import re
from twilio.rest import Client

app = Flask(__name__)

# ---- Config (set these in Railway → Variables) ----
# Apps Script webhook (optional)
GSCRIPT_LOG_URL = os.getenv("GSCRIPT_LOG_URL")
MENU_MP3_URL = os.getenv(
    "MENU_MP3_URL", "https://blue-lemur-4041.twil.io/assets/kolel%20robo%2C%20mon%2010%2020....mp3.mp3")
OPT1_MP3_URL = os.getenv(
    "OPT1_MP3_URL",  "https://your-bucket.s3.us-east-1.amazonaws.com/option1.mp3")
OPT3_MP3_URL = os.getenv(
    "OPT3_MP3_URL",  "https://your-bucket.s3.us-east-1.amazonaws.com/option3.mp3")
TZ = os.getenv("LOG_TZ", "America/New_York")  # US timestamp


TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
# optional default FROM (you can still override from the form)
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER", "")

E164 = re.compile(r"^\+\d{6,15}$")  # simple E.164 check


# --- small helper ---
def is_e164(s: str) -> bool:
    return bool(s and E164.match(s.strip()))


def base_url():
    """Compute absolute base URL from incoming request."""
    return request.url_root.rstrip("/")


@app.route("/health")
def health():
    return "ok", 200


@app.route("/voice", methods=["GET", "POST"])
def voice():
    """Main IVR entry — plays the menu and waits for 1 DTMF key."""
    vr = VoiceResponse()

    # Create the DTMF listener
    g = Gather(
        num_digits=1,
        input="dtmf",
        timeout=10,
        action=f"{base_url()}/gather",
        method="POST",
        actionOnEmptyResult=True
    )

    # Play the main menu audio inside the gather (interruptible)
    g.play(MENU_MP3_URL)
    vr.append(g)

    # If no key pressed, Twilio continues here after timeout
    vr.say("We did not receive any input. Goodbye.")

    return Response(str(vr), mimetype="text/xml")


# --- Add this new endpoint: POST /dial ---
@app.route("/dial", methods=["POST"])
def dial():
    """
    JSON body:
      { "from": "+1XXXXXXXXXX", "to": ["+49...", "+1..."] }
    Returns: { "ok": true, "calls": [{"to":"..","sid":".."}] }
    """
    if not (TWILIO_ACCOUNT_SID.startswith("AC") and len(TWILIO_AUTH_TOKEN) >= 10):
        return {"ok": False, "error": "Twilio credentials not set on server"}, 500

    try:
        data = request.get_json(force=True, silent=False) or {}
    except Exception:
        return {"ok": False, "error": "Invalid JSON"}, 400

    from_num = (data.get("from") or TWILIO_PHONE_NUMBER or "").strip()
    to_list = data.get("to") or []

    # Normalize: allow textarea string with newlines/commas or array
    if isinstance(to_list, str):
        raw = to_list.replace(",", "\n").splitlines()
        to_list = [x.strip() for x in raw if x.strip()]

    # Validate
    if not is_e164(from_num):
        return {"ok": False, "error": "Invalid FROM (use E.164, e.g. +14155550123)"}, 400
    cleaned = [n for n in to_list if is_e164(n)]
    if not cleaned:
        return {"ok": False, "error": "No valid TO numbers (use E.164)"},
        400

    # Place calls
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    results = []
    for n in cleaned:
        call = client.calls.create(
            to=n,
            from_=from_num,
            url=f"{base_url()}/voice"  # your IVR entry
        )
        results.append({"to": n, "sid": call.sid})

    return {"ok": True, "calls": results}, 200


@app.route("/gather", methods=["GET", "POST"])
def gather():
    """Handle keypress and play the relevant message."""
    vr = VoiceResponse()

    digits = request.values.get("Digits") or "NA"
    from_num = request.values.get("From") or ""
    to_num = request.values.get("To") or ""
    callsid = request.values.get("CallSid") or ""

    # 🇺🇸 Timestamp (Eastern Time)
    ts = datetime.now(pytz.timezone(TZ)).strftime("%Y-%m-%d %I:%M:%S %p")

    # Log to Google Sheet (optional)
    if GSCRIPT_LOG_URL:
        try:
            payload = {
                "from": from_num,
                "to": to_num,
                "callsid": callsid,
                "digits": digits,
                "timestamp": ts
            }
            requests.post(GSCRIPT_LOG_URL, data=json.dumps(payload), timeout=3)
        except Exception as e:
            app.logger.warning(f"Sheet log error: {e}")

    # IVR options
    if digits == "1":
        vr.play(OPT1_MP3_URL)
    elif digits == "3":
        vr.play(OPT3_MP3_URL)
    else:
        vr.say("Invalid key press. Goodbye.")

    return Response(str(vr), mimetype="text/xml")


@app.route("/", methods=["GET"])
def index():
    return """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>IVR Dialer</title>
  <style>
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 40px; color:#111; }
    .card { max-width: 720px; padding:24px; border:1px solid #e5e7eb; border-radius:14px; box-shadow:0 4px 14px rgba(0,0,0,.06); }
    label { display:block; font-weight:600; margin-top:14px; }
    input, textarea { width:100%; padding:12px; border:1px solid #d1d5db; border-radius:10px; font-size:15px; }
    textarea { min-height:120px; }
    .hint { color:#6b7280; font-size:12px; margin-top:6px }
    button { margin-top:16px; padding:12px 18px; border-radius:10px; border:0; background:#111827; color:#fff; font-weight:600; cursor:pointer; }
    pre { background:#0b1020; color:#e5e7eb; padding:12px; border-radius:10px; overflow:auto; }
  </style>
</head>
<body>
  <div class="card">
    <h2>Outbound Dialer</h2>
    <p class="hint">Enter a Twilio <b>From</b> number and the list of <b>To</b> numbers (E.164 format).</p>

    <label>From (Twilio number, E.164)</label>
    <input id="from" placeholder="+16469703520" />

    <label>To numbers (one per line or comma-separated)</label>
    <textarea id="to" placeholder="+4917671070000
+16466680000"></textarea>
    <div class="hint">Example format: +14155550123</div>

    <button id="dial">Make Calls</button>

    <h3>Response</h3>
    <pre id="out">—</pre>
  </div>

<script>
async function dial() {
  const from = document.getElementById('from').value.trim();
  const toRaw = document.getElementById('to').value.trim();
  const payload = { from, to: toRaw };

  const res = await fetch('/dial', {
    method: 'POST',
    headers: { 'Content-Type':'application/json' },
    body: JSON.stringify(payload)
  });
  const data = await res.json();
  document.getElementById('out').textContent = JSON.stringify(data, null, 2);
}

document.getElementById('dial').addEventListener('click', () => {
  document.getElementById('out').textContent = 'Calling…';
  dial().catch(err => {
    document.getElementById('out').textContent = 'Error: ' + err;
  });
});
</script>
</body>
</html>
    """


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5050"))
    app.run(host="0.0.0.0", port=port)
