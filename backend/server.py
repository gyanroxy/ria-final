"""Dashboard server: serves the UI in ../frontend and the API it uses, and runs
the dialer that places queued calls through Vobiz.

Set DASHBOARD_PASSWORD in backend/.env. Without it the dashboard only opens
from this machine (http://localhost:5001).
"""
import asyncio
import hashlib
import hmac
import logging
import os
import threading
import time
import uuid

from dotenv import load_dotenv
from flask import Flask, abort, jsonify, request, send_from_directory, session

load_dotenv()

from db_driver import DatabaseDriver  # noqa: E402
from dialer import Dialer, parse_invoice  # noqa: E402

logging.basicConfig(level=logging.INFO)

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frontend")
PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
MAX_IMPORT_ROWS = 5000

DB = DatabaseDriver()
dialer = Dialer(DB)

app = Flask(__name__, static_folder=None)
app.secret_key = os.getenv("FLASK_SECRET_KEY") or hashlib.sha256(
    f"ria-dashboard:{os.getenv('LIVEKIT_API_SECRET', '')}:{PASSWORD}".encode()
).hexdigest()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict")


def is_local_request() -> bool:
    # A reverse proxy on this machine would look local; it adds X-Forwarded-For
    return request.remote_addr in ("127.0.0.1", "::1") and "X-Forwarded-For" not in request.headers


def signed_in() -> bool:
    return session.get("ok") is True if PASSWORD else is_local_request()


@app.before_request
def require_login():
    if request.path.startswith("/api/") and request.path not in ("/api/session", "/api/login"):
        if not signed_in():
            return jsonify({"error": "Sign in first"}), 401


@app.get("/")
def index():
    return send_from_directory(FRONTEND_DIR, "index.html")


@app.get("/<path:path>")
def static_files(path: str):
    if path.startswith("api/"):
        abort(404)
    return send_from_directory(FRONTEND_DIR, path)


@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": "ria-invoice-agent"})


@app.get("/api/session")
def get_session():
    return jsonify({"password_required": bool(PASSWORD), "signed_in": signed_in(),
                    "local_only": not PASSWORD and not is_local_request()})


@app.post("/api/login")
def login():
    if not PASSWORD:
        return jsonify({"error": "Set DASHBOARD_PASSWORD in backend/.env"}), 400
    given = str((request.get_json(silent=True) or {}).get("password", ""))
    if not hmac.compare_digest(given.encode(), PASSWORD.encode()):
        time.sleep(1)  # slows down password guessing
        return jsonify({"error": "Wrong password"}), 401
    session["ok"] = True
    return jsonify({"signed_in": True})


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify({"signed_in": False})


@app.get("/api/calls")
def list_calls():
    return jsonify({"calls": DB.list_calls(), "dialer": dialer.status()})


@app.post("/api/calls")
def queue_calls():
    """Queues one call per valid row: name, phone, business, invoice_no, amount, due_date, language."""
    body = request.get_json(silent=True) or {}
    rows = body.get("rows")
    if not isinstance(rows, list) or not rows:
        return jsonify({"error": "No rows to call"}), 400
    if len(rows) > MAX_IMPORT_ROWS:
        return jsonify({"error": f"At most {MAX_IMPORT_ROWS} rows per import"}), 400
    company = str(body.get("company") or os.getenv("COLLECTION_COMPANY", "")).strip()[:120]
    if not company:
        return jsonify({"error": "Set COLLECTION_COMPANY in backend/.env"}), 400

    queued, skipped = 0, []
    for line, row in enumerate(rows, start=1):
        invoice, problem = parse_invoice(row if isinstance(row, dict) else {})
        if invoice and DB.has_opted_out(invoice["phone"]):
            problem = "customer opted out of calls earlier"
        elif invoice and DB.called_recently(invoice["phone"], invoice["invoice_no"]):
            problem = "already queued or discussed in the last 20 hours"
        if problem or not invoice:
            skipped.append({"row": line, "reason": problem})
            continue
        DB.save_collection_call(uuid.uuid4().hex, company=company, status="queued", **invoice)
        queued += 1
    return jsonify({"queued": queued, "skipped": skipped})


@app.post("/api/calls/<call_id>/retry")
def retry_call(call_id: str):
    call = DB.get_collection_call(call_id)
    if not call:
        return jsonify({"error": "No such call"}), 404
    if call["status"] in ("queued", "dialing", "live"):
        return jsonify({"error": "This call is still in progress"}), 409
    if DB.has_opted_out(call["phone"]):
        return jsonify({"error": "This customer opted out of calls"}), 409
    fields = ("customer_name", "phone", "business", "invoice_no", "amount", "due_date", "company", "language")
    DB.save_collection_call(uuid.uuid4().hex, status="queued", **{f: call[f] for f in fields})
    return jsonify({"queued": 1})


@app.post("/api/calls/<call_id>/cancel")
def cancel_call(call_id: str):
    if not DB.cancel_call(call_id):
        return jsonify({"error": "Only queued calls can be cancelled"}), 409
    return jsonify({"cancelled": True})


@app.post("/api/dialer")
def set_dialer():
    dialer.paused = bool((request.get_json(silent=True) or {}).get("paused"))
    return jsonify(dialer.status())


def voice_stack() -> dict:
    """What hears, thinks and speaks on calls (mirrors agent.py's AGENT_MODE)."""
    mode = os.getenv("AGENT_MODE", "realtime-mishka")
    if mode == "realtime-mishka":
        model = os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime")
        return {
            "llm": f"{model} (hears the call, writes replies)",
            "stt": f"built into {model}",
            "tts": f"Smallest {os.getenv('SMALLEST_TTS_MODEL', 'lightning_v3.1_pro')} · {os.getenv('SMALLEST_TTS_VOICE_ID', 'mishka')}",
        }
    if mode == "realtime":
        model = os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime")
        return {
            "llm": f"{model} (speech to speech)",
            "stt": f"built into {model}",
            "tts": f"{model} · {os.getenv('OPENAI_REALTIME_VOICE', 'shimmer')}",
        }
    return {
        "llm": f"Smallest {os.getenv('SMALLEST_LLM_MODEL', 'electron')}"
               if os.getenv("LLM_PROVIDER", "openai") == "smallest"
               else os.getenv("OPENAI_LLM_MODEL", "gpt-4.1-mini"),
        "stt": "Smallest Pulse" if os.getenv("STT_PROVIDER", "smallest") == "smallest"
               else os.getenv("OPENAI_STT_MODEL", "gpt-4o-mini-transcribe"),
        "tts": f"{os.getenv('SMALLEST_TTS_MODEL', 'lightning_v3.1')} · {os.getenv('SMALLEST_TTS_VOICE_ID', 'mishka')}",
    }


@app.get("/api/settings")
def settings():
    number = os.getenv("VOBIZ_PHONE_NUMBER", "")
    return jsonify({
        "company": os.getenv("COLLECTION_COMPANY", ""),
        "vobiz_number": number,
        "vobiz_domain": os.getenv("VOBIZ_SIP_DOMAIN", ""),
        "opening_language": os.getenv("COLLECTION_LANGUAGE", "te"),
        **voice_stack(),
        "dialer": dialer.status(),
    })


def start_dialer() -> None:
    threading.Thread(target=lambda: asyncio.run(dialer.run()), name="dialer", daemon=True).start()


if __name__ == "__main__":
    start_dialer()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5001")), debug=False)
