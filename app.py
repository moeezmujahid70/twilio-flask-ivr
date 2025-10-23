import time
from botocore.config import Config as BotoConfig
import boto3
import os
from datetime import datetime
from flask import Flask, request, Response, jsonify, abort
from twilio.twiml.voice_response import VoiceResponse, Gather
import pytz
import json
import requests
import re
from twilio.rest import Client

app = Flask(__name__)


# Current audio URLs (start from env, can be changed at runtime)
AUDIO = {
    "menu": os.getenv("MENU_MP3_URL", "https://example.com/menu.mp3"),
    "opt1": os.getenv("OPT1_MP3_URL",  "https://example.com/opt1.mp3"),
    "opt3": os.getenv("OPT3_MP3_URL",  "https://example.com/opt3.mp3"),
}

S3_BUCKET = os.getenv("S3_BUCKET", "")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

s3_client = boto3.client(
    "s3",
    region_name=AWS_REGION,
    config=BotoConfig(s3={"addressing_style": "virtual"})
)


def twilio_client():
    if not (TWILIO_ACCOUNT_SID.startswith("AC") and len(TWILIO_AUTH_TOKEN) > 10):
        raise RuntimeError("Twilio credentials not configured")
    return Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


def require_admin():
    token = request.headers.get("x-admin-token") or request.args.get("token")
    if not ADMIN_TOKEN or token != ADMIN_TOKEN:
        abort(403)


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
    g.play(AUDIO["menu"])
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
        vr.play(AUDIO["opt1"])
    elif digits == "3":
        vr.play(AUDIO["opt3"])
    else:
        vr.say("Invalid key press. Goodbye.")

    return Response(str(vr), mimetype="text/xml")


@app.route("/twilio/from-numbers", methods=["GET"])
def twilio_from_numbers():
    """
    Returns numbers you own (capable of being 'from' numbers).
    JSON: [{sid, phone_number, friendly_name, capabilities:{voice,sms,mms}}]
    """
    client = twilio_client()
    items = []
    for num in client.incoming_phone_numbers.list(limit=100):
        caps = getattr(num, "capabilities", {}) or {}
        items.append({
            "sid": num.sid,
            "phone_number": num.phone_number,
            "friendly_name": num.friendly_name or "",
            "capabilities": {
                "voice": bool(caps.get("voice")),
                "sms":   bool(caps.get("sms")),
                "mms":   bool(caps.get("mms")),
            }
        })
    return {"ok": True, "numbers": items}


