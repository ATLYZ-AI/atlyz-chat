# chatbot_server.py — Atlyz Chat Server

import os
import json
import re
import uuid
import time
import csv
import threading
from datetime import datetime
import requests
from dotenv import load_dotenv
from flask import Flask, request, jsonify, render_template, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

import plans

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT   = os.path.dirname(BASE_DIR)   # parent of Atlyz chat/ — where .env lives

# Load .env from repo root (one level up) — handles running from inside Atlyz chat/
load_dotenv(os.path.join(REPO_ROOT, ".env"))

LOGO_EXTS    = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                "gif": "image/gif", "svg": "image/svg+xml", "webp": "image/webp"}
MAX_LOGO_BYTES = 512 * 1024  # 512 KB
# DATA_DIR: on Railway set to /data (mounted volume) so data survives deploys.
DATA_DIR    = os.getenv("DATA_DIR", BASE_DIR)
CLIENTS_DIR = os.path.join(DATA_DIR, "clients")
ACCOUNTS_DIR = os.path.join(DATA_DIR, "accounts")
ACCOUNTS_FILE = os.path.join(ACCOUNTS_DIR, "accounts.json")

DEV_MODE = os.getenv("DEV_MODE", "false").lower() == "true"

app = Flask(__name__)
_secret = os.getenv("SECRET_KEY", "")
if not _secret:
    if not DEV_MODE:
        raise RuntimeError("SECRET_KEY must be set in production (DEV_MODE=false).")
    _secret = "atlyz-chat-dev-secret"
app.secret_key = _secret

# Signed auth tokens — stateless, survive restarts, work across gunicorn workers.
token_serializer = URLSafeTimedSerializer(_secret, salt="atlyz-auth")
TOKEN_MAX_AGE    = 60 * 60 * 24 * 30  # 30 days

# ── Tunables ────────────────────────────────────────────────────────────────────
MAX_MESSAGE_CHARS   = int(os.getenv("MAX_MESSAGE_CHARS", 2000))
MAX_KNOWLEDGE_CHARS = int(os.getenv("MAX_KNOWLEDGE_CHARS", 12000))
RATE_LIMIT_MAX      = int(os.getenv("RATE_LIMIT_MAX", 20))
RATE_LIMIT_WINDOW   = int(os.getenv("RATE_LIMIT_WINDOW", 60))
IP_RATE_LIMIT_MAX   = int(os.getenv("IP_RATE_LIMIT_MAX", 40))

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"]  = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Admin-Key, X-Auth-Token"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS, DELETE"
    return response


@app.before_request
def handle_options():
    if request.method == "OPTIONS":
        resp = app.make_default_options_response()
        resp.headers["Access-Control-Allow-Origin"]  = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Admin-Key, X-Auth-Token"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS, DELETE"
        return resp


# ── In-memory stores ────────────────────────────────────────────────────────────
sessions        = {}   # session_id → session data
knowledge_cache = {}   # business_id → knowledge text
rate_limits     = {}   # session_id → [timestamps]
ip_rate_limits  = {}   # ip → [timestamps]
_disk_lock      = threading.Lock()
_ais_ready      = False   # True once the startup auto-scrape thread finishes


# ═══════════════════════════════════════════════════════════════════════════════
# SECURITY HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

_VALID_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def valid_business_id(bid: str) -> bool:
    return bool(bid) and bool(_VALID_ID.match(bid))


def client_dir(bid: str):
    """Absolute clients/<id> path, or None if the id is unsafe."""
    if not valid_business_id(bid):
        return None
    path = os.path.normpath(os.path.join(CLIENTS_DIR, bid))
    if os.path.commonpath([path, CLIENTS_DIR]) != CLIENTS_DIR:
        return None
    return path


def business_exists(bid: str) -> bool:
    d = client_dir(bid)
    return bool(d) and os.path.isdir(d)


def check_admin_key() -> bool:
    required = os.getenv("ATLYZ_ADMIN_KEY", "")
    if not required:
        return DEV_MODE
    provided = (
        request.headers.get("X-Admin-Key", "") or
        request.args.get("key", "") or
        (request.get_json(silent=True) or {}).get("admin_key", "")
    )
    return bool(provided) and provided == required


def client_ip() -> str:
    fwd = request.headers.get("X-Forwarded-For", "")
    return fwd.split(",")[0].strip() if fwd else (request.remote_addr or "unknown")


def check_rate_limit(store: dict, key: str, limit: int) -> bool:
    now    = time.time()
    stamps = [t for t in store.get(key, []) if now - t < RATE_LIMIT_WINDOW]
    if len(stamps) >= limit:
        store[key] = stamps
        return False
    stamps.append(now)
    store[key] = stamps
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# AUTH — signed tokens + accounts.json
# ═══════════════════════════════════════════════════════════════════════════════

def issue_token(email: str) -> str:
    return token_serializer.dumps(email)


