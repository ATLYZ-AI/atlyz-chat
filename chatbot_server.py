# chatbot_server.py — Atlyz Chat Server
# Handles chat API requests from embedded widget
# Each business gets a unique ID — widget loads their knowledge

import os
import json
import re
import uuid
import time
from datetime import datetime
from dotenv import load_dotenv
from flask import Flask, request, jsonify, render_template, send_from_directory
from flask_socketio import SocketIO

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "atlyz-chat-secret")
socketio = SocketIO(app, cors_allowed_origins="*")

@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response

@app.before_request
def handle_options():
    if request.method == "OPTIONS":
        resp = app.make_default_options_response()
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        return resp

# ── In-memory session store ──────────────────────────────────────────────────
# Each session = one customer conversation
# { session_id: { history, business_id, lead_captured } }
sessions = {}

# ── Knowledge cache ──────────────────────────────────────────────────────────
# Avoid re-reading files on every message
knowledge_cache = {}


# ═══════════════════════════════════════════════════════════════════════════════
# KNOWLEDGE LOADER
# ═══════════════════════════════════════════════════════════════════════════════
def load_knowledge(business_id: str) -> str:
    """Load business knowledge from file. Cached after first load."""
    if business_id in knowledge_cache:
        return knowledge_cache[business_id]

    config_dir = os.path.join("clients", business_id, "config")

    # Try knowledge.txt first
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
    """Load business config — name, colors, widget settings."""
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
    """Load chatbot-specific settings — colors, icon, language."""
    path = os.path.join("clients", business_id, "config", "chatbot_config.json")
    defaults = {
        "primary_color": "#7c3aed",
        "secondary_color": "#f3f4f6",
        "icon": "default",
        "greeting": "Hi! How can I help you today?",
        "language_lock": None,  # None = auto-detect
        "business_name": "Business",
        "collect_leads": True,
        "widget_position": "bottom-right"
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
# CORE AI — single GPT call handles everything
# ═══════════════════════════════════════════════════════════════════════════════
def ai_chat_response(
    message: str,
    business_id: str,
    session: dict,
    knowledge: str,
    config: dict
) -> dict:
    """
    Single GPT call that:
    - Detects language and responds in it
    - Answers from knowledge base
    - Handles off-topic, rude, confused messages
    - Detects when customer wants to leave contact
    - Detects repeat questions
    Returns: { "reply": str, "action": "chat"|"collect_lead"|"end", "language": str }
    """
    business_name = config.get("business_name", "the business")
    language_lock = config.get("language_lock")

    # Build history for context
    history_text = ""
    if session.get("history"):
        recent = session["history"][-6:]
        history_text = "\nConversation so far:\n" + "\n".join(
            f"  Customer: {h['customer']}\n  Atlyz: {h['atlyz']}" for h in recent
        )

    knowledge_section = f"\n\nBusiness Knowledge:\n{knowledge}" if knowledge else "\n\n(No business information available yet.)"

    language_instruction = (
        f"Always respond in {language_lock} only."
        if language_lock
        else "Detect the customer's language and respond in the same language. If unclear, use English."
    )

    try:
        from openai import OpenAI
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

        prompt = f"""You are Atlyz, a smart and friendly AI assistant for {business_name}.
You work like ChatGPT or Gemini but you only know about this specific business.

Customer message: "{message}"{history_text}{knowledge_section}

HOW TO BEHAVE:
- Sound natural, warm and conversational — like a real human assistant
- Keep replies concise — 1-3 sentences usually enough
- {language_instruction}

ANSWERING RULES:
- Answer directly and confidently from the business knowledge
- If something is clearly in the knowledge, state it as fact: "Our return policy is 30 days" not "I think it might be..."
- If something is NOT in the knowledge, say honestly: "I don't have that info, but you can reach the team at [contact if available]"
- NEVER make up prices, products, or policies
- NEVER offer to collect contact details unless customer explicitly asks to be contacted
- If customer asks "can I speak to someone" or "contact owner" THEN set action to "collect_lead"
- If customer says bye/goodbye/thanks that's it: set action to "end"
- Handle rude messages calmly and professionally
- For greetings, respond warmly and ask how you can help

Respond in EXACTLY this JSON (no markdown, no extra text):
{{
  "reply": "your natural response here",
  "action": "chat or collect_lead or end",
  "language": "English or detected language"
}}"""

        response = client.chat.completions.create(
            model="gpt-4.1-nano",
            messages=[
                {"role": "system", "content": "You are a JSON-only API. Return valid JSON and nothing else."},
                {"role": "user", "content": prompt}
            ],
            max_completion_tokens=300
        )

        raw = response.choices[0].message.content.strip()

        # Robust JSON extraction
        try:
            result = json.loads(raw)
            return result
        except Exception:
            pass

        cleaned = raw.replace("```json", "").replace("```", "").strip()
        try:
            result = json.loads(cleaned)
            return result
        except Exception:
            pass

        match = re.search(r'\{.*\}', cleaned, re.DOTALL)
        if match:
            try:
                result = json.loads(match.group())
                return result
            except Exception:
                pass

        # Fallback
        return {
            "reply": "I'm having trouble right now. Please try again in a moment.",
            "action": "chat",
            "language": "English"
        }

    except Exception as e:
        print(f"[CHAT AI ERROR] {e}")
        return {
            "reply": "Sorry, I'm having a technical issue. Please try again shortly.",
            "action": "chat",
            "language": "English"
        }


# ═══════════════════════════════════════════════════════════════════════════════
# LEAD CAPTURE
# ═══════════════════════════════════════════════════════════════════════════════
def save_lead(business_id: str, lead: dict):
    """Save customer lead to CSV."""
    try:
        path = os.path.join("clients", business_id, "data", "leads.csv")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        import csv
        write_header = not os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["lead_id", "timestamp", "name", "email", "phone", "question", "session_id"])
            if write_header:
                writer.writeheader()
            writer.writerow(lead)
    except Exception as e:
        print(f"[LEAD SAVE ERROR] {e}")


