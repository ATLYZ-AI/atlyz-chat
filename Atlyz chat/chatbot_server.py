# chatbot_server.py — Atlyz Chat Server (hardened)

import os
import json
import re
import uuid
import time
import smtplib
import csv
import threading
from datetime import datetime
from email.mime.text import MIMEText
from dotenv import load_dotenv
from flask import Flask, request, jsonify, render_template, send_from_directory, session as flask_session
from werkzeug.security import generate_password_hash, check_password_hash

import plans

load_dotenv()

LOGO_EXTS = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
             "gif": "image/gif", "svg": "image/svg+xml", "webp": "image/webp"}
MAX_LOGO_BYTES = 512 * 1024  # 512 KB

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
CLIENTS_DIR = os.path.join(BASE_DIR, "clients")

DEV_MODE = os.getenv("DEV_MODE", "false").lower() == "true"

app = Flask(__name__)
_secret = os.getenv("SECRET_KEY", "")
if not _secret:
    if not DEV_MODE:
        raise RuntimeError("SECRET_KEY must be set in production (DEV_MODE=false).")
    _secret = "atlyz-chat-dev-secret"
app.secret_key = _secret

# ── Tunables ────────────────────────────────────────────────────────────────────
MAX_MESSAGE_CHARS   = int(os.getenv("MAX_MESSAGE_CHARS", 2000))
MAX_KNOWLEDGE_CHARS = int(os.getenv("MAX_KNOWLEDGE_CHARS", 12000))
RATE_LIMIT_MAX      = int(os.getenv("RATE_LIMIT_MAX", 20))
RATE_LIMIT_WINDOW   = int(os.getenv("RATE_LIMIT_WINDOW", 60))
IP_RATE_LIMIT_MAX   = int(os.getenv("IP_RATE_LIMIT_MAX", 40))


@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Admin-Key"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


@app.before_request
def handle_options():
    if request.method == "OPTIONS":
        resp = app.make_default_options_response()
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Admin-Key"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        return resp


# ── In-memory stores (caches only — source of truth is on disk) ─────────────────
sessions        = {}  # session_id → session data
knowledge_cache = {}  # business_id → knowledge text
rate_limits     = {}  # session_id → [timestamps]
ip_rate_limits  = {}  # ip → [timestamps]
_disk_lock      = threading.Lock()


# ═══════════════════════════════════════════════════════════════════════════════
# SECURITY HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

_VALID_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def valid_business_id(business_id: str) -> bool:
    """Reject anything that could escape the clients directory."""
    return bool(business_id) and bool(_VALID_ID.match(business_id))


def client_dir(business_id: str):
    """Return the absolute clients/<id> path, or None if the id is unsafe."""
    if not valid_business_id(business_id):
        return None
    path = os.path.normpath(os.path.join(CLIENTS_DIR, business_id))
    # Defense in depth: ensure the resolved path stays inside CLIENTS_DIR.
    if os.path.commonpath([path, CLIENTS_DIR]) != CLIENTS_DIR:
        return None
    return path


def business_exists(business_id: str) -> bool:
    d = client_dir(business_id)
    return bool(d) and os.path.isdir(d)


def check_admin_key() -> bool:
    """Valid admin key, or dev mode with no key configured."""
    required = os.getenv("ATLYZ_ADMIN_KEY", "")
    if not required:
        return DEV_MODE  # in production, no key configured ⇒ deny
    provided = (
        request.headers.get("X-Admin-Key", "") or
        request.args.get("key", "") or
        (request.get_json(silent=True) or {}).get("admin_key", "")
    )
    return bool(provided) and provided == required


def is_owner_of(business_id: str) -> bool:
    """True if the request is an authenticated owner of this business, or a valid admin."""
    if check_admin_key():
        return True
    return business_id in flask_session.get("owner_of", [])


def client_ip() -> str:
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or "unknown"


def check_rate_limit(store: dict, key: str, limit: int) -> bool:
    now = time.time()
    stamps = [t for t in store.get(key, []) if now - t < RATE_LIMIT_WINDOW]
    if len(stamps) >= limit:
        store[key] = stamps
        return False
    stamps.append(now)
    store[key] = stamps
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# NOTIFICATIONS
# ═══════════════════════════════════════════════════════════════════════════════

