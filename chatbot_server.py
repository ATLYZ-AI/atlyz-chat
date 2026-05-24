# chatbot_server.py — Atlyz Chat Server

import os
import json
import re
import uuid
import time
import secrets
import smtplib
import csv
from datetime import datetime
from email.mime.text import MIMEText
from dotenv import load_dotenv
from flask import Flask, request, jsonify, render_template, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "atlyz-chat-secret")

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

# ── In-memory stores ──────────────────────────────────────────────────────────
sessions       = {}  # session_id → session data
knowledge_cache = {} # business_id → knowledge text
rate_limits    = {}  # session_id → [timestamps]

RATE_LIMIT_MAX    = 20  # messages per window
RATE_LIMIT_WINDOW = 60  # seconds

# ── Accounts (owner signup / login) ─────────────────────────────────────────────
ACCOUNTS_DIR  = "accounts"
ACCOUNTS_FILE = os.path.join(ACCOUNTS_DIR, "accounts.json")
auth_tokens   = {}  # token → email (in-memory; cleared on server restart)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


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
    with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        json.dump(accounts, f, indent=2)


def issue_token(email: str) -> str:
    token = secrets.token_urlsafe(32)
    auth_tokens[token] = email
    return token


def email_from_token(token: str):
    return auth_tokens.get(token) if token else None


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def check_admin_key():
    """Return True if request carries a valid admin key, or if none is configured (dev)."""
    required = os.getenv("ATLYZ_ADMIN_KEY", "")
    if not required:
        return True  # dev mode — no key set, allow all
    provided = (
        request.headers.get("X-Admin-Key", "") or
        request.args.get("key", "") or
        (request.json or {}).get("admin_key", "")
    )
    return provided == required


def check_rate_limit(session_id: str) -> bool:
    """Return True if session is within rate limit."""
    now = time.time()
    timestamps = [t for t in rate_limits.get(session_id, []) if now - t < RATE_LIMIT_WINDOW]
    if len(timestamps) >= RATE_LIMIT_MAX:
        rate_limits[session_id] = timestamps
        return False
    timestamps.append(now)
    rate_limits[session_id] = timestamps
    return True


def send_lead_email(owner_email: str, business_name: str, lead: dict):
    """Email the owner when a new lead is captured."""
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
# KNOWLEDGE LOADER
# ═══════════════════════════════════════════════════════════════════════════════

def load_knowledge(business_id: str) -> str:
    if business_id in knowledge_cache:
        return knowledge_cache[business_id]

    config_dir = os.path.join("clients", business_id, "config")

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
    config_path = os.path.join("clients", business_id, "config", "business_config.txt")
    config = {}
    if os.path.exists(config_path):
        with open(config_path, encoding="utf-8") as f:
            for line in f:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    config[k.strip()] = v.strip()
    return config


def load_chatbot_config(business_id: str) -> dict:
    path = os.path.join("clients", business_id, "config", "chatbot_config.json")
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
        "white_label":     False
    }
    if os.path.exists(path):
        try:
            with open(path) as f:
                saved = json.load(f)
                defaults.update(saved)
        except Exception:
            pass
    return defaults


# ═══════════════════════════════════════════════════════════════════════════════
# CORE AI
# ═══════════════════════════════════════════════════════════════════════════════

def ai_chat_response(message: str, business_id: str, session: dict, knowledge: str, config: dict) -> dict:
    business_name = config.get("business_name", "the business")
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

    knowledge_section = knowledge if knowledge else "(No business information provided yet.)"

    system_prompt = f"""You are a sharp AI assistant for {business_name}. Always respond with valid JSON only.

OUTPUT FORMAT (required): {{"reply": "...", "action": "chat", "language": "English"}}
- "reply": your answer to the customer
- "action": one of chat | collect_lead | end  (always in English)
- "language": the English name of the language you are replying in

BUSINESS KNOWLEDGE:
{knowledge_section}

RULES:
- Answer from the knowledge above. If not in knowledge, say you don't have that info and give the contact if available.
- Never make up prices, hours, or policies.
- Keep replies to 1-3 sentences. Never greet with Hi/Hello.
- If customer asks to speak to someone or be contacted: action = collect_lead
- If customer says bye and is done: action = end

{language_instruction}"""

    try:
        from openai import OpenAI
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

        messages = [{"role": "system", "content": system_prompt}]
        for h in session.get("history", [])[-12:]:
            messages.append({"role": "user",      "content": h["customer"]})
            messages.append({"role": "assistant",  "content": h["atlyz"]})
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

        match = re.search(r'\{.*\}', cleaned, re.DOTALL)
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
    try:
        path = os.path.join("clients", business_id, "data", "leads.csv")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        write_header = not os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["lead_id", "timestamp", "name", "email", "phone", "question", "session_id"])
            if write_header:
                writer.writeheader()
            writer.writerow(lead)
    except Exception as e:
        print(f"[LEAD SAVE ERROR] {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# API ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/chat/start", methods=["POST"])