def notify_lead(business_id: str, lead: dict, config: dict):
    """Notify owner of new lead via email."""
    try:
        from notifications.email import send_email_alert
        from notifications.notification import notify_owner
        business_name = config.get("business_name", business_id)
        call_data = {
            "call_id": lead["lead_id"],
            "name": lead.get("name", ""),
            "phone": lead.get("phone", ""),
            "email": lead.get("email", ""),
            "service": f"Chat inquiry: {lead.get('question', '')}",
            "preferred_time": "ASAP",
            "urgent": "NO",
            "summary": f"Customer {lead.get('name', 'Unknown')} contacted via chat widget.",
            "timestamp": lead["timestamp"],
            "status": "NEW",
            "address": ""
        }
        notify_owner(business_id=business_id, call=call_data)
    except Exception as e:
        print(f"[LEAD NOTIFY ERROR] {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# API ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/chat/start", methods=["POST"])
def chat_start():
    """Initialize a new chat session."""
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
        "session_id": session_id,
        "greeting": config.get("greeting", "Hi! How can I help you today?"),
        "business_name": config["business_name"],
        "config": {
            "primary_color": config.get("primary_color", "#7c3aed"),
            "widget_position": config.get("widget_position", "bottom-right")
        }
    })


@app.route("/chat/message", methods=["POST"])
def chat_message():
    """Handle incoming customer message."""
    data = request.json or {}
    session_id = data.get("session_id", "")
    message = data.get("message", "").strip()

    if not session_id or session_id not in sessions:
        return jsonify({"error": "Invalid session"}), 400

    if not message:
        return jsonify({"error": "Empty message"}), 400

    session = sessions[session_id]
    business_id = session["business_id"]
    config = session["config"]
    knowledge = load_knowledge(business_id)

    # Get AI response
    result = ai_chat_response(message, business_id, session, knowledge, config)

    reply = result.get("reply", "Sorry, I couldn't process that.")
    action = result.get("action", "chat")
    language = result.get("language", "English")

    # Update history
    session["history"].append({"customer": message, "atlyz": reply})
    if len(session["history"]) > 20:
        session["history"].pop(0)

    return jsonify({
        "reply": reply,
        "action": action,
        "language": language,
        "session_id": session_id
    })