def send_lead_email(owner_email: str, business_name: str, lead: dict):
    try:
        from_addr = os.getenv("EMAIL_FROM", "")
        password  = os.getenv("EMAIL_PASSWORD", "")
        if not from_addr or not password or not owner_email:
            return
        body = (
            f"New lead from your Atlyz chatbot!\n\n"
            f"Name:    {lead.get('name') or 'Not provided'}\n"
            f"Email:   {lead.get('email') or 'Not provided'}\n"
            f"Phone:   {lead.get('phone') or 'Not provided'}\n"
            f"Message: {lead.get('question') or 'Not provided'}\n\n"
            f"Business: {business_name}\n"
            f"Time:     {lead.get('timestamp', '')}\n"
        )
        msg = MIMEText(body)
        msg["Subject"] = f"[Atlyz] New lead — {lead.get('name') or lead.get('email') or 'Unknown'}"
        msg["From"]    = from_addr
        msg["To"]      = owner_email
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(from_addr, password)
            server.sendmail(from_addr, owner_email, msg.as_string())
        print(f"[LEAD] Email sent to {owner_email}")
    except Exception as e:
        print(f"[LEAD EMAIL ERROR] {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG / KNOWLEDGE LOADERS
# ═══════════════════════════════════════════════════════════════════════════════

def load_knowledge(business_id: str) -> str:
    if business_id in knowledge_cache:
        return knowledge_cache[business_id]

    base = client_dir(business_id)
    if not base:
        return ""
    config_dir = os.path.join(base, "config")

    for fname in ["knowledge.txt", "knowledge.pdf"]:
        path = os.path.join(config_dir, fname)
        if os.path.exists(path):
            if fname.endswith(".txt"):
                with open(path, encoding="utf-8") as f:
                    content = f.read().strip()
                knowledge_cache[business_id] = content
                return content
            elif fname.endswith(".pdf"):
                try:
                    import pdfplumber
                    with pdfplumber.open(path) as pdf:
                        content = "\n".join(p.extract_text() or "" for p in pdf.pages).strip()
                    knowledge_cache[business_id] = content
                    return content
                except Exception:
                    pass

    knowledge_cache[business_id] = ""
    return ""


def load_business_config(business_id: str) -> dict:
    base = client_dir(business_id)
    config = {}
    if not base:
        return config
    config_path = os.path.join(base, "config", "business_config.txt")
    if os.path.exists(config_path):
        with open(config_path, encoding="utf-8") as f:
            for line in f:
                if "=" in line:
                    k, v = line.split("=", 1)
                    config[k.strip()] = v.strip()
    return config


def load_chatbot_config(business_id: str) -> dict:
    defaults = {
        "primary_color":   "#7c3aed",
        "secondary_color": "#f3f4f6",
        "icon":            "default",
        "greeting":        "Hi! How can I help you today?",
        "language_lock":   None,
        "business_name":   "Business",
        "bot_name":        "Aria",
        "bot_tagline":     "Your AI Assistant",
        "collect_leads":   True,
        "widget_position": "bottom-right",
        "white_label":     False,
        "color_mode":      "manual",   # "manual" | "auto" (auto = detect from website)
        "logo_mode":       "default",  # "default" | "custom"
    }
    base = client_dir(business_id)
    if base:
        path = os.path.join(base, "config", "chatbot_config.json")
        if os.path.exists(path):
            try:
                with open(path) as f:
                    defaults.update(json.load(f))
            except Exception:
                pass
    return defaults


def load_owner_info(business_id: str) -> str:
    """Owner-written notes about the business and themselves (the PRIMARY source)."""
    base = client_dir(business_id)
    if not base:
        return ""
    path = os.path.join(base, "config", "owner_info.txt")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    return ""


def business_plan(business_id: str) -> str:
    return plans.normalize_plan(load_business_config(business_id).get("plan", plans.DEFAULT_PLAN))


def plan_features(business_id: str) -> dict:
    return plans.get_plan(business_plan(business_id))


def logo_info(business_id: str):
    """Return (relative_filename, mimetype) of a saved custom logo, or (None, None)."""
    base = client_dir(business_id)
    if not base:
        return None, None
    for ext, mime in LOGO_EXTS.items():
        fname = f"logo.{ext}"
        if os.path.exists(os.path.join(base, "config", fname)):
            return fname, mime
    return None, None


# ═══════════════════════════════════════════════════════════════════════════════
# PERSISTENCE — transcripts, stats, sessions survive restarts
# ═══════════════════════════════════════════════════════════════════════════════

def _conv_path(business_id: str, session_id: str):
    base = client_dir(business_id)
    if not base or not re.match(r"^[a-f0-9-]{8,40}$", session_id):
        return None
    return os.path.join(base, "data", "conversations", f"{session_id}.json")


def save_conversation(business_id: str, session_id: str, session: dict):
    path = _conv_path(business_id, session_id)
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        record = {
            "session_id":    session_id,
            "business_id":   business_id,
            "started_at":    session.get("started_at"),
            "last_active":   datetime.now().isoformat(),
            "lead_captured": session.get("lead_captured", False),
            "history":       session.get("history", []),
        }
        with _disk_lock:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(record, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[CONV SAVE ERROR] {e}")


def load_conversation(business_id: str, session_id: str):
    path = _conv_path(business_id, session_id)
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _stats_path(business_id: str):
    base = client_dir(business_id)
    return os.path.join(base, "data", "stats.json") if base else None


def bump_stats(business_id: str, chats: int = 0, messages: int = 0):
    path = _stats_path(business_id)
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        this_month = datetime.now().strftime("%Y-%m")
        with _disk_lock:
            data = {"total_chats": 0, "total_messages": 0, "first_seen": None,
                    "month": this_month, "chats_this_month": 0}
            if os.path.exists(path):
                try:
                    with open(path) as f:
                        data.update(json.load(f))
                except Exception:
                    pass
            if data.get("first_seen") is None:
                data["first_seen"] = datetime.now().isoformat()
            # Monthly rollover — reset the monthly chat counter at the start of a new month
            if data.get("month") != this_month:
                data["month"] = this_month
                data["chats_this_month"] = 0
            data["total_chats"]      = data.get("total_chats", 0) + chats
            data["chats_this_month"] = data.get("chats_this_month", 0) + chats
            data["total_messages"]   = data.get("total_messages", 0) + messages
            data["last_active"]      = datetime.now().isoformat()
            with open(path, "w") as f:
                json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[STATS ERROR] {e}")


def monthly_chats_used(business_id: str) -> int:
    stats = read_stats(business_id)
    if stats.get("month") != datetime.now().strftime("%Y-%m"):
        return 0  # new month, counter effectively reset
    return stats.get("chats_this_month", 0)


def read_stats(business_id: str) -> dict:
    path = _stats_path(business_id)
    if path and os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def get_or_load_session(session_id: str, business_id: str = "") -> dict:
    """Return an in-memory session, rehydrating from disk after a restart if needed."""
    if session_id in sessions:
        return sessions[session_id]
    if not business_id or not business_exists(business_id):
        return None
    record = load_conversation(business_id, session_id)
    config = load_chatbot_config(business_id)
    config["business_name"] = load_business_config(business_id).get(
        "business_name", business_id.replace("_", " ").title()
    )
    sessions[session_id] = {
        "business_id":   business_id,
        "history":       record.get("history", []) if record else [],
        "lead_captured": record.get("lead_captured", False) if record else False,
        "started_at":    record.get("started_at") if record else datetime.now().isoformat(),
        "config":        config,
        "rehydrated":    record is not None,
    }
    return sessions[session_id]


# ═══════════════════════════════════════════════════════════════════════════════
# CORE AI
# ═══════════════════════════════════════════════════════════════════════════════

def ai_chat_response(message: str, business_id: str, session: dict, knowledge: str, config: dict,
                     owner_info: str = "", lead_capture: bool = True) -> dict:
    business_name = config.get("business_name", "the business")
    bot_name      = config.get("bot_name", "Aria")
    language_lock = config.get("language_lock")

    if language_lock:
        language_instruction = (
            f'LANGUAGE: Always write the "reply" value in {language_lock} only, '
            f"regardless of what language the customer writes in."
        )
    else:
        language_instruction = (
            'LANGUAGE RULE (mandatory): Detect the language of the customer\'s latest message. '
            'The "reply" value MUST be written in that exact same language — no exceptions. '
            'This applies to ALL actions including collect_lead and end. '
            'Examples: Urdu message → Urdu reply. Spanish message → Spanish reply. Arabic message → Arabic reply. '
            'Do NOT reply in English if the customer wrote in another language. '
            'The JSON keys "reply", "action", "language" always stay in English. '
            'The "action" value is always one of: chat | collect_lead | end — never translated. '
            'The "language" value is the English name of the language you replied in (e.g. "Urdu", "Spanish", "Arabic").'
        )

    # Two knowledge tiers. Owner-provided info is authoritative and checked first;
    # the scraped website knowledge is the fallback. Split the budget between them.
    owner_info = (owner_info or "").strip()[:MAX_KNOWLEDGE_CHARS // 2]
    remaining  = MAX_KNOWLEDGE_CHARS - len(owner_info)
    knowledge  = (knowledge or "").strip()[:max(remaining, 1000)]

    owner_section     = owner_info if owner_info else "(No owner-provided info yet.)"
    knowledge_section = knowledge if knowledge else "(No scraped website knowledge yet.)"

    if lead_capture:
        lead_rule = "- If the customer asks to speak to someone, get a quote, book, or be contacted: action = collect_lead."
    else:
        lead_rule = ("- Lead capture is OFF for this plan. Never use collect_lead. If they want to be contacted, "
                     "point them to the contact details in the info above.")

    system_prompt = f"""You are {bot_name}, the friendly AI assistant for {business_name}. Always respond with valid JSON only.

OUTPUT FORMAT (required): {{"reply": "...", "action": "chat", "language": "English"}}
- "reply": your answer to the customer
- "action": one of chat | collect_lead | end  (always in English)
- "language": the English name of the language you are replying in

Your name is {bot_name}. If asked who you are, say you're {business_name}'s assistant.

PRIMARY SOURCE — OWNER-PROVIDED INFO (trust this first):
{owner_section}

SECONDARY SOURCE — SCRAPED WEBSITE KNOWLEDGE (use if the answer isn't in the owner info):
{knowledge_section}

HOW TO ANSWER:
- For questions about {business_name} (prices, hours, services, policies, contact): answer ONLY from the two sources above, checking owner info first. Never invent business-specific facts.
- If a business-specific answer truly isn't in either source, say you don't have that detail and offer the contact info if available.
- For general questions (everyday facts, definitions, small talk, "what is a wall clock"), it's fine to answer helpfully from common knowledge — be warm and natural, don't rigidly refuse. Then gently steer back to how you can help with {business_name}.
- Keep replies to 1-3 sentences. Don't open with Hi/Hello every time.
{lead_rule}
- If the customer says goodbye and is done: action = end.

{language_instruction}"""

    try:
        from openai import OpenAI
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

        messages = [{"role": "system", "content": system_prompt}]
        for h in session.get("history", [])[-12:]:
            messages.append({"role": "user",      "content": h["customer"]})
            messages.append({"role": "assistant", "content": h["atlyz"]})
        messages.append({"role": "user", "content": message})

        response = client.chat.completions.create(
            model="gpt-5-nano",
            messages=messages,
            max_completion_tokens=400,
            response_format={"type": "json_object"}
        )

        raw = (response.choices[0].message.content or "").strip()

        try:
            return json.loads(raw)
        except Exception:
            pass

        cleaned = raw.replace("```json", "").replace("```", "").strip()
        try:
            return json.loads(cleaned)
        except Exception:
            pass

        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except Exception:
                pass

        return {"reply": "I'm having trouble right now. Please try again in a moment.", "action": "chat", "language": "English"}

    except Exception as e:
        print(f"[CHAT AI ERROR] {e}")
        return {"reply": "Sorry, I'm having a technical issue. Please try again shortly.", "action": "chat", "language": "English"}


# ═══════════════════════════════════════════════════════════════════════════════
# LEAD CAPTURE
# ═══════════════════════════════════════════════════════════════════════════════

def save_lead(business_id: str, lead: dict):
    base = client_dir(business_id)
    if not base:
        return
    try:
        path = os.path.join(base, "data", "leads.csv")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        write_header = not os.path.exists(path)
        with _disk_lock:
            with open(path, "a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=["lead_id", "timestamp", "name", "email", "phone", "question", "session_id"])
                if write_header:
                    writer.writeheader()
                writer.writerow(lead)
    except Exception as e:
        print(f"[LEAD SAVE ERROR] {e}")


def read_leads(business_id: str) -> list:
    base = client_dir(business_id)
    if not base:
        return []
    path = os.path.join(base, "data", "leads.csv")
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


# ═══════════════════════════════════════════════════════════════════════════════
# PUBLIC CHAT ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/chat/start", methods=["POST"])
def chat_start():
    data = request.get_json(silent=True) or {}
    business_id = data.get("business_id", "")

    if not business_id:
        return jsonify({"error": "business_id required"}), 400
    if not business_exists(business_id):
        return jsonify({"error": "Unknown business"}), 404

    session_id = str(uuid.uuid4())
    config = load_chatbot_config(business_id)
    business_config = load_business_config(business_id)
    config["business_name"] = business_config.get("business_name", business_id.replace("_", " ").title())

    feats = plan_features(business_id)
    cap   = feats.get("monthly_chats")
    over_limit = cap is not None and monthly_chats_used(business_id) >= cap

    greeting = config.get("greeting", "Hi! How can I help you today?")
    if over_limit:
        greeting = ("Thanks for stopping by! Our live chat has reached its limit for this month. "
                    "Please reach out through the contact details on this site and we'll get right back to you.")

    sessions[session_id] = {
        "business_id":   business_id,
        "history":       [],
        "lead_captured": False,
        "started_at":    datetime.now().isoformat(),
        "config":        config,
        "over_limit":    over_limit,
    }
    if not over_limit:
        bump_stats(business_id, chats=1)

    # White-label only takes effect on plans that include it
    white_label = bool(config.get("white_label")) and feats.get("white_label", False)

    logo_fname, _ = logo_info(business_id)
    logo_url = f"/chat/logo/{business_id}" if logo_fname else None

    return jsonify({
        "session_id":    session_id,
        "greeting":      greeting,
        "business_name": config["business_name"],
        "chat_enabled":  not over_limit,
        "config": {
            "primary_color":   config.get("primary_color", "#7c3aed"),
            "widget_position": config.get("widget_position", "bottom-right"),
            "bot_name":        config.get("bot_name", "Aria"),
            "bot_tagline":     config.get("bot_tagline", "Your AI Assistant"),
            "white_label":     white_label,
            "logo_url":        logo_url,
            "lead_capture":    feats.get("lead_capture", False),
        }
    })


@app.route("/chat/message", methods=["POST"])
def chat_message():
    data = request.get_json(silent=True) or {}
    session_id  = data.get("session_id", "")
    business_id = data.get("business_id", "")
    message     = (data.get("message", "") or "").strip()

    if not session_id:
        return jsonify({"error": "Invalid session"}), 400
    if not message:
        return jsonify({"error": "Empty message"}), 400

    # Cost guard: cap message length
    if len(message) > MAX_MESSAGE_CHARS:
        message = message[:MAX_MESSAGE_CHARS]

    session = get_or_load_session(session_id, business_id)
    if not session:
        return jsonify({"error": "Invalid session"}), 400

    # Abuse guards: per-session and per-IP
    if not check_rate_limit(rate_limits, session_id, RATE_LIMIT_MAX) or \
       not check_rate_limit(ip_rate_limits, client_ip(), IP_RATE_LIMIT_MAX):
        return jsonify({
            "reply": "You're sending messages too fast. Please wait a moment.",
            "action": "chat",
            "language": "English"
        }), 429

    business_id = session["business_id"]
    config      = session["config"]

    # Plan chat cap reached — answer with a fallback, don't spend on the AI
    if session.get("over_limit"):
        notice = ("Our live chat has hit its monthly limit. Please use the contact details on this "
                  "site to reach the team and they'll follow up with you shortly.")
        session["history"].append({"customer": message, "atlyz": notice, "ts": datetime.now().isoformat()})
        save_conversation(business_id, session_id, session)
        return jsonify({"reply": notice, "action": "chat", "language": "English", "session_id": session_id})

    feats      = plan_features(business_id)
    knowledge  = load_knowledge(business_id)
    owner_info = load_owner_info(business_id)

    result   = ai_chat_response(message, business_id, session, knowledge, config,
                                owner_info=owner_info, lead_capture=feats.get("lead_capture", False))
    reply    = result.get("reply", "Sorry, I couldn't process that.")
    action   = result.get("action", "chat")
    language = result.get("language", "English")

    # Hard gate: never emit a lead action on a plan without lead capture
    if action == "collect_lead" and not feats.get("lead_capture", False):
        action = "chat"

    session["history"].append({"customer": message, "atlyz": reply, "ts": datetime.now().isoformat()})
    session["last_language"] = language
    if len(session["history"]) > 50:
        session["history"] = session["history"][-50:]

    save_conversation(business_id, session_id, session)
    bump_stats(business_id, messages=1)

    return jsonify({"reply": reply, "action": action, "language": language, "session_id": session_id})


@app.route("/chat/lead", methods=["POST"])
def chat_lead():
    data = request.get_json(silent=True) or {}
    session_id  = data.get("session_id", "")
    business_id = data.get("business_id", "")

    session = get_or_load_session(session_id, business_id)
    if not session:
        return jsonify({"error": "Invalid session"}), 400

    business_id = session["business_id"]
    config      = session["config"]

    if not plan_features(business_id).get("lead_capture", False):
        return jsonify({"error": "Lead capture is not available on this plan"}), 403

    lead = {
        "lead_id":    str(uuid.uuid4())[:8],
        "timestamp":  datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "name":       (data.get("name", "") or "")[:120],
        "email":      (data.get("email", "") or "")[:160],
        "phone":      (data.get("phone", "") or "")[:40],
        "question":   (data.get("question", "") or "")[:500],
        "session_id": session_id
    }

    save_lead(business_id, lead)
    session["lead_captured"] = True
    save_conversation(business_id, session_id, session)

    business_config = load_business_config(business_id)
    owner_email     = business_config.get("owner_email", "")
    business_name   = config.get("business_name", business_id)
    send_lead_email(owner_email, business_name, lead)

    return jsonify({"success": True, "message": "Thank you! The owner will be in touch with you shortly."})


@app.route("/chat/config/<business_id>", methods=["GET"])
def get_chat_config(business_id):
    if not business_exists(business_id):
        return jsonify({"error": "Unknown business"}), 404
    config = load_chatbot_config(business_id)
    business_config = load_business_config(business_id)
    config["business_name"] = business_config.get("business_name", business_id.replace("_", " ").title())
    feats = plan_features(business_id)
    logo_fname, _ = logo_info(business_id)
    config["plan"]         = business_plan(business_id)
    config["features"]     = feats
    config["logo_url"]     = f"/chat/logo/{business_id}" if logo_fname else None
    config["white_label"]  = bool(config.get("white_label")) and feats.get("white_label", False)
    return jsonify(config)


@app.route("/widget.js")
def widget_js():
    return send_from_directory(os.path.join(BASE_DIR, "static"), "widget.js", mimetype="application/javascript")


@app.route("/chat/logo/<business_id>")
def chat_logo(business_id):
    if not business_exists(business_id):
        return jsonify({"error": "Unknown business"}), 404
    fname, mime = logo_info(business_id)
    if not fname:
        return jsonify({"error": "No logo"}), 404
    return send_from_directory(os.path.join(client_dir(business_id), "config"), fname, mimetype=mime)


@app.route("/dashboard")
@app.route("/dashboard/")
def dashboard_page():
    return render_template("dashboard.html")


@app.route("/chat/test/<business_id>")
def test_page(business_id):
    if not business_exists(business_id):
        return jsonify({"error": "Unknown business"}), 404
    config          = load_chatbot_config(business_id)
    business_config = load_business_config(business_id)
    business_name   = business_config.get("business_name", business_id.replace("_", " ").title())
    return render_template("chat_test.html",
                           business_id=business_id,
                           business_name=business_name,
                           config=config)


# ═══════════════════════════════════════════════════════════════════════════════
# OWNER AUTH (foundation for the dashboard)
# ═══════════════════════════════════════════════════════════════════════════════

def _find_businesses_by_email(email: str) -> list:
    matches = []
    if not os.path.isdir(CLIENTS_DIR):
        return matches
    email = email.lower().strip()
    for bid in os.listdir(CLIENTS_DIR):
        if not valid_business_id(bid):
            continue
        cfg = load_business_config(bid)
        if cfg.get("owner_email", "").lower().strip() == email and cfg.get("owner_password_hash"):
            matches.append(bid)
    return matches


@app.route("/auth/login", methods=["POST"])
def auth_login():
    data     = request.get_json(silent=True) or {}
    email    = (data.get("email", "") or "").strip()
    password = data.get("password", "") or ""

    if not email or not password:
        return jsonify({"error": "email and password required"}), 400

    owned = []
    for bid in _find_businesses_by_email(email):
        cfg = load_business_config(bid)
        if check_password_hash(cfg.get("owner_password_hash", ""), password):
            owned.append(bid)

    if not owned:
        return jsonify({"error": "Invalid email or password"}), 401

    flask_session["owner_email"] = email
    flask_session["owner_of"]    = owned
    flask_session.permanent      = True
    return jsonify({"success": True, "businesses": owned})


@app.route("/auth/logout", methods=["POST"])
def auth_logout():
    flask_session.clear()
    return jsonify({"success": True})


@app.route("/auth/me", methods=["GET"])
def auth_me():
    return jsonify({
        "owner_email": flask_session.get("owner_email"),
        "businesses":  flask_session.get("owner_of", [])
    })


# ═══════════════════════════════════════════════════════════════════════════════
# OWNER-SCOPED DATA (auth required — admin key or logged-in owner)
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/chatbot/config/<business_id>", methods=["POST"])
def save_chatbot_config(business_id):
    if not business_exists(business_id):
        return jsonify({"error": "Unknown business"}), 404
    if not is_owner_of(business_id):
        return jsonify({"error": "Unauthorized"}), 401

    data    = request.get_json(silent=True) or {}
    current = load_chatbot_config(business_id)
    for field in ["primary_color", "greeting", "widget_position", "business_name",
                  "language_lock", "bot_name", "bot_tagline", "color_mode", "logo_mode"]:
        if field in data:
            current[field] = data[field]

    # White-label is a Pro-only feature — silently clamp on lower plans
    if "white_label" in data:
        current["white_label"] = bool(data["white_label"]) and plan_features(business_id).get("white_label", False)

    config_path = os.path.join(client_dir(business_id), "config", "chatbot_config.json")
    with open(config_path, "w") as f:
        json.dump(current, f, indent=2)
    return jsonify({"success": True})


@app.route("/chatbot/stats/<business_id>", methods=["GET"])
def chatbot_stats(business_id):
    if not business_exists(business_id):
        return jsonify({"error": "Unknown business"}), 404
    if not is_owner_of(business_id):
        return jsonify({"error": "Unauthorized"}), 401

    leads_count = len(read_leads(business_id))
    stats       = read_stats(business_id)

    scrape_meta = {}
    meta_path = os.path.join(client_dir(business_id), "config", "scrape_meta.json")
    if os.path.exists(meta_path):
        try:
            with open(meta_path) as f:
                scrape_meta = json.load(f)
        except Exception:
            pass

    active = [s for s in sessions.values() if s.get("business_id") == business_id]
    plan  = business_plan(business_id)
    feats = plans.get_plan(plan)

    return jsonify({
        "business_id":      business_id,
        "plan":             plan,
        "plan_label":       feats.get("label", plan.title()),
        "monthly_cap":      feats.get("monthly_chats"),   # null = unlimited
        "chats_this_month": monthly_chats_used(business_id),
        "leads_total":      leads_count,
        "total_chats":      stats.get("total_chats", 0),
        "total_messages":   stats.get("total_messages", 0),
        "active_sessions":  len(active),
        "last_active":      stats.get("last_active", "never"),
        "last_scraped":     scrape_meta.get("timestamp", "never"),
        "pages_scraped":    scrape_meta.get("pages_scraped", 0)
    })


@app.route("/owner/leads/<business_id>", methods=["GET"])
def owner_leads(business_id):
    if not business_exists(business_id):
        return jsonify({"error": "Unknown business"}), 404
    if not is_owner_of(business_id):
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify({"business_id": business_id, "leads": read_leads(business_id)})


@app.route("/owner/conversations/<business_id>", methods=["GET"])
def owner_conversations(business_id):
    if not business_exists(business_id):
        return jsonify({"error": "Unknown business"}), 404
    if not is_owner_of(business_id):
        return jsonify({"error": "Unauthorized"}), 401

    conv_dir = os.path.join(client_dir(business_id), "data", "conversations")
    items = []
    if os.path.isdir(conv_dir):
        files = sorted(
            (os.path.join(conv_dir, f) for f in os.listdir(conv_dir) if f.endswith(".json")),
            key=os.path.getmtime, reverse=True
        )
        for path in files[:50]:
            try:
                with open(path, encoding="utf-8") as f:
                    rec = json.load(f)
                items.append({
                    "session_id":  rec.get("session_id"),
                    "started_at":  rec.get("started_at"),
                    "last_active": rec.get("last_active"),
                    "turns":       len(rec.get("history", [])),
                    "lead":        rec.get("lead_captured", False),
                    "history":     rec.get("history", []),
                })
            except Exception:
                continue
    return jsonify({"business_id": business_id, "conversations": items})


@app.route("/owner/info/<business_id>", methods=["GET", "POST"])
def owner_info_route(business_id):
    """Owner-written notes about the business and themselves (PRIMARY knowledge source)."""
    if not business_exists(business_id):
        return jsonify({"error": "Unknown business"}), 404
    if not is_owner_of(business_id):
        return jsonify({"error": "Unauthorized"}), 401

    if request.method == "GET":
        return jsonify({"business_id": business_id, "owner_info": load_owner_info(business_id)})

    data = request.get_json(silent=True) or {}
    text = (data.get("owner_info", "") or "").strip()[:20000]
    path = os.path.join(client_dir(business_id), "config", "owner_info.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return jsonify({"success": True, "chars": len(text)})


@app.route("/owner/logo/<business_id>", methods=["POST", "DELETE"])
def owner_logo(business_id):
    """Upload or remove the bot's custom logo (available on all plans)."""
    if not business_exists(business_id):
        return jsonify({"error": "Unknown business"}), 404
    if not is_owner_of(business_id):
        return jsonify({"error": "Unauthorized"}), 401

    config_dir = os.path.join(client_dir(business_id), "config")

    # Remove any existing logo first (only one logo per business)
    for ext in LOGO_EXTS:
        old = os.path.join(config_dir, f"logo.{ext}")
        if os.path.exists(old):
            os.remove(old)

    if request.method == "DELETE":
        _set_config_field(business_id, "logo_mode", "default")
        return jsonify({"success": True, "logo_url": None})

    file = request.files.get("logo")
    if not file or not file.filename:
        return jsonify({"error": "No file uploaded (field name: logo)"}), 400

    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in LOGO_EXTS:
        return jsonify({"error": f"Unsupported type. Allowed: {', '.join(LOGO_EXTS)}"}), 400

    blob = file.read(MAX_LOGO_BYTES + 1)
    if len(blob) > MAX_LOGO_BYTES:
        return jsonify({"error": "Logo too large (max 512 KB)"}), 400

    with open(os.path.join(config_dir, f"logo.{ext}"), "wb") as f:
        f.write(blob)
    _set_config_field(business_id, "logo_mode", "custom")
    return jsonify({"success": True, "logo_url": f"/chat/logo/{business_id}"})


def _set_config_field(business_id: str, key: str, value):
    cfg = load_chatbot_config(business_id)
    cfg[key] = value
    with open(os.path.join(client_dir(business_id), "config", "chatbot_config.json"), "w") as f:
        json.dump(cfg, f, indent=2)


# ═══════════════════════════════════════════════════════════════════════════════
# SCRAPING
# ═══════════════════════════════════════════════════════════════════════════════

def run_scrape(business_id: str, website_url: str, force: bool = True) -> dict:
    """Scrape, replace knowledge, and apply auto-detected brand color when enabled.
    With force=False, skips if the site content hash matches the last scrape."""
    try:
        from scraper import scrape_website, save_scraped_knowledge
        max_pages = plan_features(business_id).get("scrape_pages", 50)
        result = scrape_website(website_url, max_pages=max_pages)
        if result.get("status") != "ok":
            return result

        if not force:
            meta_path = os.path.join(client_dir(business_id), "config", "scrape_meta.json")
            if os.path.exists(meta_path):
                try:
                    with open(meta_path) as f:
                        prev = json.load(f)
                    if prev.get("content_hash") and prev["content_hash"] == result.get("content_hash"):
                        return {"status": "skipped"}
                except Exception:
                    pass

        save_scraped_knowledge(business_id, result, clients_dir=CLIENTS_DIR)
        knowledge_cache.pop(business_id, None)

        # Auto color: when enabled and a brand color was detected, adopt it
        cfg = load_chatbot_config(business_id)
        if cfg.get("color_mode") == "auto" and result.get("brand_color"):
            _set_config_field(business_id, "primary_color", result["brand_color"])

        return result
    except Exception as e:
        print(f"[SCRAPE ERROR] {business_id}: {e}")
        return {"status": "error", "error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN ROUTES (admin key required)
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/chat/scrape", methods=["POST"])
def scrape_endpoint():
    if not check_admin_key():
        return jsonify({"error": "Unauthorized"}), 401

    data        = request.get_json(silent=True) or {}
    business_id = data.get("business_id", "")
    website_url = data.get("url", "")

    if not business_id or not website_url:
        return jsonify({"error": "business_id and url required"}), 400
    if not business_exists(business_id):
        return jsonify({"error": "Unknown business"}), 404

    result = run_scrape(business_id, website_url)
    if result.get("status") == "ok":
        return jsonify({
            "success":       True,
            "pages_scraped": result["pages_scraped"],
            "brand_color":   result.get("brand_color", ""),
            "preview":       result["content"][:300]
        })
    return jsonify({"success": False, "error": result.get("error", "Scrape failed")}), 400


@app.route("/chat/rescrape", methods=["POST"])
def rescrape_endpoint():
    """Re-scrape a website. Replaces old knowledge with fresh content. Owner or admin.
    Skips work if the site content is unchanged unless force=true."""
    data        = request.get_json(silent=True) or {}
    business_id = data.get("business_id", "")
    force       = bool(data.get("force", False))

    if not business_exists(business_id):
        return jsonify({"error": "Unknown business"}), 404
    if not is_owner_of(business_id):
        return jsonify({"error": "Unauthorized"}), 401

    website_url = data.get("url", "") or load_business_config(business_id).get("website", "")
    if not website_url:
        return jsonify({"error": "No website on file"}), 400

    result = run_scrape(business_id, website_url, force=force)
    if result.get("status") == "skipped":
        return jsonify({"success": True, "changed": False, "message": "Website unchanged since last scrape"})
    if result.get("status") == "ok":
        return jsonify({
            "success":       True,
            "changed":       True,
            "pages_scraped": result["pages_scraped"],
            "brand_color":   result.get("brand_color", ""),
        })
    return jsonify({"success": False, "error": result.get("error", "Scrape failed")}), 400


@app.route("/setup/create", methods=["POST"])
def setup_create():
    if not check_admin_key():
        return jsonify({"error": "Unauthorized"}), 401

    data          = request.get_json(silent=True) or {}
    business_name = (data.get("business_name", "") or "").strip()
    website_url   = (data.get("website_url", "") or "").strip()
    owner_email   = (data.get("owner_email", "") or "").strip()
    password      = data.get("password", "") or ""
    primary_color = data.get("primary_color", "#7c3aed")
    color_mode    = "auto" if (data.get("color_mode") == "auto") else "manual"
    greeting      = (data.get("greeting", "") or "").strip()
    position      = data.get("widget_position", "bottom-right")
    plan          = plans.normalize_plan(data.get("plan", "starter"))
    owner_info    = (data.get("owner_info", "") or "").strip()[:20000]

    if not business_name or not website_url:
        return jsonify({"success": False, "error": "Business name and website URL are required"}), 400

    slug        = re.sub(r"[^a-z0-9]+", "-", business_name.lower()).strip("-")[:40] or "business"
    business_id = slug
    counter     = 2
    while os.path.exists(os.path.join(CLIENTS_DIR, business_id)):
        business_id = f"{slug}-{counter}"
        counter += 1

    config_dir = os.path.join(CLIENTS_DIR, business_id, "config")
    data_dir   = os.path.join(CLIENTS_DIR, business_id, "data")
    os.makedirs(config_dir, exist_ok=True)
    os.makedirs(data_dir,   exist_ok=True)

    with open(os.path.join(config_dir, "business_config.txt"), "w") as f:
        f.write(f"business_name = {business_name}\n")
        f.write(f"owner_email = {owner_email}\n")
        f.write(f"website = {website_url}\n")
        f.write(f"plan = {plan}\n")
        if password:
            f.write(f"owner_password_hash = {generate_password_hash(password)}\n")

    if owner_info:
        with open(os.path.join(config_dir, "owner_info.txt"), "w", encoding="utf-8") as f:
            f.write(owner_info)

    if not greeting:
        greeting = f"Hi! I'm {data.get('bot_name', 'Aria')}, the virtual assistant for {business_name}. How can I help you today?"

    chatbot_cfg = {
        "primary_color":   primary_color,
        "secondary_color": "#f3f4f6",
        "icon":            "default",
        "greeting":        greeting,
        "language_lock":   None,
        "business_name":   business_name,
        "bot_name":        data.get("bot_name", "Aria"),
        "bot_tagline":     data.get("bot_tagline", "Your AI Assistant"),
        "collect_leads":   True,
        "widget_position": position,
        "white_label":     False,
        "color_mode":      color_mode,
        "logo_mode":       "default",
    }
    with open(os.path.join(config_dir, "chatbot_config.json"), "w") as f:
        json.dump(chatbot_cfg, f, indent=2)

    result = run_scrape(business_id, website_url)
    pages_scraped  = result.get("pages_scraped", 0) if result.get("status") == "ok" else 0
    scrape_preview = result.get("content", "")[:200] if result.get("status") == "ok" else ""

    return jsonify({
        "success":        True,
        "business_id":    business_id,
        "plan":           plan,
        "pages_scraped":  pages_scraped,
        "scrape_ok":      pages_scraped > 0,
        "scrape_error":   result.get("error") if result.get("status") != "ok" else None,
        "brand_color":    result.get("brand_color", "") if result.get("status") == "ok" else "",
        "scrape_preview": scrape_preview,
        "embed_code":     f'<script src="SERVER_URL/widget.js?id={business_id}"></script>'
    })


# ═══════════════════════════════════════════════════════════════════════════════
# CONTACT FORM (company website)
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/contact", methods=["POST"])
def contact_form():
    data    = request.get_json(silent=True) or {}
    name    = (data.get("name", "") or "").strip()[:120]
    email   = (data.get("email", "") or "").strip()[:160]
    topic   = (data.get("topic", "") or "").strip()[:120]
    message = (data.get("message", "") or "").strip()[:4000]

    if not email or not message:
        return jsonify({"error": "email and message required"}), 400

    print(f"[CONTACT] From: {name} <{email}> | Topic: {topic}")

    try:
        from_addr = os.getenv("EMAIL_FROM")
        password  = os.getenv("EMAIL_PASSWORD")
        to_addr   = os.getenv("EMAIL_FROM")
        if from_addr and password:
            body = f"New contact form submission\n\nName: {name}\nEmail: {email}\nTopic: {topic}\n\nMessage:\n{message}"
            msg  = MIMEText(body)
            msg["Subject"] = f"[Atlyz] Contact: {topic or 'General'} from {name or email}"
            msg["From"]    = from_addr
            msg["To"]      = to_addr
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(from_addr, password)
                server.sendmail(from_addr, to_addr, msg.as_string())
            print(f"[CONTACT] Email sent to {to_addr}")
    except Exception as e:
        print(f"[CONTACT] Email failed (still logged above): {e}")

    return jsonify({"success": True})


@app.route("/health")
def health():
    return jsonify({"status": "ok", "sessions": len(sessions)})


if __name__ == "__main__":
    print("=" * 50)
    print("  ATLYZ — Chat Server")
    print(f"  API:  http://localhost:{os.environ.get('PORT', 5002)}")
    print("  Test: /chat/test/quickfix_plumbing")
    print("=" * 50)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5002)), debug=DEV_MODE)