def chat_start():
    data = request.json or {}
    business_id = data.get("business_id", "")

    if not business_id:
        return jsonify({"error": "business_id required"}), 400

    session_id = str(uuid.uuid4())
    config = load_chatbot_config(business_id)
    business_config = load_business_config(business_id)
    config["business_name"] = business_config.get("business_name", business_id.replace("_", " ").title())

    sessions[session_id] = {
        "business_id": business_id,
        "history": [],
        "lead_captured": False,
        "started_at": datetime.now().isoformat(),
        "config": config
    }

    return jsonify({
        "session_id":    session_id,
        "greeting":      config.get("greeting", "Hi! How can I help you today?"),
        "business_name": config["business_name"],
        "config": {
            "primary_color":   config.get("primary_color", "#7c3aed"),
            "widget_position": config.get("widget_position", "bottom-right"),
            "bot_name":        config.get("bot_name", "Aria"),
            "bot_tagline":     config.get("bot_tagline", "Your AI Assistant"),
            "white_label":     config.get("white_label", False)
        }
    })


@app.route("/chat/message", methods=["POST"])
def chat_message():
    data = request.json or {}
    session_id = data.get("session_id", "")
    message    = data.get("message", "").strip()

    if not session_id or session_id not in sessions:
        return jsonify({"error": "Invalid session"}), 400

    if not message:
        return jsonify({"error": "Empty message"}), 400

    if not check_rate_limit(session_id):
        return jsonify({
            "reply": "You're sending messages too fast. Please wait a moment.",
            "action": "chat",
            "language": "English"
        }), 429

    session     = sessions[session_id]
    business_id = session["business_id"]
    config      = session["config"]
    knowledge   = load_knowledge(business_id)

    result   = ai_chat_response(message, business_id, session, knowledge, config)
    reply    = result.get("reply", "Sorry, I couldn't process that.")
    action   = result.get("action", "chat")
    language = result.get("language", "English")

    # Store plain reply text so the model sees natural conversation history (not raw JSON)
    session["history"].append({"customer": message, "atlyz": reply})
    session["last_language"] = language
    if len(session["history"]) > 20:
        session["history"].pop(0)

    return jsonify({"reply": reply, "action": action, "language": language, "session_id": session_id})


@app.route("/chat/lead", methods=["POST"])
def chat_lead():
    data = request.json or {}
    session_id = data.get("session_id", "")

    if not session_id or session_id not in sessions:
        return jsonify({"error": "Invalid session"}), 400

    session     = sessions[session_id]
    business_id = session["business_id"]
    config      = session["config"]

    lead = {
        "lead_id":    str(uuid.uuid4())[:8],
        "timestamp":  datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "name":       data.get("name", ""),
        "email":      data.get("email", ""),
        "phone":      data.get("phone", ""),
        "question":   data.get("question", ""),
        "session_id": session_id
    }

    save_lead(business_id, lead)
    session["lead_captured"] = True

    # Notify owner by email
    business_config = load_business_config(business_id)
    owner_email     = business_config.get("owner_email", "")
    business_name   = config.get("business_name", business_id)
    send_lead_email(owner_email, business_name, lead)

    return jsonify({"success": True, "message": "Thank you! The owner will be in touch with you shortly."})


@app.route("/chat/config/<business_id>", methods=["GET"])
def get_chat_config(business_id):
    config = load_chatbot_config(business_id)
    business_config = load_business_config(business_id)
    config["business_name"] = business_config.get("business_name", business_id.replace("_", " ").title())
    return jsonify(config)