@app.route("/chat/lead", methods=["POST"])
def chat_lead():
    """Save customer lead when they leave contact details."""
    data = request.json or {}
    session_id = data.get("session_id", "")

    if not session_id or session_id not in sessions:
        return jsonify({"error": "Invalid session"}), 400

    session = sessions[session_id]
    business_id = session["business_id"]
    config = session["config"]

    lead = {
        "lead_id": str(uuid.uuid4())[:8],
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "name": data.get("name", ""),
        "email": data.get("email", ""),
        "phone": data.get("phone", ""),
        "question": data.get("question", ""),
        "session_id": session_id
    }

    save_lead(business_id, lead)
    notify_lead(business_id, lead, config)
    session["lead_captured"] = True

    return jsonify({
        "success": True,
        "message": "Thank you! The owner will be in touch with you shortly."
    })


@app.route("/chat/config/<business_id>", methods=["GET"])
def get_chat_config(business_id):
    """Return chatbot config for widget initialization."""
    config = load_chatbot_config(business_id)
    business_config = load_business_config(business_id)
    config["business_name"] = business_config.get("business_name", business_id.replace("_", " ").title())
    return jsonify(config)


@app.route("/chatbot/config/<business_id>", methods=["POST"])
def save_chatbot_config(business_id):
    """Save updated widget config — color, greeting, position."""
    data = request.json or {}
    config_path = os.path.join("clients", business_id, "config", "chatbot_config.json")

    if not os.path.exists(os.path.dirname(config_path)):
        return jsonify({"success": False, "error": "Business not found"}), 404

    current = load_chatbot_config(business_id)
    if "primary_color" in data:
        current["primary_color"] = data["primary_color"]
    if "greeting" in data:
        current["greeting"] = data["greeting"]
    if "widget_position" in data:
        current["widget_position"] = data["widget_position"]
    if "business_name" in data:
        current["business_name"] = data["business_name"]

    with open(config_path, "w") as f:
        json.dump(current, f, indent=2)

    return jsonify({"success": True})


@app.route("/widget.js")
def widget_js():
    """Serve the embeddable widget script."""
    return send_from_directory("static", "widget.js",
                               mimetype="application/javascript")


@app.route("/chat/scrape", methods=["POST"])
def scrape_endpoint():
    """Trigger website scrape for a business."""
    data = request.json or {}
    business_id = data.get("business_id", "")
    website_url = data.get("url", "")

    if not business_id or not website_url:
        return jsonify({"error": "business_id and url required"}), 400

    try:
        from scraper import scrape_website, save_scraped_knowledge
        result = scrape_website(website_url)
        if result["status"] == "ok":
            save_scraped_knowledge(business_id, result)
            # Clear cache so next message uses new knowledge
            knowledge_cache.pop(business_id, None)
            return jsonify({
                "success": True,
                "pages_scraped": result["pages_scraped"],
                "preview": result["content"][:300]
            })
        else:
            return jsonify({"success": False, "error": result["error"]}), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/health")
def health():
    return jsonify({"status": "ok", "sessions": len(sessions)})