def email_from_token(token: str):
    if not token:
        return None
    try:
        return token_serializer.loads(token, max_age=TOKEN_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None


def request_account_email():
    """Email of the signed-in account on this request, or None."""
    body  = request.get_json(silent=True) or {}
    auth_header = request.headers.get("Authorization", "")
    bearer = auth_header[7:] if auth_header.startswith("Bearer ") else ""
    token = (bearer or
             body.get("account_token", "") or
             request.headers.get("X-Auth-Token", "") or
             request.args.get("token", ""))
    return email_from_token(token)


def load_accounts() -> dict:
    if os.path.exists(ACCOUNTS_FILE):
        try:
            with open(ACCOUNTS_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_accounts(accounts: dict):
    os.makedirs(ACCOUNTS_DIR, exist_ok=True)
    with _disk_lock:
        with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
            json.dump(accounts, f, indent=2)


def is_owner_of(bid: str) -> bool:
    """True if the request carries a valid admin key OR is an authenticated owner.
    Never bypasses auth via DEV_MODE — owner routes contain customer data."""
    required = os.getenv("ATLYZ_ADMIN_KEY", "")
    if required:
        provided = (
            request.headers.get("X-Admin-Key", "") or
            request.args.get("key", "") or
            (request.get_json(silent=True) or {}).get("admin_key", "")
        )
        if provided and provided == required:
            return True
    email = request_account_email()
    if not email:
        return False
    accounts = load_accounts()
    account  = accounts.get(email)
    if account and bid in account.get("businesses", []):
        return True
    # Fallback: owner_email in business_config matches (older accounts)
    cfg = load_business_config(bid)
    return cfg.get("owner_email", "").lower().strip() == email.lower().strip()


# ═══════════════════════════════════════════════════════════════════════════════
# NOTIFICATIONS
# ═══════════════════════════════════════════════════════════════════════════════

def send_welcome_email(email: str, name: str):
    try:
        api_key = os.getenv("RESEND_API_KEY", "")
        if not api_key or not email:
            return
        first = name.split()[0] if name.strip() else "there"
        body = (
            f"Hey {first},\n\n"
            "Welcome to Atlyz! Your account is ready.\n\n"
            "Here's what to do next:\n\n"
            "1. Go to your dashboard: https://app.atlyz.com/dashboard\n"
            "2. Enter your website URL — Atlyz reads it and builds your knowledge base in 60 seconds\n"
            "3. Copy your embed code and paste it on your site — your AI chatbot goes live instantly\n\n"
            "Setup takes under 5 minutes. No developer needed.\n\n"
            "Questions? Just reply to this email or contact us at support@atlyz.com — we're happy to help.\n\n"
            "— The Atlyz Team\n"
            "atlyz.com"
        )
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "from":     "Atlyz <noreply@send.atlyz.com>",
                "to":       [email],
                "reply_to": "support@atlyz.com",
                "subject":  "Welcome to Atlyz 👋",
                "text":     body,
            },
            timeout=10,
        )
        if resp.status_code >= 300:
            print(f"[WELCOME EMAIL ERROR] Resend {resp.status_code}: {resp.text}")
        else:
            print(f"[WELCOME] Email sent to {email}")
    except Exception as e:
        print(f"[WELCOME EMAIL ERROR] {e}")


