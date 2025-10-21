import os
from datetime import datetime
from flask import Flask, request, Response
from twilio.twiml.voice_response import VoiceResponse, Gather
import pytz
import json
import requests

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


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5050"))
    app.run(host="0.0.0.0", port=port)