@app.route("/admin", methods=["GET"])
def admin_page():
    require_admin()
    # make absolute URLs for quick testing links
    base = base_url()
    menu = AUDIO["menu"]
    opt1 = AUDIO["opt1"]
    opt3 = AUDIO["opt3"]
    def absu(u): return u if u.startswith("http") else f"{base}{u}"
    return f"""
<!doctype html><meta charset="utf-8">
<title>IVR Admin</title>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>
  :root {{ --fg:#111; --muted:#6b7280; --bg:#fff; --card:#f9fafb; --btn:#111827; }}
  * {{ box-sizing:border-box; }}
  body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; color:var(--fg); background:var(--bg); margin:24px; }}
  .wrap {{ max-width: 980px; margin: 0 auto; display: grid; gap: 24px; }}
  .card {{ background:var(--card); border:1px solid #e5e7eb; border-radius:14px; padding:20px; box-shadow:0 4px 14px rgba(0,0,0,.05); }}
  h2 {{ margin:0 0 12px; }}
  h3 {{ margin:18px 0 8px; }}
  label {{ display:block; font-weight:600; margin:10px 0 6px; }}
  input, textarea {{ width:100%; padding:10px 12px; border:1px solid #d1d5db; border-radius:10px; font-size:14px; }}
  textarea {{ min-height:110px; }}
  .row {{ display:flex; gap:10px; align-items:center; margin:10px 0; flex-wrap:wrap; }}
  button {{ padding:10px 14px; border-radius:10px; border:0; background:var(--btn); color:#fff; font-weight:600; cursor:pointer; }}
  .muted {{ color:var(--muted); font-size:12px; }}
  .mono {{ font:12px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; word-break: break-all; }}
  .kvs div {{ display:flex; gap:8px; align-items:center; margin:4px 0; }}
  audio {{ width: 100%; margin-top:6px; }}
  .grid2 {{ display:grid; gap:16px; grid-template-columns: repeat(2, minmax(0,1fr)); }}
  .kvs a {{
  color: #2563eb;
  text-decoration: none;
  font-weight: 500;
}}
.kvs a:hover {{
  text-decoration: underline;
}}

.kvs {{
  display: flex;
  flex-direction: column;
  gap: 10px;
}}

.audio-row {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  flex-wrap: wrap;
}}

.audio-row .label {{
  flex: 0 0 150px; /* left side width */
  font-size: 14px;
}}

.audio-row audio {{
  flex: 1;
  min-width: 200px;
  max-width: 450px;
}}

.kvs a {{
  color: #2563eb;
  text-decoration: none;
  font-weight: 500;
}}

.upload-row {{
  margin-bottom: 18px;
}}

.file-wrap {{
  display: flex;
  align-items: center;
  position: relative;
}}

.file-wrap input[type="file"] {{
  flex: 1;
  padding: 10px 14px;
  border: 1px solid #d1d5db;
  border-radius: 10px;
  font-size: 14px;
  background: #fff;
  color: #111;
}}

.file-wrap button {{
  position: absolute;
  right: 8px;
  top: 50%;
  transform: translateY(-50%);
  padding: 8px 12px;
  border: none;
  background: #111827;
  color: white;
  font-weight: 600;
  border-radius: 8px;
  cursor: pointer;
  font-size: 13px;
}}

.file-wrap button:hover {{
  background: #1f2937;
}}

  @media (max-width:880px) {{ .grid2 {{ grid-template-columns: 1fr; }} }}
</style>



<div class="wrap">
  <!-- DIALER -->
  <div class="card">
    <h2>Outbound Dialer</h2>
    <p class="muted">Enter a Twilio <b>From</b> number and one or more <b>To</b> numbers (E.164, one per line or comma-separated).</p>
    <label>From (Twilio number)</label>

<div class="row">
  <select id="from-select"></select>
  <button type="button" id="refresh-numbers">Refresh</button>
</div>

<input id="from" style="display:none" />
    <label>To numbers</label>
    <textarea id="to" placeholder="+4917671079494
+16466681045"></textarea>
    <div class="row">
      <button id="btn-dial">Make Calls</button>
      <a href="{base}/voice" target="_blank" class="muted">Open /voice (TwiML) »</a>
    </div>
    <h3>Response</h3>
    <pre id="dial-out" class="mono">—</pre>
  </div>

  <!-- AUDIO -->
  <div class="card">
    <h2>Upload & Set IVR Audio</h2>

    <div class="grid2">
      <div>
        <h3>Current</h3>

        <div class="kvs">
  <div class="audio-row">
    <div class="label"><strong>Menu:</strong> <a href="{absu(menu)}" target="_blank">View file</a></div>
    <audio controls src="{absu(menu)}"></audio>
  </div>
  <div class="audio-row">
    <div class="label"><strong>Option 1:</strong> <a href="{absu(opt1)}" target="_blank">View file</a></div>
    <audio controls src="{absu(opt1)}"></audio>
  </div>
  <div class="audio-row">
    <div class="label"><strong>Option 3:</strong> <a href="{absu(opt3)}" target="_blank">View file</a></div>
    <audio controls src="{absu(opt3)}"></audio>
  </div>
</div>


       
      </div>

      <div>
        <h3>Upload new files</h3>

        <div class="upload-row">
  <label>Menu MP3</label>
  <div class="file-wrap">
    <input id="file-menu" type="file" accept="audio/mpeg,audio/mp3" />
    <button type="button" onclick="handleUpload('menu','file-menu')">Upload & Set</button>
  </div>
</div>

<div class="upload-row">
  <label>Option 1 MP3</label>
  <div class="file-wrap">
    <input id="file-opt1" type="file" accept="audio/mpeg,audio/mp3" />
    <button type="button" onclick="handleUpload('opt1','file-opt1')">Upload & Set</button>
  </div>
</div>

<div class="upload-row">
  <label>Option 3 MP3</label>
  <div class="file-wrap">
    <input id="file-opt3" type="file" accept="audio/mpeg,audio/mp3" />
    <button type="button" onclick="handleUpload('opt3','file-opt3')">Upload & Set</button>
  </div>
</div>


       
    </div>

    <h3>Status</h3>
    <pre id="up-out" class="mono">Ready.</pre>
  </div>
</div>

<script>
const ADMIN_TOKEN = "{ADMIN_TOKEN}"; // server-injected
function slug(s){{return (s||'').toLowerCase().replace(/[^a-z0-9]+/g,'-').replace(/(^-|-$)/g,'');}}

async function callDialer() {{
  const from = document.getElementById('from').value.trim();
  const toRaw = document.getElementById('to').value.trim();
  const out = document.getElementById('dial-out');
  out.textContent = 'Creating calls…';
  try {{
    const r = await fetch('/dial', {{
      method:'POST',
      headers: {{ 'Content-Type':'application/json' }},
      body: JSON.stringify({{ from, to: toRaw }})
    }});
    const data = await r.json().catch(async () => ({{ ok:false, error: await r.text() }}));
    out.textContent = JSON.stringify(data, null, 2);
  }} catch (e) {{
    out.textContent = 'Error: ' + e.message;
  }}
}}
document.getElementById('btn-dial').addEventListener('click', callDialer);

async function presign(key, type) {{
  const r = await fetch('/sign-upload?key='+encodeURIComponent(key)+'&type='+encodeURIComponent(type), {{
    headers: {{ 'x-admin-token': ADMIN_TOKEN }}
  }});
  if(!r.ok) throw new Error('sign failed ('+r.status+')');
  return r.json();
}}
async function s3Upload(url, fields, file) {{
  const fd = new FormData();
  Object.entries(fields).forEach(([k,v]) => fd.append(k,v));
  fd.append('file', file);
  const resp = await fetch(url, {{ method:'POST', body: fd }});
  if(resp.status !== 204) throw new Error('s3 upload failed ('+resp.status+')');
}}
async function setAudio(kind, url) {{
  const r = await fetch('/set-audio', {{
    method:'POST',
    headers: {{ 'Content-Type':'application/json', 'x-admin-token': ADMIN_TOKEN }},
    body: JSON.stringify({{ kind, url }})
  }});
  const data = await r.json().catch(async () => ({{ ok:false, error: await r.text() }}));
  if(!r.ok || !data.ok) throw new Error('set-audio failed: ' + (data.error || r.status));
  return data;
}}
async function handleUpload(kind, inputId){{
  const out = document.getElementById('up-out');
  try {{
    const el = document.getElementById(inputId);
    const file = el.files[0];
    if(!file) throw new Error('Pick a file first');
    if(!file.type || !file.type.includes('audio')) throw new Error('Select an MP3');

    const key = `${{kind}}/${{Date.now()}}-${{slug(file.name)}}`;
    out.textContent = 'Signing…';
    const p = await presign(key, file.type || 'audio/mpeg');

    out.textContent = 'Uploading to S3…';
    await s3Upload(p.url, p.fields, file);

    out.textContent = 'Updating live URL…';
    const res = await setAudio(kind, p.publicUrl);

    out.textContent = 'Done. New URL: ' + res.url + '\\nNext calls will use it.';
  }} catch(e) {{
    out.textContent = 'Error: ' + e.message;
  }}
}}

async function loadFromNumbers() {{
  const sel = document.getElementById('from-select');
  sel.innerHTML = '<option>Loading…</option>';
  try {{
    let r = await fetch('/twilio/from-numbers');
    let data = await r.json();
    let options = [];
    if (r.ok && data.ok && data.numbers && data.numbers.length) {{
      options = data.numbers.map(n => ({{
        value: n.phone_number,
        label: `${{n.phone_number}} ${{n.friendly_name ? '— ' + n.friendly_name : ''}}`
      }}));
    }} else {{
      r = await fetch('/twilio/caller-ids');
      data = await r.json();
      if (r.ok && data.ok && data.caller_ids && data.caller_ids.length) {{
        options = data.caller_ids.map(n => ({{
          value: n.phone_number,
          label: `${{n.phone_number}} ${{n.friendly_name ? '— ' + n.friendly_name : ''}}`
        }}));
      }}
    }}
    if (!options.length) {{
      sel.innerHTML = '<option>No numbers found</option>';
      return;
    }}
    sel.innerHTML = options.map(o => `<option value="${{o.value}}">${{o.label}}</option>`).join('');
  }} catch(e) {{
    sel.innerHTML = '<option>Error loading numbers</option>';
  }}
}}

async function callDialer() {{
  const from = document.getElementById('from-select').value || '';
  const toRaw = document.getElementById('to').value.trim();
  const out = document.getElementById('dial-out');
  out.textContent = 'Creating calls…';
  try {{
    const r = await fetch('/dial', {{
      method:'POST',
      headers: {{ 'Content-Type':'application/json' }},
      body: JSON.stringify({{ from, to: toRaw }})
    }});
    const data = await r.json().catch(async () => ({{ ok:false, error: await r.text() }}));
    out.textContent = JSON.stringify(data, null, 2);
  }} catch(e) {{
    out.textContent = 'Error: ' + e.message;
  }}
}}

document.getElementById('btn-dial').addEventListener('click', callDialer);
document.getElementById('refresh-numbers').addEventListener('click', loadFromNumbers);
window.addEventListener('load', loadFromNumbers);


</script>
"""