@app.route("/chatbot/config/<business_id>", methods=["POST"])
def save_chatbot_config(business_id):
    if not check_admin_key():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.json or {}
    config_path = os.path.join("clients", business_id, "config", "chatbot_config.json")

    if not os.path.exists(os.path.dirname(config_path)):
        return jsonify({"success": False, "error": "Business not found"}), 404

    current = load_chatbot_config(business_id)
    for field in ["primary_color", "greeting", "widget_position", "business_name", "language_lock", "bot_name", "bot_tagline", "white_label"]:
        if field in data:
            current[field] = data[field]

    with open(config_path, "w") as f:
        json.dump(current, f, indent=2)

    return jsonify({"success": True})


@app.route("/chatbot/stats/<business_id>", methods=["GET"])
def chatbot_stats(business_id):
    leads_count = 0
    leads_path  = os.path.join("clients", business_id, "data", "leads.csv")
    if os.path.exists(leads_path):
        with open(leads_path, encoding="utf-8") as f:
            leads_count = max(0, sum(1 for _ in f) - 1)  # subtract header row

    active = [s for s in sessions.values() if s.get("business_id") == business_id]
    total_messages = sum(len(s.get("history", [])) for s in active)

    scrape_meta = {}
    meta_path = os.path.join("clients", business_id, "config", "scrape_meta.json")
    if os.path.exists(meta_path):
        try:
            with open(meta_path) as f:
                scrape_meta = json.load(f)
        except Exception:
            pass

    return jsonify({
        "business_id":      business_id,
        "leads_total":      leads_count,
        "active_sessions":  len(active),
        "total_messages":   total_messages,
        "last_scraped":     scrape_meta.get("timestamp", "never"),
        "pages_scraped":    scrape_meta.get("pages_scraped", 0)
    })


@app.route("/widget.js")
def widget_js():
    return send_from_directory("static", "widget.js", mimetype="application/javascript")


@app.route("/chat/scrape", methods=["POST"])
def scrape_endpoint():
    if not check_admin_key():
        return jsonify({"error": "Unauthorized"}), 401

    data        = request.json or {}
    business_id = data.get("business_id", "")
    website_url = data.get("url", "")

    if not business_id or not website_url:
        return jsonify({"error": "business_id and url required"}), 400

    try:
        from scraper import scrape_website, save_scraped_knowledge
        result = scrape_website(website_url)
        if result["status"] == "ok":
            save_scraped_knowledge(business_id, result)
            knowledge_cache.pop(business_id, None)
            return jsonify({
                "success":       True,
                "pages_scraped": result["pages_scraped"],
                "preview":       result["content"][:300]
            })
        else:
            return jsonify({"success": False, "error": result["error"]}), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/health")
def health():
    return jsonify({"status": "ok", "sessions": len(sessions)})


@app.route("/auth/signup", methods=["POST"])
def auth_signup():
    data     = request.json or {}
    name     = (data.get("name") or "").strip()
    email    = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

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
        "businesses":    []
    }
    save_accounts(accounts)

    return jsonify({"success": True, "token": issue_token(email), "email": email, "name": name})


@app.route("/auth/login", methods=["POST"])
def auth_login():
    data     = request.json or {}
    email    = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    account = load_accounts().get(email)
    if not account or not check_password_hash(account.get("password_hash", ""), password):
        return jsonify({"success": False, "error": "Incorrect email or password."}), 401

    return jsonify({"success": True, "token": issue_token(email), "email": email, "name": account.get("name", "")})


@app.route("/auth/me", methods=["GET"])
def auth_me():
    email = email_from_token(request.args.get("token", "") or request.headers.get("X-Auth-Token", ""))
    if not email:
        return jsonify({"success": False, "error": "Not signed in"}), 401
    account = load_accounts().get(email, {})
    return jsonify({"success": True, "email": email, "name": account.get("name", ""), "businesses": account.get("businesses", [])})