def send_lead_email(owner_email: str, business_name: str, lead: dict):
    try:
        api_key = os.getenv("RESEND_API_KEY", "")
        if not api_key or not owner_email:
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
        payload = {
            "from":    "Atlyz Contact <noreply@send.atlyz.com>",
            "to":      [owner_email],
            "subject": f"[Atlyz] New lead — {lead.get('name') or lead.get('email') or 'Unknown'}",
            "text":    body,
        }
        # Reply-To the lead's email so the owner can reply to the customer directly.
        lead_email = (lead.get("email") or "").strip()
        if lead_email:
            payload["reply_to"] = lead_email

        resp = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=10,
        )
        if resp.status_code >= 300:
            print(f"[LEAD EMAIL ERROR] Resend {resp.status_code}: {resp.text}")
        else:
            print(f"[LEAD] Email sent to {owner_email}")
    except Exception as e:
        print(f"[LEAD EMAIL ERROR] {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG / KNOWLEDGE LOADERS
# ═══════════════════════════════════════════════════════════════════════════════

def load_knowledge(bid: str) -> str:
    if bid in knowledge_cache:
        return knowledge_cache[bid]
    base = client_dir(bid)
    if not base:
        return ""
    config_dir = os.path.join(base, "config")
    for fname in ["knowledge.txt", "knowledge.pdf"]:
        path = os.path.join(config_dir, fname)
        if os.path.exists(path):
            if fname.endswith(".txt"):
                with open(path, encoding="utf-8") as f:
                    content = f.read().strip()
                knowledge_cache[bid] = content
                return content
            elif fname.endswith(".pdf"):
                try:
                    import pdfplumber
                    with pdfplumber.open(path) as pdf:
                        content = "\n".join(p.extract_text() or "" for p in pdf.pages).strip()
                    knowledge_cache[bid] = content
                    return content
                except Exception:
                    pass
    knowledge_cache[bid] = ""
    return ""


def load_business_config(bid: str) -> dict:
    config = {}
    base   = client_dir(bid)
    if not base:
        return config
    path = os.path.join(base, "config", "business_config.txt")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                if "=" in line:
                    k, v = line.split("=", 1)
                    config[k.strip()] = v.strip()
    return config


def load_chatbot_config(bid: str) -> dict:
    defaults = {
        "primary_color":   "#00C2FF",
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
        "color_mode":      "manual",
        "logo_mode":       "default",
    }
    base = client_dir(bid)
    if base:
        path = os.path.join(base, "config", "chatbot_config.json")
        if os.path.exists(path):
            try:
                with open(path) as f:
                    defaults.update(json.load(f))
            except Exception:
                pass
    return defaults


def load_owner_info(bid: str) -> str:
    base = client_dir(bid)
    if not base:
        return ""
    path = os.path.join(base, "config", "owner_info.txt")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    return ""


def business_plan(bid: str) -> str:
    return plans.normalize_plan(load_business_config(bid).get("plan", plans.DEFAULT_PLAN))


def plan_features(bid: str) -> dict:
    return plans.get_plan(business_plan(bid))


def logo_info(bid: str):
    base = client_dir(bid)
    if not base:
        return None, None
    for ext, mime in LOGO_EXTS.items():
        if os.path.exists(os.path.join(base, "config", f"logo.{ext}")):
            return f"logo.{ext}", mime
    return None, None


# ═══════════════════════════════════════════════════════════════════════════════
# PERSISTENCE — conversations + stats survive restarts
# ═══════════════════════════════════════════════════════════════════════════════

def _conv_path(bid: str, session_id: str):
    base = client_dir(bid)
    if not base or not re.match(r"^[a-f0-9-]{8,40}$", session_id):
        return None
    return os.path.join(base, "data", "conversations", f"{session_id}.json")


def save_conversation(bid: str, session_id: str, session: dict):
    path = _conv_path(bid, session_id)
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        record = {
            "session_id":    session_id,
            "business_id":   bid,
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


def load_conversation(bid: str, session_id: str):
    path = _conv_path(bid, session_id)
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _stats_path(bid: str):
    base = client_dir(bid)
    return os.path.join(base, "data", "stats.json") if base else None


def bump_stats(bid: str, chats: int = 0, messages: int = 0):
    path = _stats_path(bid)
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


def monthly_chats_used(bid: str) -> int:
    stats = read_stats(bid)
    if stats.get("month") != datetime.now().strftime("%Y-%m"):
        return 0
    return stats.get("chats_this_month", 0)


def read_stats(bid: str) -> dict:
    path = _stats_path(bid)
    if path and os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def get_or_load_session(session_id: str, bid: str = "") -> dict:
    """Return an in-memory session, rehydrating from disk after a restart."""
    if session_id in sessions:
        return sessions[session_id]
    if not bid or not business_exists(bid):
        return None
    record = load_conversation(bid, session_id)
    config = load_chatbot_config(bid)
    config["business_name"] = load_business_config(bid).get(
        "business_name", bid.replace("_", " ").title()
    )
    sessions[session_id] = {
        "business_id":   bid,
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

def ai_chat_response(message: str, bid: str, session: dict, knowledge: str, config: dict,
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
            'Examples: Urdu message → Urdu reply. Spanish message → Spanish reply. '
            'Do NOT reply in English if the customer wrote in another language. '
            'The JSON keys "reply", "action", "language" always stay in English. '
            'The "action" value is always one of: chat | collect_lead | end — never translated. '
            'The "language" value is the English name of the language you replied in (e.g. "Urdu", "Spanish").'
        )

    owner_info = (owner_info or "").strip()[:MAX_KNOWLEDGE_CHARS // 2]
    remaining  = MAX_KNOWLEDGE_CHARS - len(owner_info)
    knowledge  = (knowledge or "").strip()[:max(remaining, 1000)]

    owner_section     = owner_info if owner_info else "(No owner-provided info yet.)"
    knowledge_section = knowledge if knowledge else "(No scraped website knowledge yet.)"

    if owner_info:
        knowledge_block = (f"OWNER-PROVIDED INFO (most authoritative — trust this first):\n{owner_section}\n\n"
                           f"WEBSITE KNOWLEDGE:\n{knowledge_section}")
    else:
        knowledge_block = knowledge_section

    if lead_capture:
        lead_rule = "- If the customer asks to speak to someone, get a quote, book an appointment, or be contacted: action = collect_lead."
    else:
        lead_rule = ("- Lead capture is OFF for this plan. Never use collect_lead. "
                     "If they want to be contacted, point them to the contact details in the knowledge above.")

    history_lines = []
    for h in session.get("history", [])[-6:]:
        history_lines.append(f"Customer: {h['customer']}")
        history_lines.append(f"Assistant: {h['atlyz']}")
    history_text = "\n".join(history_lines) if history_lines else "(new conversation)"

    # Smart email routing is specific to Atlyz's own site. Every other business gets a
    # generic, business-appropriate fallback — customers must never be sent to Atlyz inboxes.
    if bid == "atlyz_website":
        dont_know_section = (
            "WHEN YOU DON'T KNOW THE ANSWER:\n"
            "Never say connection issue, technical error, or flag to the team.\n"
            "Instead, based on what the question is about, direct them to the right email naturally and warmly:\n\n"
            "- Pricing, plans, payments, billing → billing@atlyz.com\n"
            "- Jobs, hiring, working at Atlyz → careers@atlyz.com\n"
            "- Setup, embed code, not working, technical help → support@atlyz.com\n"
            "- Anything else → contact@atlyz.com\n\n"
            "Example response when you don't know:\n"
            "'That one's a bit outside what I have on hand! For anything about pricing, "
            "billing@atlyz.com is your best bet — they'll sort you out quickly 😊'\n\n"
            "Vary these responses — never say the same thing twice."
        )
    else:
        dont_know_section = (
            "WHEN YOU DON'T KNOW THE ANSWER:\n"
            "Never say connection issue, technical error, or flag to the team.\n"
            f"Warmly let the customer know it's just outside what you have on hand, then point them to the "
            f"contact details for {business_name} shown in the knowledge base above so the team can follow up. "
            "Vary how you say this — never repeat the same wording twice."
        )

    system_prompt = f"""You are {bot_name} — warm, sharp, and genuinely helpful. You work for {business_name} and you care about giving customers exactly what they need.

PERSONALITY:
- Conversational and natural — like a smart friend, not a bot
- Vary your openings — never start two replies the same way
- Light humour when appropriate
- 1 emoji max per message, only when it feels natural
- Short answers for simple questions, detailed for complex ones

YOUR JOB:
- Answer exactly what was asked — nothing more, nothing less
- Never push products unless the customer asks about them
- If asked what YOU can do: say you can answer questions about this business, help find what they need, and point them to the right contact if needed

{dont_know_section}

KNOWLEDGE BASE:
{knowledge_block}

CONVERSATION HISTORY:
{history_text}

LEAD & FLOW RULES:
{lead_rule}
- If the customer says goodbye and is done: action = end.

{language_instruction}

Always respond with valid JSON: {{"reply": "...", "action": "chat", "language": "English"}}
action must be: chat, collect_lead, or end"""

    # Code-level safety net — used only if the model returns unusable output twice or
    # the API call errors. No "flag to team" phrasing; route to a contact instead.
    if bid == "atlyz_website":
        fallback_reply = ("Hmm, that one slipped right past me! Email contact@atlyz.com and the "
                          "team will take great care of you. 😊")
    else:
        fallback_reply = ("Sorry — I didn't quite catch that. Mind rephrasing? For anything specific, "
                          "the contact details on this site will reach the team.")
    fallback = {"reply": fallback_reply, "action": "chat", "language": "English"}

    def _parse_reply(text):
        text = (text or "").strip()
        candidates = [text, text.replace("```json", "").replace("```", "").strip()]
        for candidate in candidates:
            try:
                return json.loads(candidate)
            except Exception:
                pass
        match = re.search(r"\{.*\}", candidates[1], re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except Exception:
                pass
        return None

    try:
        from openai import OpenAI
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

        messages = [{"role": "system", "content": system_prompt}]
        for h in session.get("history", [])[-12:]:
            messages.append({"role": "user",      "content": h["customer"]})
            messages.append({"role": "assistant",  "content": h["atlyz"]})
        messages.append({"role": "user", "content": message})

        # Initial call plus one retry if the model returns malformed JSON.
        for attempt in range(2):
            response = client.chat.completions.create(
                model="gpt-5-nano",
                messages=messages,
                max_completion_tokens=1500,
                response_format={"type": "json_object"}
            )
            parsed = _parse_reply(response.choices[0].message.content)
            if parsed is not None:
                return parsed
            if attempt == 0:
                print("[CHAT AI] Malformed JSON — retrying once")

        return fallback

    except Exception as e:
        print(f"[CHAT AI ERROR] {e}")
        return fallback


# ═══════════════════════════════════════════════════════════════════════════════
# LEAD CAPTURE
# ═══════════════════════════════════════════════════════════════════════════════

def save_lead(bid: str, lead: dict):
    base = client_dir(bid)
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


def read_leads(bid: str) -> list:
    base = client_dir(bid)
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
    bid  = data.get("business_id", "")

    if not bid:
        return jsonify({"error": "business_id required"}), 400
    if not business_exists(bid):
        return jsonify({"error": "Unknown business"}), 404

    # Wait up to 10 s for the startup auto-scrape to finish.
    deadline = time.time() + 10
    while not _ais_ready and time.time() < deadline:
        time.sleep(0.5)

    config          = load_chatbot_config(bid)
    business_config = load_business_config(bid)
    config["business_name"] = business_config.get("business_name", bid.replace("_", " ").title())

    feats     = plan_features(bid)
    cap       = feats.get("monthly_chats")
    over_limit = cap is not None and monthly_chats_used(bid) >= cap

    greeting = config.get("greeting", "Hi! How can I help you today?")
    if over_limit:
        greeting = ("Thanks for stopping by! Our live chat has reached its limit for this month. "
                    "Please reach out through the contact details on this site and we'll get right back to you.")

    session_id = str(uuid.uuid4())
    sessions[session_id] = {
        "business_id":   bid,
        "history":       [],
        "lead_captured": False,
        "started_at":    datetime.now().isoformat(),
        "config":        config,
        "over_limit":    over_limit,
    }
    if not over_limit:
        bump_stats(bid, chats=1)

    white_label  = bool(config.get("white_label")) and feats.get("white_label", False)
    logo_fname, _ = logo_info(bid)

    return jsonify({
        "session_id":    session_id,
        "greeting":      greeting,
        "business_name": config["business_name"],
        "chat_enabled":  not over_limit,
        "config": {
            "primary_color":   config.get("primary_color", "#00C2FF"),
            "widget_position": config.get("widget_position", "bottom-right"),
            "bot_name":        config.get("bot_name", "Aria"),
            "bot_tagline":     config.get("bot_tagline", "Your AI Assistant"),
            "white_label":     white_label,
            "logo_url":        f"/chat/logo/{bid}" if logo_fname else None,
            "lead_capture":    feats.get("lead_capture", False),
        }
    })


@app.route("/chat/message", methods=["POST"])
def chat_message():
    data        = request.get_json(silent=True) or {}
    session_id  = data.get("session_id", "")
    bid         = data.get("business_id", "")
    message     = (data.get("message", "") or "").strip()

    if not session_id:
        return jsonify({"error": "Invalid session"}), 400
    if not message:
        return jsonify({"error": "Empty message"}), 400

    if len(message) > MAX_MESSAGE_CHARS:
        message = message[:MAX_MESSAGE_CHARS]

    session = get_or_load_session(session_id, bid)
    if not session:
        return jsonify({"error": "Invalid session"}), 400

    if (not check_rate_limit(rate_limits, session_id, RATE_LIMIT_MAX) or
            not check_rate_limit(ip_rate_limits, client_ip(), IP_RATE_LIMIT_MAX)):
        return jsonify({
            "reply":    "You're sending messages too fast. Please wait a moment.",
            "action":   "chat",
            "language": "English"
        }), 429

    bid    = session["business_id"]
    config = session["config"]

    _PAID_PLANS = {"starter", "growth", "pro"}
    if load_business_config(bid).get("plan", "").strip().lower() not in _PAID_PLANS:
        return jsonify({
            "reply":    "This chatbot is not active. The business owner needs to activate a plan.",
            "action":   "chat",
            "language": "English",
        }), 403

    if session.get("over_limit"):
        notice = ("Our live chat has hit its monthly limit. Please use the contact details on this "
                  "site to reach the team and they'll follow up with you shortly.")
        session["history"].append({"customer": message, "atlyz": notice, "ts": datetime.now().isoformat()})
        save_conversation(bid, session_id, session)
        return jsonify({"reply": notice, "action": "chat", "language": "English", "session_id": session_id})

    feats      = plan_features(bid)
    knowledge  = load_knowledge(bid)
    owner_info = load_owner_info(bid)

    result   = ai_chat_response(message, bid, session, knowledge, config,
                                owner_info=owner_info, lead_capture=feats.get("lead_capture", False))
    reply    = result.get("reply", "Sorry, I couldn't process that.")
    action   = result.get("action", "chat")
    language = result.get("language", "English")

    if action == "collect_lead" and not feats.get("lead_capture", False):
        action = "chat"

    session["history"].append({"customer": message, "atlyz": reply, "ts": datetime.now().isoformat()})
    session["last_language"] = language
    if len(session["history"]) > 50:
        session["history"] = session["history"][-50:]

    save_conversation(bid, session_id, session)
    bump_stats(bid, messages=1)

    return jsonify({"reply": reply, "action": action, "language": language, "session_id": session_id})


@app.route("/chat/lead", methods=["POST"])
def chat_lead():
    data       = request.get_json(silent=True) or {}
    session_id = data.get("session_id", "")
    bid        = data.get("business_id", "")

    session = get_or_load_session(session_id, bid)
    if not session:
        return jsonify({"error": "Invalid session"}), 400

    bid    = session["business_id"]
    config = session["config"]

    if not plan_features(bid).get("lead_capture", False):
        return jsonify({"error": "Lead capture is not available on this plan"}), 403

    lead = {
        "lead_id":    str(uuid.uuid4())[:8],
        "timestamp":  datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "name":       (data.get("name", "") or "")[:120],
        "email":      (data.get("email", "") or "")[:160],
        "phone":      (data.get("phone", "") or "")[:40],
        "question":   (data.get("question", "") or "")[:500],
        "session_id": session_id,
    }

    save_lead(bid, lead)
    session["lead_captured"] = True
    save_conversation(bid, session_id, session)

    owner_email   = load_business_config(bid).get("owner_email", "")
    business_name = config.get("business_name", bid)
    send_lead_email(owner_email, business_name, lead)

    return jsonify({"success": True, "message": "Thank you! The owner will be in touch with you shortly."})


@app.route("/chat/config/<bid>", methods=["GET"])
def get_chat_config(bid):
    if not business_exists(bid):
        return jsonify({"error": "Unknown business"}), 404
    config          = load_chatbot_config(bid)
    business_config = load_business_config(bid)
    config["business_name"] = business_config.get("business_name", bid.replace("_", " ").title())
    feats          = plan_features(bid)
    logo_fname, _  = logo_info(bid)
    config["plan"]         = business_plan(bid)
    config["features"]     = feats
    config["logo_url"]     = f"/chat/logo/{bid}" if logo_fname else None
    config["white_label"]  = bool(config.get("white_label")) and feats.get("white_label", False)
    config["website_url"]  = business_config.get("website", "")
    return jsonify(config)


@app.route("/widget.js")
def widget_js():
    return send_from_directory(os.path.join(BASE_DIR, "static"), "widget.js",
                               mimetype="application/javascript")


@app.route("/chat/logo/<bid>")
def chat_logo(bid):
    if not business_exists(bid):
        return jsonify({"error": "Unknown business"}), 404
    fname, mime = logo_info(bid)
    if not fname:
        return jsonify({"error": "No logo"}), 404
    return send_from_directory(os.path.join(client_dir(bid), "config"), fname, mimetype=mime)


@app.route("/chat/test/<bid>")
def test_page(bid):
    if not business_exists(bid):
        return jsonify({"error": "Unknown business"}), 404
    config          = load_chatbot_config(bid)
    business_config = load_business_config(bid)
    business_name   = business_config.get("business_name", bid.replace("_", " ").title())
    return render_template("chat_test.html", business_id=bid,
                           business_name=business_name, config=config)


@app.route("/dashboard")
@app.route("/dashboard/")
def dashboard_page():
    return render_template("dashboard.html")


# ═══════════════════════════════════════════════════════════════════════════════
# AUTH ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/auth/signup", methods=["POST"])
def auth_signup():
    data     = request.get_json(silent=True) or {}
    name     = (data.get("name", "") or "").strip()
    email    = (data.get("email", "") or "").strip().lower()
    password = data.get("password", "") or ""

    if not email or not EMAIL_RE.match(email):
        return jsonify({"success": False, "error": "Please enter a valid email address."}), 400
    if len(password) < 8:
        return jsonify({"success": False, "error": "Password must be at least 8 characters."}), 400

    accounts = load_accounts()
    if email in accounts:
        return jsonify({"success": False, "error": "An account with this email already exists — try logging in."}), 409

    accounts[email] = {
        "name":          name,
        "password_hash": generate_password_hash(password),
        "created_at":    datetime.now().isoformat(),
        "businesses":    [],
    }
    save_accounts(accounts)
    send_welcome_email(email, name)
    return jsonify({"success": True, "token": issue_token(email), "email": email, "name": name})


@app.route("/auth/login", methods=["POST"])
def auth_login():
    data     = request.get_json(silent=True) or {}
    email    = (data.get("email", "") or "").strip().lower()
    password = data.get("password", "") or ""

    account = load_accounts().get(email)
    if not account or not check_password_hash(account.get("password_hash", ""), password):
        return jsonify({"success": False, "error": "Incorrect email or password."}), 401

    businesses = [b for b in account.get("businesses", []) if business_exists(b)]
    return jsonify({
        "success":    True,
        "token":      issue_token(email),
        "email":      email,
        "name":       account.get("name", ""),
        "businesses": businesses,
    })


@app.route("/auth/me", methods=["GET"])
def auth_me():
    email = request_account_email()
    if not email:
        return jsonify({"success": False, "error": "Not signed in"}), 401
    account = load_accounts().get(email, {})
    businesses = [b for b in account.get("businesses", []) if business_exists(b)]
    return jsonify({
        "success":    True,
        "email":      email,
        "name":       account.get("name", ""),
        "businesses": businesses,
    })


# ═══════════════════════════════════════════════════════════════════════════════
# OWNER-SCOPED ROUTES (token or admin key required)
# ═══════════════════════════════════════════════════════════════════════════════

def _set_config_field(bid: str, key: str, value):
    cfg = load_chatbot_config(bid)
    cfg[key] = value
    with open(os.path.join(client_dir(bid), "config", "chatbot_config.json"), "w") as f:
        json.dump(cfg, f, indent=2)


@app.route("/chatbot/config/<bid>", methods=["POST"])
def save_chatbot_config(bid):
    if not business_exists(bid):
        return jsonify({"error": "Unknown business"}), 404
    if not is_owner_of(bid):
        return jsonify({"error": "Unauthorized"}), 401

    data    = request.get_json(silent=True) or {}
    current = load_chatbot_config(bid)
    for field in ["primary_color", "greeting", "widget_position", "business_name",
                  "language_lock", "bot_name", "bot_tagline", "color_mode", "logo_mode"]:
        if field in data:
            current[field] = data[field]

    if "white_label" in data:
        current["white_label"] = bool(data["white_label"]) and plan_features(bid).get("white_label", False)

    with open(os.path.join(client_dir(bid), "config", "chatbot_config.json"), "w") as f:
        json.dump(current, f, indent=2)
    return jsonify({"success": True})


@app.route("/chatbot/stats/<bid>", methods=["GET"])
def chatbot_stats(bid):
    if not business_exists(bid):
        return jsonify({"error": "Unknown business"}), 404
    if not is_owner_of(bid):
        return jsonify({"error": "Unauthorized"}), 401

    leads_count = len(read_leads(bid))
    stats       = read_stats(bid)
    scrape_meta = {}
    meta_path   = os.path.join(client_dir(bid), "config", "scrape_meta.json")
    if os.path.exists(meta_path):
        try:
            with open(meta_path) as f:
                scrape_meta = json.load(f)
        except Exception:
            pass

    active = [s for s in sessions.values() if s.get("business_id") == bid]
    plan   = business_plan(bid)
    feats  = plans.get_plan(plan)

    return jsonify({
        "business_id":      bid,
        "plan":             plan,
        "plan_label":       feats.get("label", plan.title()),
        "monthly_cap":      feats.get("monthly_chats"),
        "chats_this_month": monthly_chats_used(bid),
        "leads_total":      leads_count,
        "total_chats":      stats.get("total_chats", 0),
        "total_messages":   stats.get("total_messages", 0),
        "active_sessions":  len(active),
        "last_active":      stats.get("last_active", "never"),
        "last_scraped":     scrape_meta.get("timestamp", "never"),
        "pages_scraped":    scrape_meta.get("pages_scraped", 0),
    })


@app.route("/owner/leads/<bid>", methods=["GET"])
def owner_leads(bid):
    if not business_exists(bid):
        return jsonify({"error": "Unknown business"}), 404
    if not is_owner_of(bid):
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify({"business_id": bid, "leads": read_leads(bid)})


@app.route("/owner/conversations/<bid>", methods=["GET"])
def owner_conversations(bid):
    if not business_exists(bid):
        return jsonify({"error": "Unknown business"}), 404
    if not is_owner_of(bid):
        return jsonify({"error": "Unauthorized"}), 401

    conv_dir = os.path.join(client_dir(bid), "data", "conversations")
    items    = []
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
    return jsonify({"business_id": bid, "conversations": items})


@app.route("/owner/info/<bid>", methods=["GET", "POST"])
def owner_info_route(bid):
    if not business_exists(bid):
        return jsonify({"error": "Unknown business"}), 404
    if not is_owner_of(bid):
        return jsonify({"error": "Unauthorized"}), 401

    if request.method == "GET":
        return jsonify({"business_id": bid, "owner_info": load_owner_info(bid)})

    data = request.get_json(silent=True) or {}
    text = (data.get("owner_info", "") or "").strip()[:20000]
    with open(os.path.join(client_dir(bid), "config", "owner_info.txt"), "w", encoding="utf-8") as f:
        f.write(text)
    return jsonify({"success": True, "chars": len(text)})


@app.route("/owner/logo/<bid>", methods=["POST", "DELETE"])
def owner_logo(bid):
    if not business_exists(bid):
        return jsonify({"error": "Unknown business"}), 404
    if not is_owner_of(bid):
        return jsonify({"error": "Unauthorized"}), 401

    config_dir = os.path.join(client_dir(bid), "config")
    for ext in LOGO_EXTS:
        old = os.path.join(config_dir, f"logo.{ext}")
        if os.path.exists(old):
            os.remove(old)

    if request.method == "DELETE":
        _set_config_field(bid, "logo_mode", "default")
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
    _set_config_field(bid, "logo_mode", "custom")
    return jsonify({"success": True, "logo_url": f"/chat/logo/{bid}"})


# ═══════════════════════════════════════════════════════════════════════════════
# SCRAPING
# ═══════════════════════════════════════════════════════════════════════════════

def run_scrape(bid: str, website_url: str, force: bool = True) -> dict:
    try:
        from scraper import scrape_website, save_scraped_knowledge
        max_pages = plan_features(bid).get("scrape_pages", 50)
        result    = scrape_website(website_url, max_pages=max_pages)
        if result.get("status") != "ok":
            return result

        if not force:
            meta_path = os.path.join(client_dir(bid), "config", "scrape_meta.json")
            if os.path.exists(meta_path):
                try:
                    with open(meta_path) as f:
                        prev = json.load(f)
                    if prev.get("content_hash") and prev["content_hash"] == result.get("content_hash"):
                        return {"status": "skipped"}
                except Exception:
                    pass

        save_scraped_knowledge(bid, result, clients_dir=CLIENTS_DIR)
        knowledge_cache.pop(bid, None)

        cfg = load_chatbot_config(bid)
        if cfg.get("color_mode") == "auto" and result.get("brand_color"):
            _set_config_field(bid, "primary_color", result["brand_color"])

        return result
    except Exception as e:
        print(f"[SCRAPE ERROR] {bid}: {e}")
        return {"status": "error", "error": str(e)}


@app.route("/chat/scrape", methods=["POST"])
def scrape_endpoint():
    if not check_admin_key():
        return jsonify({"error": "Unauthorized"}), 401

    data        = request.get_json(silent=True) or {}
    bid         = data.get("business_id", "")
    website_url = data.get("url", "")

    if not bid or not website_url:
        return jsonify({"error": "business_id and url required"}), 400
    if not business_exists(bid):
        return jsonify({"error": "Unknown business"}), 404

    result = run_scrape(bid, website_url)
    if result.get("status") == "ok":
        return jsonify({
            "success":       True,
            "pages_scraped": result["pages_scraped"],
            "brand_color":   result.get("brand_color", ""),
            "preview":       result["content"][:300],
        })
    return jsonify({"success": False, "error": result.get("error", "Scrape failed")}), 400


@app.route("/chat/rescrape", methods=["POST"])
def rescrape_endpoint():
    data  = request.get_json(silent=True) or {}
    bid   = data.get("business_id", "")
    force = bool(data.get("force", False))

    if not business_exists(bid):
        return jsonify({"error": "Unknown business"}), 404
    if not is_owner_of(bid):
        return jsonify({"error": "Unauthorized"}), 401

    website_url = data.get("url", "") or load_business_config(bid).get("website", "")
    if not website_url:
        return jsonify({"error": "No website on file"}), 400

    result = run_scrape(bid, website_url, force=force)
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


# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/admin/knowledge", methods=["POST"])
def admin_knowledge():
    if not check_admin_key():
        return jsonify({"error": "Unauthorized"}), 401

    data           = request.get_json(silent=True) or {}
    bid            = (data.get("business_id", "") or "").strip()
    knowledge_text = data.get("knowledge_text", "") or ""

    if not bid:
        return jsonify({"error": "business_id required"}), 400
    if not knowledge_text.strip():
        return jsonify({"error": "knowledge_text required"}), 400
    if not business_exists(bid):
        return jsonify({"error": "Unknown business"}), 404

    knowledge_path = os.path.join(CLIENTS_DIR, bid, "config", "knowledge.txt")
    with open(knowledge_path, "a", encoding="utf-8") as f:
        f.write("\n" + knowledge_text)

    return jsonify({"success": True, "business_id": bid, "appended_chars": len(knowledge_text)})


# ═══════════════════════════════════════════════════════════════════════════════
# SETUP (owner or admin)
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/setup/create", methods=["POST"])
def setup_create():
    account_email = request_account_email()
    if not account_email and not check_admin_key():
        return jsonify({"success": False, "error": "Please sign in to create a business."}), 401

    data          = request.get_json(silent=True) or {}
    business_name = (data.get("business_name", "") or "").strip()
    website_url   = (data.get("website_url", "") or "").strip()
    owner_email   = (data.get("owner_email", "") or "").strip() or account_email or ""
    password      = data.get("password", "") or ""
    primary_color = data.get("primary_color", "#00C2FF")
    color_mode    = "auto" if data.get("color_mode") == "auto" else "manual"
    greeting      = (data.get("greeting", "") or "").strip()
    position      = data.get("widget_position", "bottom-right")
    plan          = plans.normalize_plan(data.get("plan", "starter"))
    owner_info    = (data.get("owner_info", "") or "").strip()[:20000]

    if not business_name or not website_url:
        return jsonify({"success": False, "error": "Business name and website URL are required"}), 400

    slug        = re.sub(r"[^a-z0-9]+", "-", business_name.lower()).strip("-")[:40] or "business"
    bid         = slug
    counter     = 2
    while os.path.exists(os.path.join(CLIENTS_DIR, bid)):
        bid     = f"{slug}-{counter}"
        counter += 1

    config_dir = os.path.join(CLIENTS_DIR, bid, "config")
    data_dir   = os.path.join(CLIENTS_DIR, bid, "data")
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

    # Link to account
    if account_email:
        accounts = load_accounts()
        acct     = accounts.get(account_email)
        if acct is not None:
            acct.setdefault("businesses", [])
            if bid not in acct["businesses"]:
                acct["businesses"].append(bid)
            save_accounts(accounts)

    result         = run_scrape(bid, website_url)
    pages_scraped  = result.get("pages_scraped", 0) if result.get("status") == "ok" else 0
    scrape_preview = result.get("content", "")[:200]  if result.get("status") == "ok" else ""

    return jsonify({
        "success":        True,
        "business_id":    bid,
        "plan":           plan,
        "pages_scraped":  pages_scraped,
        "scrape_ok":      pages_scraped > 0,
        "scrape_error":   result.get("error") if result.get("status") != "ok" else None,
        "brand_color":    result.get("brand_color", "") if result.get("status") == "ok" else "",
        "scrape_preview": scrape_preview,
        "embed_code":     f'<script src="SERVER_URL/widget.js?id={bid}"></script>',
    })


# ═══════════════════════════════════════════════════════════════════════════════
# CONTACT FORM + WEBSITE SERVING
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
        api_key = os.getenv("RESEND_API_KEY", "")
        if api_key:
            body = f"New contact form submission\n\nName: {name}\nEmail: {email}\nTopic: {topic}\n\nMessage:\n{message}"
            payload = {
                "from":     "Atlyz Contact <noreply@send.atlyz.com>",
                "to":       ["contact@atlyz.com"],
                "reply_to": email,
                "subject":  f"[Atlyz] Contact: {topic or 'General'} from {name or email}",
                "text":     body,
            }
            resp = requests.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=10,
            )
            if resp.status_code >= 300:
                print(f"[CONTACT] Email failed: Resend {resp.status_code}: {resp.text}")
    except Exception as e:
        print(f"[CONTACT] Email failed: {e}")

    return jsonify({"success": True})


@app.route("/site/")
@app.route("/site/<path:filename>")
def serve_atlyz_site(filename="index.html"):
    site_dir = os.path.join(REPO_ROOT, "ATLYZ website")
    return send_from_directory(site_dir, filename)


@app.route("/health")
def health():
    return jsonify({"status": "ok", "sessions": len(sessions), "ready": _ais_ready})


# ═══════════════════════════════════════════════════════════════════════════════
# ATLYZ WEBSITE AUTO-SCRAPE (startup background thread)
# Reads local ATLYZ website HTML files on startup and keeps AIS knowledge fresh.
# ═══════════════════════════════════════════════════════════════════════════════

def auto_scrape_atlyz_website():
    ATLYZ_BID   = "atlyz_website"
    ATLYZ_PAGES = [
        ("index.html",         "/"),
        ("chat-product.html",  "/chat-product"),
        ("voice-product.html", "/voice-product"),
        ("agent-product.html", "/agent-product"),
        ("about.html",         "/about"),
        ("contact.html",       "/contact"),
        ("blog.html",          "/blog"),
        ("privacy.html",       "/privacy"),
        ("terms.html",         "/terms"),
        ("careers.html",       "/careers"),
        ("cookies.html",       "/cookies"),
    ]

    def run():
        global _ais_ready
        try:
            knowledge_path = os.path.join(CLIENTS_DIR, ATLYZ_BID, "config", "knowledge.txt")

            # Skip if the knowledge base is manually maintained
            if os.path.exists(knowledge_path):
                with open(knowledge_path, encoding="utf-8") as f:
                    existing = f.read()
                if ("IDENTITY" in existing or "PRICING" in existing) and len(existing) > 500:
                    print("[AIS] Manually-curated knowledge found — skipping auto-scrape")
                    return

            website_dir = os.path.join(REPO_ROOT, "ATLYZ website")
            if not os.path.exists(website_dir):
                print("[AIS] ATLYZ website folder not found — skipping auto-scrape")
                return

            import re as _re

            def clean_html_local(html):
                html = _re.sub(r'<(script|style)[^>]*>.*?</(script|style)>', '', html,
                               flags=_re.DOTALL | _re.IGNORECASE)
                html = _re.sub(r'<[^>]+>', ' ', html)
                html = html.replace('&nbsp;', ' ').replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>').replace('&quot;', '"')
                return _re.sub(r'\s+', ' ', html).strip()

            pages_content = []
            for filename, path in ATLYZ_PAGES:
                filepath = os.path.join(website_dir, filename)
                if not os.path.exists(filepath):
                    continue
                with open(filepath, encoding="utf-8") as f:
                    html = f.read()
                text = clean_html_local(html)
                if text and len(text) > 100:
                    pages_content.append(f"=== Page: {path} ===\n{text[:3000]}\n")
                    print(f"[AIS] Read {filename} ({len(text):,} chars)")

            if not pages_content:
                print("[AIS] No HTML content found — skipping auto-scrape")
                return

            from openai import OpenAI
            client   = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
            combined = "\n".join(pages_content)

            prompt = f"""You are extracting business knowledge from the Atlyz website.

Scraped content from atlyz.com:
{combined[:10000]}

Extract and organize all useful information into clear sections:
- About Atlyz: mission, origin story, why it was built, company values
- Founder / ownership: who built Atlyz and their background
- Products: Atlyz Chat, Atlyz Voice, Atlyz Agent — features, status, how they work
- Pricing (all plans, founding member rates if mentioned)
- How to get started / onboarding steps
- Contact information (use support@atlyz.com for general/support, billing@atlyz.com for billing, careers@atlyz.com for jobs)
- Careers / open positions
- Privacy policy summary
- Terms of service summary
- FAQs

Write it clearly so AIS (the Atlyz AI assistant) can use it to answer ANY visitor question accurately.
Remove navigation menus, footers, cookie notices, and repetitive UI text.
Keep it factual and concise."""

            response  = client.chat.completions.create(
                model="gpt-5-nano",
                messages=[
                    {"role": "system", "content": "You extract and organize business information from website content."},
                    {"role": "user",   "content": prompt},
                ],
                max_completion_tokens=1800,
            )
            summarized = response.choices[0].message.content.strip()

            if len(summarized) < 300:
                print("[AIS] Summarization too short — keeping existing knowledge")
                return

            config_dir = os.path.join(CLIENTS_DIR, ATLYZ_BID, "config")
            os.makedirs(config_dir, exist_ok=True)

            # Preserve the manually curated IDENTITY header if present
            identity_block = ""
            if os.path.exists(knowledge_path):
                with open(knowledge_path, encoding="utf-8") as f:
                    existing = f.read()
                marker = "ABOUT ATLYZ\n==========="
                if marker in existing:
                    identity_block = existing[:existing.index(marker)].rstrip() + "\n\n"

            with open(knowledge_path, "w", encoding="utf-8") as f:
                f.write(identity_block)
                f.write(f"Source: https://atlyz.com (auto-scraped from local files)\n")
                f.write(f"Scraped: {len(pages_content)} pages\n\n")
                f.write(summarized)

            meta_path = os.path.join(config_dir, "scrape_meta.json")
            with open(meta_path, "w") as f:
                json.dump({
                    "url":           "https://atlyz.com",
                    "pages_scraped": len(pages_content),
                    "status":        "ok",
                    "timestamp":     datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "source":        "local_files",
                }, f, indent=2)

            knowledge_cache.pop(ATLYZ_BID, None)
            print(f"[AIS] Knowledge auto-updated from {len(pages_content)} pages ✓")

        except Exception as e:
            print(f"[AIS] Auto-scrape failed: {e}")
        finally:
            _ais_ready = True

    threading.Thread(target=run, daemon=True, name="ais-auto-scrape").start()


# Runs at import (works under both `python chatbot_server.py` and gunicorn).
auto_scrape_atlyz_website()


if __name__ == "__main__":
    print("=" * 50)
    print("  ATLYZ — Chat Server")
    print(f"  API:  http://localhost:{os.environ.get('PORT', 5002)}")
    print("  Test: /chat/test/quickfix_plumbing")
    print("=" * 50)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5002)), debug=DEV_MODE)