@app.route("/sign-upload", methods=["GET"])
def sign_upload():
    require_admin()
    if not S3_BUCKET:
        return {"ok": False, "error": "S3_BUCKET not configured"}, 500

    key = request.args.get("key") or f"uploads/{int(time.time())}.mp3"
    content_type = request.args.get("type") or "audio/mpeg"

    post = s3_client.generate_presigned_post(
        Bucket=S3_BUCKET,
        Key=key,
        Fields={"Content-Type": content_type},
        Conditions=[{"Content-Type": content_type}],
        ExpiresIn=600
    )

    public_url = f"https://{S3_BUCKET}.s3.{AWS_REGION}.amazonaws.com/{key}"
    return jsonify({"ok": True, "url": post["url"], "fields": post["fields"], "publicUrl": public_url})


@app.route("/set-audio", methods=["POST"])
def set_audio():
    require_admin()
    data = request.get_json(silent=True) or {}
    kind = data.get("kind")
    url = data.get("url")
    if kind not in ("menu", "opt1", "opt3"):
        return {"ok": False, "error": "kind must be menu|opt1|opt3"}, 400
    if not (url and url.startswith("https://")):
        return {"ok": False, "error": "url must be https"}, 400

    AUDIO[kind] = url
    return {"ok": True, "kind": kind, "url": AUDIO[kind]}


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5050"))
    app.run(host="0.0.0.0", port=port)