@app.route("/setup/create", methods=["POST"])
def setup_create():
    if not check_admin_key():
        return jsonify({"error": "Unauthorized"}), 401

    data          = request.json or {}
    business_name = data.get("business_name", "").strip()
    website_url   = data.get("website_url", "").strip()
    owner_email   = data.get("owner_email", "").strip()
    account_token = data.get("account_token", "")
    account_email = email_from_token(account_token)
    if account_email and not owner_email:
        owner_email = account_email
    primary_color = data.get("primary_color", "#7c3aed")
    greeting      = data.get("greeting", "")
    position      = data.get("widget_position", "bottom-right")
    plan          = data.get("plan", "starter")

    if not business_name or not website_url:
        return jsonify({"success": False, "error": "Business name and website URL are required"}), 400

    slug        = re.sub(r"[^a-z0-9]+", "-", business_name.lower()).strip("-")
    slug        = slug[:40] or "business"
    business_id = slug
    counter     = 2
    while os.path.exists(os.path.join("clients", business_id)):
        business_id = f"{slug}-{counter}"
        counter += 1

    config_dir = os.path.join("clients", business_id, "config")
    data_dir   = os.path.join("clients", business_id, "data")
    os.makedirs(config_dir, exist_ok=True)
    os.makedirs(data_dir,   exist_ok=True)

    with open(os.path.join(config_dir, "business_config.txt"), "w") as f:
        f.write(f"business_name = {business_name}\n")
        f.write(f"owner_email = {owner_email}\n")
        f.write(f"website = {website_url}\n")
        f.write(f"plan = {plan}\n")

    if not greeting:
        greeting = f"Hi! I'm the virtual assistant for {business_name}. How can I help you today?"

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
        "white_label":     False
    }
    with open(os.path.join(config_dir, "chatbot_config.json"), "w") as f:
        json.dump(chatbot_cfg, f, indent=2)

    # Link this chatbot to the owner's account
    if account_email:
        accounts = load_accounts()
        acct = accounts.get(account_email)
        if acct is not None:
            acct.setdefault("businesses", [])
            if business_id not in acct["businesses"]:
                acct["businesses"].append(business_id)
            save_accounts(accounts)

    pages_scraped  = 0
    scrape_preview = ""
    try:
        from scraper import scrape_website, save_scraped_knowledge
        result = scrape_website(website_url)
        if result["status"] == "ok":
            save_scraped_knowledge(business_id, result)
            knowledge_cache.pop(business_id, None)
            pages_scraped  = result["pages_scraped"]
            scrape_preview = result["content"][:200]
    except Exception as e:
        print(f"[SETUP] Scrape failed for {business_id}: {e}")

    return jsonify({
        "success":       True,
        "business_id":   business_id,
        "pages_scraped": pages_scraped,
        "scrape_preview": scrape_preview,
        "embed_code":    f'<script src="SERVER_URL/widget.js?id={business_id}"></script>'
    })


@app.route("/contact", methods=["POST"])
def contact_form():
    data    = request.json or {}
    name    = data.get("name", "").strip()
    email   = data.get("email", "").strip()
    topic   = data.get("topic", "").strip()
    message = data.get("message", "").strip()

    if not email or not message:
        return jsonify({"error": "email and message required"}), 400

    print(f"[CONTACT] From: {name} <{email}> | Topic: {topic}")
    print(f"[CONTACT] Message: {message[:200]}")

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


@app.route("/contact", methods=["OPTIONS"])
def contact_options():
    response = jsonify({})
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "POST"
    return response


@app.route("/site/")
@app.route("/site/<path:filename>")
def serve_atlyz_site(filename="index.html"):
    site_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ATLYZ website")
    return send_from_directory(site_dir, filename)


@app.route("/chat/test/<business_id>")
def test_page(business_id):
    config          = load_chatbot_config(business_id)
    business_config = load_business_config(business_id)
    business_name   = business_config.get("business_name", business_id.replace("_", " ").title())
    return render_template("chat_test.html",
                           business_id=business_id,
                           business_name=business_name,
                           config=config)


# ═══════════════════════════════════════════════════════════════════════════════
# ATLYZ WEBSITE AUTO-SCRAPE
# Reads local ATLYZ website HTML files on startup and keeps AIS knowledge fresh.
# Runs in a background thread — never blocks the server.
# ═══════════════════════════════════════════════════════════════════════════════