@app.route("/setup/create", methods=["POST"])
def setup_create():
    """Onboarding: create a new business, scrape website, save config."""
    data = request.json or {}
    business_name = data.get("business_name", "").strip()
    website_url   = data.get("website_url", "").strip()
    primary_color = data.get("primary_color", "#7c3aed")
    greeting      = data.get("greeting", "")
    position      = data.get("widget_position", "bottom-right")
    plan          = data.get("plan", "starter")

    if not business_name or not website_url:
        return jsonify({"success": False, "error": "Business name and website URL are required"}), 400

    # Generate slug business_id from business name
    import re as _re
    slug = _re.sub(r"[^a-z0-9]+", "-", business_name.lower()).strip("-")
    slug = slug[:40] or "business"

    # Avoid collisions — append suffix if directory exists
    business_id = slug
    counter = 2
    while os.path.exists(os.path.join("clients", business_id)):
        business_id = f"{slug}-{counter}"
        counter += 1

    # Create directory structure
    config_dir = os.path.join("clients", business_id, "config")
    data_dir   = os.path.join("clients", business_id, "data")
    os.makedirs(config_dir, exist_ok=True)
    os.makedirs(data_dir, exist_ok=True)

    # Save business_config.txt
    with open(os.path.join(config_dir, "business_config.txt"), "w") as f:
        f.write(f"business_name = {business_name}\n")
        f.write(f"owner_email = \n")
        f.write(f"website = {website_url}\n")
        f.write(f"plan = {plan}\n")

    # Build greeting if not provided
    if not greeting:
        greeting = f"Hi! I'm the virtual assistant for {business_name}. How can I help you today?"

    # Save chatbot_config.json
    chatbot_cfg = {
        "primary_color": primary_color,
        "secondary_color": "#f3f4f6",
        "icon": "default",
        "greeting": greeting,
        "language_lock": None,
        "business_name": business_name,
        "collect_leads": True,
        "widget_position": position
    }
    with open(os.path.join(config_dir, "chatbot_config.json"), "w") as f:
        json.dump(chatbot_cfg, f, indent=2)

    # Scrape the website
    pages_scraped = 0
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
        "success": True,
        "business_id": business_id,
        "pages_scraped": pages_scraped,
        "scrape_preview": scrape_preview,
        "embed_code": f'<script src="SERVER_URL/widget.js?id={business_id}"></script>'
    })


@app.route("/contact", methods=["POST"])
def contact_form():
    """Handle contact form submissions from the Atlyz website."""
    data = request.json or {}
    name    = data.get("name", "").strip()
    email   = data.get("email", "").strip()
    topic   = data.get("topic", "").strip()
    message = data.get("message", "").strip()

    if not email or not message:
        return jsonify({"error": "email and message required"}), 400

    # Log to console always
    print(f"[CONTACT] From: {name} <{email}> | Topic: {topic}")
    print(f"[CONTACT] Message: {message[:200]}")

    # Send email notification if configured
    try:
        import smtplib
        from email.mime.text import MIMEText
        from_addr = os.getenv("EMAIL_FROM")
        password  = os.getenv("EMAIL_PASSWORD")
        to_addr   = os.getenv("EMAIL_FROM")  # send to yourself
        if from_addr and password:
            body = f"New contact form submission\n\nName: {name}\nEmail: {email}\nTopic: {topic}\n\nMessage:\n{message}"
            msg = MIMEText(body)
            msg["Subject"] = f"[Atlyz] Contact: {topic or 'General'} from {name or email}"
            msg["From"]    = from_addr
            msg["To"]      = to_addr
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(from_addr, password)
                server.sendmail(from_addr, to_addr, msg.as_string())
            print(f"[CONTACT] Email sent to {to_addr}")
    except Exception as e:
        print(f"[CONTACT] Email failed (still logged above): {e}")

    response = jsonify({"success": True})
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


@app.route("/contact", methods=["OPTIONS"])
def contact_options():
    """CORS preflight for contact form."""
    response = jsonify({})
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "POST"
    return response


@app.route("/chat/test/<business_id>")
def test_page(business_id):
    """Test chat page — standalone without embedding."""
    config = load_chatbot_config(business_id)
    business_config = load_business_config(business_id)
    business_name = business_config.get("business_name", business_id.replace("_", " ").title())
    return render_template("chat_test.html",
                           business_id=business_id,
                           business_name=business_name,
                           config=config)


if __name__ == "__main__":
    print("=" * 50)
    print("  ATLYZ — Chat Server")
    print("  API:  http://localhost:5002")
    print("  Test: http://localhost:5002/chat/test/zahir_plumbers")
    print("=" * 50)
    socketio.run(app, host="0.0.0.0", port=5002, debug=True, allow_unsafe_werkzeug=True)