def auto_scrape_atlyz_website():
    import threading

    ATLYZ_BUSINESS_ID = "atlyz_website"
    ATLYZ_PAGES = [
        ("index.html",          "/"),
        ("chat-product.html",   "/chat-product"),
        ("voice-product.html",  "/voice-product"),
        ("agent-product.html",  "/agent-product"),
        ("about.html",          "/about"),
        ("contact.html",        "/contact"),
        ("blog.html",           "/blog"),
        ("privacy.html",        "/privacy"),
        ("terms.html",          "/terms"),
        ("careers.html",        "/careers"),
        ("cookies.html",        "/cookies"),
    ]

    def run():
        try:
            knowledge_path = os.path.join("clients", ATLYZ_BUSINESS_ID, "config", "knowledge.txt")

            # Skip if the knowledge base is manually maintained (has IDENTITY or PRICING section)
            if os.path.exists(knowledge_path):
                with open(knowledge_path, encoding="utf-8") as f:
                    existing = f.read()
                if ("IDENTITY" in existing or "PRICING" in existing) and len(existing) > 500:
                    print("[AIS] Manually-curated knowledge found — skipping auto-scrape")
                    return

            meta_path = os.path.join("clients", ATLYZ_BUSINESS_ID, "config", "scrape_meta.json")

            website_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ATLYZ website")
            if not os.path.exists(website_dir):
                print("[AIS] ATLYZ website folder not found — skipping auto-scrape")
                return

            import re as _re

            def clean_html_local(html):
                html = _re.sub(r'<(script|style)[^>]*>.*?</(script|style)>', '', html, flags=_re.DOTALL | _re.IGNORECASE)
                html = _re.sub(r'<[^>]+>', ' ', html)
                html = html.replace('&nbsp;', ' ').replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>').replace('&quot;', '"')
                html = _re.sub(r'\s+', ' ', html).strip()
                return html

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
            client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
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
- Privacy policy summary (plain English points)
- Terms of service summary (key points)
- FAQs
- Any current offers or promotions

Write it clearly so AIS (the Atlyz AI assistant) can use it to answer ANY visitor question accurately.
Remove navigation menus, footers, cookie notices, and repetitive UI text.
Keep it factual and concise."""

            response = client.chat.completions.create(
                model="gpt-5-nano",
                messages=[
                    {"role": "system", "content": "You extract and organize business information from website content."},
                    {"role": "user", "content": prompt}
                ],
                max_completion_tokens=1800
            )
            summarized = response.choices[0].message.content.strip()

            if len(summarized) < 300:
                print("[AIS] Summarization too short — keeping existing knowledge")
                return

            config_dir = os.path.join("clients", ATLYZ_BUSINESS_ID, "config")
            os.makedirs(config_dir, exist_ok=True)

            knowledge_path = os.path.join(config_dir, "knowledge.txt")

            # Preserve the IDENTITY / ABOUT AIS header block (manually curated)
            identity_block = ""
            if os.path.exists(knowledge_path):
                with open(knowledge_path, encoding="utf-8") as f:
                    existing = f.read()
                # Keep everything up to and including "ABOUT AIS (YOURSELF)" section
                marker = "ABOUT ATLYZ\n==========="
                if marker in existing:
                    identity_block = existing[:existing.index(marker)].rstrip() + "\n\n"

            with open(knowledge_path, "w", encoding="utf-8") as f:
                f.write(identity_block)
                f.write(f"Source: https://atlyz.com (auto-scraped from local files)\n")
                f.write(f"Scraped: {len(pages_content)} pages\n\n")
                f.write(summarized)

            with open(meta_path, "w") as f:
                json.dump({
                    "url": "https://atlyz.com",
                    "pages_scraped": len(pages_content),
                    "status": "ok",
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "source": "local_files"
                }, f, indent=2)

            knowledge_cache.pop(ATLYZ_BUSINESS_ID, None)
            print(f"[AIS] Knowledge auto-updated from {len(pages_content)} pages ✓")

        except Exception as e:
            print(f"[AIS] Auto-scrape failed: {e}")

    t = threading.Thread(target=run, daemon=True, name="ais-auto-scrape")
    t.start()


if __name__ == "__main__":
    print("=" * 50)
    print("  ATLYZ — Chat Server")
    print("  API:  http://localhost:5002")
    print("  Test: http://localhost:5002/chat/test/test_shop")
    print("=" * 50)
    auto_scrape_atlyz_website()
    debug = os.getenv("DEV_MODE", "false").lower() == "true"
    app.run(host="0.0.0.0", port=int(os.environ.get('PORT', 5002)), debug=debug)
