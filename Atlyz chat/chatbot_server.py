# chatbot_server.py — Atlyz Chat Server

import os
import json
import re
import uuid
import time
import csv
import hmac
import hashlib
import secrets
import threading
from datetime import datetime, timedelta
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
MAX_USER_MSG_CHARS  = int(os.getenv("MAX_USER_MSG_CHARS", 1000))  # hard cap: longer chat messages are rejected before the LLM call
MAX_KNOWLEDGE_CHARS = int(os.getenv("MAX_KNOWLEDGE_CHARS", 12000))
MAX_CANDIDATE_SECTIONS = int(os.getenv("MAX_CANDIDATE_SECTIONS", 10))  # stage-1 keyword pre-filter width
RATE_LIMIT_MAX      = int(os.getenv("RATE_LIMIT_MAX", 20))
RATE_LIMIT_WINDOW   = int(os.getenv("RATE_LIMIT_WINDOW", 60))
IP_RATE_LIMIT_MAX   = int(os.getenv("IP_RATE_LIMIT_MAX", 40))
AUTH_RATE_LIMIT_MAX = int(os.getenv("AUTH_RATE_LIMIT_MAX", 10))

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
knowledge_cache = {}   # business_id → knowledge text (flat summary fallback)
sections_cache  = {}   # business_id → flattened knowledge_sections.json (hybrid select)
rate_limits     = {}   # session_id → [timestamps]
ip_rate_limits  = {}   # ip → [timestamps]
auth_rate_limits = {}  # ip → [timestamps]  (login/signup/password brute-force guard)
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


def _provided_admin_key() -> str:
    return (
        request.headers.get("X-Admin-Key", "") or
        request.args.get("key", "") or
        (request.get_json(silent=True) or {}).get("admin_key", "")
    )


def admin_key_matches() -> bool:
    """Constant-time check of the request's admin key against ATLYZ_ADMIN_KEY."""
    required = os.getenv("ATLYZ_ADMIN_KEY", "")
    if not required:
        return False
    provided = _provided_admin_key()
    return bool(provided) and hmac.compare_digest(provided, required)


def check_admin_key() -> bool:
    required = os.getenv("ATLYZ_ADMIN_KEY", "")
    if not required:
        return DEV_MODE
    return admin_key_matches()


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


def auth_rate_limited() -> bool:
    """True if this IP has exceeded the auth attempt budget (brute-force guard)."""
    return not check_rate_limit(auth_rate_limits, client_ip(), AUTH_RATE_LIMIT_MAX)


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
    if admin_key_matches():
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
            f"Name:        {lead.get('name') or 'Not provided'}\n"
            f"Email:       {lead.get('email') or 'Not provided'}\n"
            f"Phone:       {lead.get('phone') or 'Not provided'}\n"
            f"Description: {lead.get('description') or 'Not provided'}\n"
            f"Message:     {lead.get('question') or 'Not provided'}\n\n"
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


# ═══════════════════════════════════════════════════════════════════════════════
# HYBRID SECTION SELECTION  (keyword pre-filter → existing single answer call)
#
# Step-3 of the scraper rewrite. The scraper writes knowledge_sections.json per
# client; at answer time we keyword-narrow it to the few sections most likely to
# hold the answer and feed only those into the existing prompt. The flat
# knowledge.txt summary stays as the fallback whenever sections are missing or
# nothing matches, so the bot never answers with nothing.
# ═══════════════════════════════════════════════════════════════════════════════

def _flatten_sections(pages: list) -> list:
    """[{page_url, page_title, sections:[...]}] → flat list of section dicts, each
    carrying its page context."""
    flat = []
    for page in pages or []:
        ptitle = page.get("page_title", "")
        purl   = page.get("page_url", "")
        for sec in page.get("sections", []):
            flat.append({
                "page_title":  ptitle,
                "page_url":    purl,
                "heading":     sec.get("heading", ""),
                "level":       sec.get("level", 0),
                "subheadings": sec.get("subheadings", []) or [],
                "text":        sec.get("text", ""),
            })
    return flat


def load_sections(bid: str) -> list:
    """Load + cache the flattened knowledge_sections.json for a client. Returns []
    when the file is absent/unreadable (older clients, or scrapes predating step-2)
    so callers transparently fall back to the knowledge.txt summary."""
    if bid in sections_cache:
        return sections_cache[bid]
    base = client_dir(bid)
    flat = []
    if base:
        path = os.path.join(base, "config", "knowledge_sections.json")
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    flat = _flatten_sections(json.load(f))
            except Exception as e:
                print(f"[SECTIONS LOAD ERROR] {bid}: {e}")
                flat = []
    sections_cache[bid] = flat
    return flat


# Small, dependency-free keyword scorer. Generous by design — the goal is to never
# miss the right section, not to be precise; the answer model does the final pick.
_STOPWORDS = frozenset({
    "the", "and", "for", "are", "but", "not", "you", "your", "with", "that", "this",
    "have", "has", "had", "was", "were", "will", "would", "can", "could", "should",
    "what", "which", "who", "how", "when", "where", "why", "does", "did", "done",
    "about", "into", "from", "they", "them", "their", "our", "its", "his", "her",
    "she", "him", "because", "just", "any", "all", "some", "more", "most", "much",
    "many", "get", "got", "want", "need", "know", "tell", "please", "here", "there",
    "then", "than", "too", "very", "also", "yes", "out", "off", "over", "under",
    "again", "once", "been", "being", "do", "is", "it", "to", "of", "in", "on", "at",
    "by", "or", "an", "as", "be", "we", "my", "me", "us", "so", "up", "no",
})
_WORD_RE       = re.compile(r"[a-z0-9]+")
_W_HEADING     = 3      # heading keyword hit weight
_W_SUBHEAD     = 2      # subheading keyword hit weight
_W_TEXT        = 1      # light weight on the lead of the section body
_TEXT_LEAD_CHARS = 400  # only the first part of the text is scored


def _stem(tok: str) -> str:
    """Crude suffix stripper so price↔pricing, ship↔shipping, return↔returns match."""
    for suf in ("ing", "ed", "es", "s"):
        if tok.endswith(suf) and len(tok) - len(suf) >= 3:
            return tok[: -len(suf)]
    return tok


def _stem_set(text: str) -> set:
    return {_stem(t) for t in _WORD_RE.findall((text or "").lower())
            if len(t) >= 2 and t not in _STOPWORDS}


def _overlap(query_stems: set, target_stems: set) -> int:
    """Count query stems hitting the target — exact stem match, or a shared 4-char
    prefix (generous, catches near-misses the stemmer leaves behind)."""
    if not query_stems or not target_stems:
        return 0
    hits = 0
    for q in query_stems:
        if q in target_stems:
            hits += 1
        elif len(q) >= 4 and any(len(t) >= 4 and t[:4] == q[:4] for t in target_stems):
            hits += 1
    return hits


def _score_section(query_stems: set, sec: dict) -> int:
    h  = _overlap(query_stems, _stem_set(sec.get("heading", "")))
    sh = _overlap(query_stems, _stem_set(" ".join(sec.get("subheadings", []))))
    tx = _overlap(query_stems, _stem_set((sec.get("text", "") or "")[:_TEXT_LEAD_CHARS]))
    return _W_HEADING * h + _W_SUBHEAD * sh + _W_TEXT * tx


def select_sections(message: str, sections: list) -> list:
    """Stage-1 keyword pre-filter. Returns up to MAX_CANDIDATE_SECTIONS sections in
    score order. Empty list → caller falls back to the flat summary. level:0 lead/nav
    sections are excluded unless nothing else matched."""
    query_stems = _stem_set(message)
    if not query_stems or not sections:
        return []
    scored = []
    for sec in sections:
        s = _score_section(query_stems, sec)
        if s > 0:
            scored.append((s, sec))
    if not scored:
        return []
    content = [pair for pair in scored if pair[1].get("level", 0) > 0]
    pool = content if content else scored          # only use lead/nav if nothing else hit
    pool.sort(key=lambda pair: pair[0], reverse=True)   # key= avoids comparing dicts on ties
    return [sec for _, sec in pool[:MAX_CANDIDATE_SECTIONS]]


def pack_sections(sections: list, budget: int) -> str:
    """Stage-2 assembly: join candidate sections (heading + text) in score order,
    whole sections only, stopping before `budget` is exceeded. Drops lowest-ranked
    sections rather than slicing mid-section (the one exception is a single top
    section larger than the entire budget, which is trimmed so we answer with
    something)."""
    parts, used = [], 0
    for sec in sections:
        heading = (sec.get("heading") or "").strip()
        text    = (sec.get("text") or "").strip()
        if not heading and not text:
            continue
        block = f"## {heading}\n{text}".strip() if heading else text
        cost  = len(block) + (2 if parts else 0)   # 2 = the "\n\n" joiner
        if used + cost > budget:
            if parts:
                break                              # drop this + all lower-ranked
            block = block[:budget].strip()         # unavoidable: top section > budget
            if block:
                parts.append(block)
            break
        parts.append(block)
        used += cost
    return "\n\n".join(parts)


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
        "bot_name":        "Assistant",
        "bot_tagline":     "Your AI Assistant",
        "collect_leads":   True,
        "widget_position": "bottom-right",
        "white_label":     False,
        "color_mode":      "manual",
        "logo_mode":       "default",
        "theme":           "dark",
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


# Plans that grant a live chatbot. Activation is gated by plan_is_active() below.
PAID_PLANS = {"starter", "growth", "pro"}

# The company's own demo bots are never paying customers — always live.
# stride_sneakers powers the public demo at atlyz.com/demo (seeded on startup).
# atlyz is the live first-party site bot on the Railway volume.
# (atlyz-website / atlyz_website were the retired old bot.)
ALWAYS_ACTIVE_BIDS = {"atlyz", "stride_sneakers"}


def set_business_config_field(bid: str, key: str, value: str) -> bool:
    """Update (or append) a `key = value` line in business_config.txt, preserving
    every other line. Returns False if the business id is unsafe / missing."""
    base = client_dir(bid)
    if not base:
        return False
    path = os.path.join(base, "config", "business_config.txt")
    lines, found = [], False
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                if "=" in line and line.split("=", 1)[0].strip() == key:
                    lines.append(f"{key} = {value}\n")
                    found = True
                else:
                    lines.append(line if line.endswith("\n") else line + "\n")
    if not found:
        lines.append(f"{key} = {value}\n")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with _disk_lock:
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines)
    return True


def plan_is_active(bid: str) -> bool:
    """True if this business may serve live chats.

    A business is active when it has a paid plan AND its subscription is active.
    `plan_status` semantics:
      - "active"                         → live
      - missing (legacy pre-webhook biz) → live (grandfathered)
      - "pending"/"canceled"/"past_due"  → blocked (no verified payment)
    """
    if bid in ALWAYS_ACTIVE_BIDS:
        return True
    cfg    = load_business_config(bid)
    plan   = (cfg.get("plan", "") or "").strip().lower()
    if plan not in PAID_PLANS:
        return False
    status = (cfg.get("plan_status", "") or "").strip().lower()
    return status in ("", "active")


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


def bump_stats(bid: str, chats: int = 0, messages: int = 0, rescrapes: int = 0):
    path = _stats_path(bid)
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        this_month = datetime.now().strftime("%Y-%m")
        with _disk_lock:
            data = {"total_chats": 0, "total_messages": 0, "first_seen": None,
                    "month": this_month, "chats_this_month": 0,
                    "total_rescrapes": 0, "rescrapes_this_month": 0}
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
                data["chats_this_month"]     = 0
                data["rescrapes_this_month"] = 0   # same month boundary as chats
            data["total_chats"]          = data.get("total_chats", 0) + chats
            data["chats_this_month"]     = data.get("chats_this_month", 0) + chats
            data["total_messages"]       = data.get("total_messages", 0) + messages
            data["total_rescrapes"]      = data.get("total_rescrapes", 0) + rescrapes
            data["rescrapes_this_month"] = data.get("rescrapes_this_month", 0) + rescrapes
            data["last_active"]          = datetime.now().isoformat()
            with open(path, "w") as f:
                json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[STATS ERROR] {e}")


def monthly_chats_used(bid: str) -> int:
    stats = read_stats(bid)
    if stats.get("month") != datetime.now().strftime("%Y-%m"):
        return 0
    return stats.get("chats_this_month", 0)


def monthly_rescrapes_used(bid: str) -> int:
    """Manual re-scrapes used this calendar month (0 once the month rolls over —
    boundary handled on read, exactly like monthly_chats_used)."""
    stats = read_stats(bid)
    if stats.get("month") != datetime.now().strftime("%Y-%m"):
        return 0
    return stats.get("rescrapes_this_month", 0)


def rescrape_limit(bid: str):
    """Monthly manual re-scrape allowance for this bid. None = unlimited
    (whitelisted first-party/demo bots bypass the quota entirely)."""
    if bid in ALWAYS_ACTIVE_BIDS:
        return None
    return plan_features(bid).get("rescrapes_per_month")


def rescrapes_remaining(bid: str):
    """Re-scrapes left this month, or None when unlimited."""
    limit = rescrape_limit(bid)
    if limit is None:
        return None
    return max(limit - monthly_rescrapes_used(bid), 0)


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
    bot_name      = config.get("bot_name", "Assistant")
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
    kbudget    = max(remaining, 1000)
    flat_fallback = (knowledge or "").strip()[:kbudget]

    # Hybrid section selection (step-3). When the scraper has written
    # knowledge_sections.json, keyword-narrow it to the handful of sections most
    # likely to hold the answer and feed only those as WEBSITE KNOWLEDGE, packed
    # whole within the same budget. Falls back to the flat knowledge.txt summary
    # when sections are missing (older clients, atlyz/demo) or nothing matched, so
    # the bot never answers with nothing. No extra LLM call — the answer model does
    # the final relevance pick as it writes the reply below.
    website_knowledge = flat_fallback
    selected = select_sections(message, load_sections(bid))
    if selected:
        packed = pack_sections(selected, kbudget)
        if packed:
            website_knowledge = packed

    owner_section     = owner_info if owner_info else "(No owner-provided info yet.)"
    knowledge_section = website_knowledge if website_knowledge else "(No scraped website knowledge yet.)"

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

    # Conversation history is NOT injected into the system prompt — it is sent below as
    # real chat turns instead. Keeping it out keeps the large knowledge prefix stable and
    # cacheable, and avoids paying for the same history twice.

    # Smart email routing is specific to Atlyz's own site. Every other business gets a
    # generic, business-appropriate fallback — customers must never be sent to Atlyz inboxes.
    if bid == "atlyz":
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
- Light humour when appropriate; 1 emoji max, only when it feels natural

BREVITY (most important):
- Keep every reply as short as possible — hard cap 7 lines, ideally far fewer
- Get straight to the point — no filler, no over-explaining, no restating the question
- Skip greetings and sign-offs ("Hi there!", "Hope that helps!") — just answer
- This is a chat widget, not an article: short and scannable beats thorough

FORMATTING:
- Use a short numbered or bulleted list ONLY when the answer is naturally list-shaped (multiple steps, options, or items)
- For everything else, write brief natural prose
- Never bullet a single fact or a greeting; keep each list item to one line

ACCURACY (never invent):
- Answer ONLY from the KNOWLEDGE BASE below (owner-provided info + website knowledge)
- If a specific detail isn't there (e.g. a list of blog articles, a price, a product spec), say you don't have that detail and point them to the relevant page or contact
- NEVER make up article titles, product names, prices, features, or any fact that isn't in your knowledge — a guess that sounds right is still wrong

YOUR JOB:
- Answer exactly what was asked — nothing more, nothing less
- Never push products unless the customer asks about them
- If asked what YOU can do: say you can answer questions about this business, help find what they need, and point them to the right contact if needed

{dont_know_section}

KNOWLEDGE BASE:
{knowledge_block}

LEAD & FLOW RULES:
{lead_rule}
- If the customer says goodbye and is done: action = end.

{language_instruction}

Always respond with valid JSON: {{"reply": "...", "action": "chat", "language": "English"}}
action must be: chat, collect_lead, or end"""

    # Code-level safety net — used only if the model returns unusable output twice or
    # the API call errors. No "flag to team" phrasing; route to a contact instead.
    if bid == "atlyz":
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
                reasoning_effort="low",  # FAQ-style answers don't need deep reasoning — cuts reasoning/output token cost
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
                writer = csv.DictWriter(f, fieldnames=["lead_id", "timestamp", "name", "email", "phone", "description", "question", "session_id"])
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
    # First-party / demo bots (ALWAYS_ACTIVE_BIDS) get unlimited chats — the
    # monthly cap never applies to them, regardless of plan. Everyone else is
    # capped per their plan's monthly_chats.
    over_limit = (bid not in ALWAYS_ACTIVE_BIDS
                  and cap is not None and monthly_chats_used(bid) >= cap)

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
            "theme":           config.get("theme", "dark"),
            "widget_position": config.get("widget_position", "bottom-right"),
            "bot_name":        config.get("bot_name", "Assistant"),
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

    # Reject over-long messages before they ever reach the LLM (cost + abuse guard).
    if len(message) > MAX_USER_MSG_CHARS:
        return jsonify({
            "reply":    "Message too long, please shorten it.",
            "action":   "chat",
            "language": "English",
        }), 400

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

    if not plan_is_active(bid):
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
        "lead_id":     str(uuid.uuid4())[:8],
        "timestamp":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "name":        (data.get("name", "") or "")[:120],
        "email":       (data.get("email", "") or "")[:160],
        "phone":       (data.get("phone", "") or "")[:40],
        "description": (data.get("description", "") or "")[:500],
        "question":    (data.get("question", "") or "")[:500],
        "session_id":  session_id,
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
    if not config.get("bot_name"):
        config["bot_name"] = business_config.get("bot_name", "Assistant")
    response = jsonify(config)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response


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
    if auth_rate_limited():
        return jsonify({"success": False, "error": "Too many attempts. Please wait a minute and try again."}), 429

    data     = request.get_json(silent=True) or {}
    name     = (data.get("name", "") or "").strip()
    email    = (data.get("email", "") or "").strip().lower()
    password = data.get("password", "") or ""

    phone    = (data.get("phone", "") or "").strip()[:40]

    if not email or not EMAIL_RE.match(email):
        return jsonify({"success": False, "error": "Please enter a valid email address."}), 400
    if len(password) < 8:
        return jsonify({"success": False, "error": "Password must be at least 8 characters."}), 400

    accounts = load_accounts()
    if email in accounts:
        return jsonify({"success": False, "error": "An account with this email already exists — try logging in."}), 409

    accounts[email] = {
        "name":          name,
        "phone":         phone,
        "password_hash": generate_password_hash(password),
        "created_at":    datetime.now().isoformat(),
        "businesses":    [],
    }
    save_accounts(accounts)
    send_welcome_email(email, name)
    return jsonify({"success": True, "token": issue_token(email), "email": email, "name": name, "phone": phone})


@app.route("/auth/login", methods=["POST"])
def auth_login():
    if auth_rate_limited():
        return jsonify({"success": False, "error": "Too many attempts. Please wait a minute and try again."}), 429

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
        "phone":      account.get("phone", ""),
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
        "phone":      account.get("phone", ""),
        "businesses": businesses,
    })


@app.route("/auth/profile", methods=["POST"])
def auth_profile():
    email = request_account_email()
    if not email:
        return jsonify({"success": False, "error": "Not signed in"}), 401
    accounts = load_accounts()
    account  = accounts.get(email)
    if not account:
        return jsonify({"success": False, "error": "Account not found"}), 404

    data = request.get_json(silent=True) or {}

    if "name" in data:
        account["name"] = (data["name"] or "").strip()[:120]
    if "phone" in data:
        account["phone"] = (data["phone"] or "").strip()[:40]

    if "new_password" in data:
        cur = data.get("current_password", "")
        nw  = data.get("new_password", "")
        cf  = data.get("confirm_password", "")
        if not check_password_hash(account.get("password_hash", ""), cur):
            return jsonify({"success": False, "error": "Current password is incorrect."}), 400
        if len(nw) < 8:
            return jsonify({"success": False, "error": "New password must be at least 8 characters."}), 400
        if nw != cf:
            return jsonify({"success": False, "error": "Passwords don't match."}), 400
        account["password_hash"] = generate_password_hash(nw)

    save_accounts(accounts)
    return jsonify({"success": True, "name": account.get("name", ""), "phone": account.get("phone", ""), "email": email})


@app.route("/auth/forgot-password", methods=["POST"])
def auth_forgot_password():
    if auth_rate_limited():
        return jsonify({"success": True}), 429  # generic — don't reveal anything

    data  = request.get_json(silent=True) or {}
    email = (data.get("email", "") or "").strip().lower()

    accounts = load_accounts()
    account  = accounts.get(email)
    if account:
        token  = secrets.token_hex(32)
        expiry = (datetime.now() + timedelta(hours=1)).isoformat()
        account["reset_token"]  = token
        account["reset_expiry"] = expiry
        save_accounts(accounts)

        try:
            api_key = os.getenv("RESEND_API_KEY", "")
            if api_key:
                body = (
                    "Hi,\n\n"
                    "You requested a password reset for your Atlyz account.\n\n"
                    "Click the link below to reset your password. This link expires in 1 hour.\n\n"
                    f"https://app.atlyz.com/dashboard?reset_token={token}\n\n"
                    "If you didn't request this, ignore this email.\n\n"
                    "— The Atlyz Team"
                )
                requests.post(
                    "https://api.resend.com/emails",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={
                        "from":    "Atlyz <noreply@send.atlyz.com>",
                        "to":      [email],
                        "subject": "Reset your Atlyz password",
                        "text":    body,
                    },
                    timeout=10,
                )
        except Exception as e:
            print(f"[RESET EMAIL ERROR] {e}")

    # Always return success — don't reveal whether the email exists
    return jsonify({"success": True})


@app.route("/auth/reset-password", methods=["POST"])
def auth_reset_password():
    if auth_rate_limited():
        return jsonify({"success": False, "error": "Too many attempts. Please wait a minute and try again."}), 429

    data         = request.get_json(silent=True) or {}
    token        = (data.get("token", "") or "").strip()
    new_password = data.get("new_password", "") or ""

    if not token:
        return jsonify({"success": False, "error": "Missing reset token."}), 400
    if len(new_password) < 8:
        return jsonify({"success": False, "error": "Password must be at least 8 characters."}), 400

    accounts = load_accounts()
    matched_email = None
    for email, account in accounts.items():
        if account.get("reset_token") == token:
            matched_email = email
            break

    if not matched_email:
        return jsonify({"success": False, "error": "Invalid or expired reset link."}), 400

    account = accounts[matched_email]
    expiry  = account.get("reset_expiry", "")
    try:
        if datetime.fromisoformat(expiry) < datetime.now():
            return jsonify({"success": False, "error": "This reset link has expired. Please request a new one."}), 400
    except Exception:
        return jsonify({"success": False, "error": "Invalid reset link."}), 400

    account["password_hash"] = generate_password_hash(new_password)
    account.pop("reset_token",  None)
    account.pop("reset_expiry", None)
    save_accounts(accounts)
    return jsonify({"success": True})


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

    if "theme" in data:
        current["theme"] = "light" if str(data["theme"]).lower() == "light" else "dark"

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

    leads       = read_leads(bid)
    leads_count = len(leads)
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

    # Chats today / this week from conversation files (scan up to 500 most recent)
    now        = datetime.now()
    today      = now.date()
    week_start = today - timedelta(days=today.weekday())
    chats_today      = 0
    chats_this_week  = 0
    conv_dir = os.path.join(client_dir(bid), "data", "conversations")
    if os.path.isdir(conv_dir):
        try:
            fnames = [f for f in os.listdir(conv_dir) if f.endswith(".json")]
            fnames.sort(key=lambda f: os.path.getmtime(os.path.join(conv_dir, f)), reverse=True)
            for fname in fnames[:500]:
                try:
                    with open(os.path.join(conv_dir, fname), encoding="utf-8") as fh:
                        rec = json.load(fh)
                    ts = rec.get("started_at", "")
                    if ts:
                        d = datetime.fromisoformat(ts).date()
                        if d == today:
                            chats_today += 1
                        if d >= week_start:
                            chats_this_week += 1
                except Exception:
                    pass
        except Exception:
            pass

    # Last 3 leads for overview preview
    recent_leads = [
        {"name": l.get("name", ""), "email": l.get("email", ""), "timestamp": l.get("timestamp", "")}
        for l in leads[-3:]
    ]
    recent_leads.reverse()

    return jsonify({
        "business_id":      bid,
        "plan":             plan,
        "plan_label":       feats.get("label", plan.title()),
        "features":         feats,
        # Whitelisted first-party bots are uncapped — report no limit so the
        # dashboard shows "Unlimited" (mirrors the /chat/start cap bypass).
        "monthly_cap":      None if bid in ALWAYS_ACTIVE_BIDS else feats.get("monthly_chats"),
        "chats_this_month": monthly_chats_used(bid),
        # Manual re-scrape quota (None cap = unlimited for whitelisted bots)
        "rescrape_limit":       rescrape_limit(bid),
        "rescrapes_this_month": monthly_rescrapes_used(bid),
        "rescrapes_remaining":  rescrapes_remaining(bid),
        "chats_today":      chats_today,
        "chats_this_week":  chats_this_week,
        "leads_total":      leads_count,
        "recent_leads":     recent_leads,
        "total_chats":      stats.get("total_chats", 0),
        "total_messages":   stats.get("total_messages", 0),
        "active_sessions":  len(active),
        "last_active":      stats.get("last_active", "never"),
        "last_scraped":     scrape_meta.get("timestamp", "never"),
        "pages_scraped":    scrape_meta.get("pages_scraped", 0),
    })


@app.route("/chatbot/analytics/<bid>", methods=["GET"])
def chatbot_analytics(bid):
    if not business_exists(bid):
        return jsonify({"error": "Unknown business"}), 404
    if not is_owner_of(bid):
        return jsonify({"error": "Unauthorized"}), 401

    plan  = business_plan(bid)
    feats = plans.get_plan(plan)
    if not feats.get("analytics", False):
        return jsonify({"error": "Analytics not available on this plan"}), 403

    is_pro   = plan == "pro"
    now      = datetime.now()
    today    = now.date()
    week_start      = today - timedelta(days=today.weekday())
    last_week_start = week_start - timedelta(days=7)

    # Parse all conversation files
    conv_dir = os.path.join(client_dir(bid), "data", "conversations")
    parsed   = []
    if os.path.isdir(conv_dir):
        for fname in os.listdir(conv_dir):
            if not fname.endswith(".json"):
                continue
            try:
                with open(os.path.join(conv_dir, fname), encoding="utf-8") as fh:
                    rec = json.load(fh)
                ts = rec.get("started_at", "")
                if ts:
                    parsed.append({
                        "dt":   datetime.fromisoformat(ts),
                        "lead": bool(rec.get("lead_captured", False)),
                    })
            except Exception:
                pass

    # 7-day chart
    days_7 = []
    for i in range(6, -1, -1):
        d     = today - timedelta(days=i)
        count = sum(1 for p in parsed if p["dt"].date() == d)
        days_7.append({"date": d.isoformat(), "label": d.strftime("%a"), "count": count})

    # Chats by hour (0-23)
    hour_counts = [0] * 24
    for p in parsed:
        hour_counts[p["dt"].hour] += 1
    hours = [{"hour": h, "label": f"{h:02d}:00" if h % 6 == 0 else "", "count": hour_counts[h]}
             for h in range(24)]

    # This week vs last week
    this_week = sum(1 for p in parsed if week_start <= p["dt"].date() < week_start + timedelta(days=7))
    last_week = sum(1 for p in parsed if last_week_start <= p["dt"].date() < week_start)

    # Leads this week
    leads = read_leads(bid)
    leads_this_week = 0
    for l in leads:
        try:
            lt = datetime.strptime(l.get("timestamp", ""), "%Y-%m-%d %H:%M:%S")
            if lt.date() >= week_start:
                leads_this_week += 1
        except Exception:
            pass

    result = {
        "plan":             plan,
        "days_7":           days_7,
        "hours":            hours,
        "this_week":        this_week,
        "last_week":        last_week,
        "total_leads":      len(leads),
        "leads_this_week":  leads_this_week,
    }

    if is_pro:
        # 30-day chart
        days_30 = []
        for i in range(29, -1, -1):
            d     = today - timedelta(days=i)
            count = sum(1 for p in parsed if p["dt"].date() == d)
            days_30.append({"date": d.isoformat(), "label": d.strftime("%-d %b"), "count": count})

        total_30    = sum(d["count"] for d in days_30)
        avg_per_day = round(total_30 / 30, 1)

        # Busiest day of week
        dow = [0] * 7
        for p in parsed:
            dow[p["dt"].weekday()] += 1
        day_names   = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
        busiest_day = day_names[dow.index(max(dow))] if any(dow) else "—"

        # Lead conversion rate
        total_convs   = len(parsed)
        leads_captured = sum(1 for p in parsed if p["lead"])
        conversion_rate = round(leads_captured / total_convs * 100, 1) if total_convs else 0.0

        # Weekly trend (last 4 weeks)
        weekly_trend = []
        for w in range(3, -1, -1):
            ws    = week_start - timedelta(days=7 * w)
            we    = ws + timedelta(days=7)
            count = sum(1 for p in parsed if ws <= p["dt"].date() < we)
            weekly_trend.append({"label": ws.strftime("%-d %b"), "count": count})

        result.update({
            "days_30":         days_30,
            "avg_per_day":     avg_per_day,
            "busiest_day":     busiest_day,
            "conversion_rate": conversion_rate,
            "weekly_trend":    weekly_trend,
        })

    return jsonify(result)


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
        sections_cache.pop(bid, None)

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

    # Plan-based monthly re-scrape quota. Whitelisted bots (limit is None) bypass
    # entirely. At limit → block before scraping, no work done.
    limit = rescrape_limit(bid)
    if limit is not None and monthly_rescrapes_used(bid) >= limit:
        return jsonify({
            "success":             False,
            "error":               f"You've used all {limit} re-scrapes this month. Resets on the 1st.",
            "rescrape_limit":      limit,
            "rescrapes_remaining": 0,
        }), 429

    result = run_scrape(bid, website_url, force=force)
    if result.get("status") == "skipped":
        # Unchanged site = no new content; don't spend a re-scrape on a no-op.
        return jsonify({
            "success":             True,
            "changed":             False,
            "message":             "Website unchanged since last scrape",
            "rescrape_limit":      limit,
            "rescrapes_remaining": rescrapes_remaining(bid),
        })
    if result.get("status") == "ok":
        knowledge_cache.pop(bid, None)
        sections_cache.pop(bid, None)
        bump_stats(bid, rescrapes=1)   # only a real, content-changing re-scrape counts
        return jsonify({
            "success":             True,
            "changed":             True,
            "pages_scraped":       result["pages_scraped"],
            "brand_color":         result.get("brand_color", ""),
            "rescrape_limit":      limit,
            "rescrapes_remaining": rescrapes_remaining(bid),
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
    base = client_dir(bid)
    if not base or not business_exists(bid):
        return jsonify({"error": "Unknown business"}), 404

    knowledge_path = os.path.join(base, "config", "knowledge.txt")
    with open(knowledge_path, "a", encoding="utf-8") as f:
        f.write("\n" + knowledge_text)
    knowledge_cache.pop(bid, None)
    sections_cache.pop(bid, None)

    return jsonify({"success": True, "business_id": bid, "appended_chars": len(knowledge_text)})


def list_all_bids() -> list:
    if not os.path.isdir(CLIENTS_DIR):
        return []
    return sorted(d for d in os.listdir(CLIENTS_DIR)
                  if os.path.isdir(os.path.join(CLIENTS_DIR, d)) and valid_business_id(d))


def parse_owner_meta(bid: str) -> dict:
    """Parse the structured header written by setup/create into a dict."""
    key_map = {
        "owner name": "owner_name", "email":   "email",
        "company":    "company",    "website": "website",
        "phone":      "phone",      "plan":    "plan",
        "joined":     "joined",
    }
    meta = {v: "" for v in key_map.values()}
    for line in load_owner_info(bid).splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            mapped = key_map.get(k.strip().lower())
            if mapped:
                meta[mapped] = v.strip()
    return meta


def analyze_faqs(bid: str) -> list:
    """Read all conversation files, count customer messages, write bot_faqs.txt."""
    conv_dir = os.path.join(client_dir(bid), "data", "conversations")
    if not os.path.isdir(conv_dir):
        return []
    freq = {}
    for fname in os.listdir(conv_dir):
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(conv_dir, fname), encoding="utf-8") as fh:
                rec = json.load(fh)
            for h in rec.get("history", []):
                msg = (h.get("customer", "") or "").strip().lower()
                if len(msg) < 5:
                    continue
                freq[msg] = freq.get(msg, 0) + 1
        except Exception:
            pass

    top = sorted(freq.items(), key=lambda x: x[1], reverse=True)[:20]

    base = client_dir(bid)
    if base:
        faq_path = os.path.join(base, "config", "bot_faqs.txt")
        try:
            with open(faq_path, "w", encoding="utf-8") as fh:
                for q, cnt in top:
                    fh.write(f"[{cnt}] {q}\n")
        except Exception:
            pass

    return [{"count": cnt, "question": q} for q, cnt in top]


def read_faqs(bid: str) -> list:
    base = client_dir(bid)
    if not base:
        return []
    path = os.path.join(base, "config", "bot_faqs.txt")
    if not os.path.exists(path):
        return []
    faqs = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("["):
                    close = line.index("]")
                    count = int(line[1:close])
                    q     = line[close + 2:]
                    faqs.append({"count": count, "question": q})
    except Exception:
        pass
    return faqs[:10]


@app.route("/admin/clients", methods=["GET"])
def admin_clients():
    if not check_admin_key():
        return jsonify({"error": "Unauthorized"}), 401
    result = []
    for bid in list_all_bids():
        stats  = read_stats(bid)
        leads  = read_leads(bid)
        meta   = parse_owner_meta(bid)
        faqs   = read_faqs(bid)
        result.append({
            "business_id":   bid,
            "owner_meta":    meta,
            "plan":          business_plan(bid),
            "total_chats":   stats.get("total_chats", 0),
            "total_leads":   len(leads),
            "last_active":   stats.get("last_active", "never"),
            "top_faqs":      faqs,
        })
    return jsonify({"clients": result, "count": len(result)})


@app.route("/admin/analyze-faqs/<bid>", methods=["POST"])
def admin_analyze_faqs(bid):
    if not check_admin_key():
        return jsonify({"error": "Unauthorized"}), 401
    if not business_exists(bid):
        return jsonify({"error": "Unknown business"}), 404
    faqs = analyze_faqs(bid)
    return jsonify({"business_id": bid, "faqs": faqs, "count": len(faqs)})


@app.route("/admin/analyze-all-faqs", methods=["POST"])
def admin_analyze_all_faqs():
    if not check_admin_key():
        return jsonify({"error": "Unauthorized"}), 401
    summary = []
    for bid in list_all_bids():
        faqs = analyze_faqs(bid)
        summary.append({"business_id": bid, "faq_count": len(faqs),
                        "top": faqs[0]["question"] if faqs else None})
    return jsonify({"processed": len(summary), "summary": summary})


@app.route("/admin-panel")
def admin_panel():
    if not check_admin_key():
        return "<h1>Unauthorized</h1>", 401

    admin_key = (request.args.get("key", "") or
                 request.headers.get("X-Admin-Key", "") or
                 os.getenv("ATLYZ_ADMIN_KEY", ""))

    bids    = list_all_bids()
    clients = []
    total_leads = 0
    total_chats = 0
    for bid in bids:
        meta    = parse_owner_meta(bid)
        stats   = read_stats(bid)
        leads   = read_leads(bid)
        faqs    = read_faqs(bid)
        plan    = business_plan(bid)
        cfg     = load_chatbot_config(bid)
        total_leads += len(leads)
        total_chats += stats.get("total_chats", 0)
        clients.append({
            "bid":          bid,
            "biz_name":     cfg.get("business_name") or meta.get("company") or bid,
            "owner_name":   meta.get("owner_name", ""),
            "email":        meta.get("email", ""),
            "website":      meta.get("website", ""),
            "phone":        meta.get("phone", ""),
            "plan":         plan,
            "joined":       meta.get("joined", ""),
            "total_chats":  stats.get("total_chats", 0),
            "total_leads":  len(leads),
            "last_active":  stats.get("last_active", "never"),
            "faqs":         faqs,
        })

    def card(c):
        faq_html = ""
        if c["faqs"]:
            rows = "".join(
                f'<div class="faq-item"><span class="faq-count">×{f["count"]}</span>{f["question"]}</div>'
                for f in c["faqs"]
            )
            faq_html = f'<div class="faqs"><div class="faq-label">Top FAQs</div>{rows}</div>'
        else:
            faq_html = '<div class="faqs"><div class="faq-label">Top FAQs</div><div class="faq-empty">No data yet — click Analyze</div></div>'

        website_html = (f'<a href="{c["website"]}" target="_blank" class="link">{c["website"]}</a>'
                        if c["website"] else "—")
        phone_html   = f'<br>📞 {c["phone"]}' if c["phone"] else ""

        return f"""
        <div class="card" id="card-{c['bid']}">
          <div class="card-top">
            <div>
              <div class="biz-name">{c['biz_name']}</div>
              <span class="badge badge-{c['plan']}">{c['plan'].upper()}</span>
            </div>
            <div class="card-stats">
              <div class="cstat"><div class="cstat-n">{c['total_chats']}</div><div class="cstat-l">chats</div></div>
              <div class="cstat"><div class="cstat-n">{c['total_leads']}</div><div class="cstat-l">leads</div></div>
            </div>
          </div>
          <div class="meta">
            👤 {c['owner_name'] or '—'} &nbsp;·&nbsp; ✉️ {c['email'] or '—'}{phone_html}<br>
            🌐 {website_html}<br>
            📅 Joined {c['joined'] or '—'} &nbsp;·&nbsp; Last active: {c['last_active'][:10] if c['last_active'] != 'never' else 'never'}
          </div>
          {faq_html}
          <div style="display:flex;gap:8px;margin-top:14px;align-items:center">
            <button class="btn-analyze" onclick="analyzeFAQs('{c['bid']}', this)">Analyze FAQs</button>
            <span class="msg" id="msg-{c['bid']}" style="display:none"></span>
          </div>
        </div>"""

    cards_html = "\n".join(card(c) for c in clients)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Atlyz Admin Panel</title>
<style>
  :root{{--bg:#06080f;--panel:#111827;--line:rgba(255,255,255,.08);--txt:#F3F4F6;--muted:#8B90A3;
        --dim:#5B6072;--pri:#00C2FF;--mint:#10B981;--coral:#ef4444}}
  *{{box-sizing:border-box}}
  body{{margin:0;padding:24px 28px;background:var(--bg);color:var(--txt);
       font-family:system-ui,-apple-system,sans-serif;font-size:14px}}
  a{{color:var(--pri)}}
  .topbar{{display:flex;align-items:center;justify-content:space-between;
           border-bottom:1px solid var(--line);padding-bottom:16px;margin-bottom:24px}}
  .topbar h1{{margin:0;font-size:22px;color:var(--pri);letter-spacing:-.3px}}
  .topbar .sub{{color:var(--muted);font-size:13px;margin-top:3px}}
  .summary{{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:24px}}
  .scard{{background:var(--panel);border:1px solid var(--line);border-radius:12px;
          padding:14px 20px;min-width:120px;text-align:center}}
  .scard .n{{font-size:26px;font-weight:700;color:var(--pri)}}
  .scard .l{{font-size:12px;color:var(--muted);margin-top:2px}}
  .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(400px,1fr));gap:16px}}
  .card{{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:20px}}
  .card-top{{display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:12px}}
  .biz-name{{font-size:16px;font-weight:700;margin-bottom:4px}}
  .badge{{display:inline-block;padding:2px 9px;border-radius:20px;font-size:10px;font-weight:700;
          letter-spacing:.5px;text-transform:uppercase}}
  .badge-starter{{background:rgba(255,255,255,.06);color:var(--muted);border:1px solid var(--line)}}
  .badge-growth{{background:rgba(16,185,129,.12);color:var(--mint);border:1px solid rgba(16,185,129,.3)}}
  .badge-pro{{background:rgba(0,194,255,.12);color:var(--pri);border:1px solid rgba(0,194,255,.3)}}
  .card-stats{{display:flex;gap:10px}}
  .cstat{{background:rgba(0,0,0,.3);border:1px solid var(--line);border-radius:8px;
          padding:8px 12px;text-align:center;min-width:52px}}
  .cstat-n{{font-size:18px;font-weight:700}}
  .cstat-l{{font-size:10px;color:var(--dim)}}
  .meta{{font-size:12.5px;color:var(--muted);line-height:1.9;margin-bottom:14px}}
  .link{{color:var(--pri);text-decoration:none;word-break:break-all}}
  .faqs{{border-top:1px solid var(--line);padding-top:12px}}
  .faq-label{{font-size:10.5px;text-transform:uppercase;letter-spacing:1px;color:var(--dim);margin-bottom:8px}}
  .faq-item{{font-size:12px;color:var(--muted);padding:4px 0;border-bottom:1px solid rgba(255,255,255,.04);display:flex;gap:6px;align-items:baseline}}
  .faq-item:last-child{{border-bottom:none}}
  .faq-count{{color:var(--pri);font-weight:700;font-size:10px;flex-shrink:0}}
  .faq-empty{{font-size:12px;color:var(--dim);font-style:italic}}
  .btn-analyze{{background:none;border:1px solid rgba(0,194,255,.35);color:var(--pri);
               border-radius:8px;padding:6px 13px;font-size:12.5px;cursor:pointer;transition:.15s}}
  .btn-analyze:hover{{background:rgba(0,194,255,.07)}}
  .btn-analyze:disabled{{opacity:.45;cursor:not-allowed}}
  .msg{{font-size:12px;padding:4px 10px;border-radius:6px}}
  .msg.ok{{background:rgba(16,185,129,.1);color:var(--mint);border:1px solid rgba(16,185,129,.2)}}
  .msg.bad{{background:rgba(239,68,68,.08);color:var(--coral);border:1px solid rgba(239,68,68,.2)}}
  .analyze-all{{background:rgba(0,194,255,.1);border:1px solid rgba(0,194,255,.3);color:var(--pri);
               border-radius:8px;padding:8px 16px;font-size:13px;cursor:pointer;transition:.15s}}
  .analyze-all:hover{{background:rgba(0,194,255,.18)}}
</style>
</head>
<body>
<div class="topbar">
  <div>
    <h1>Atlyz Admin Panel</h1>
    <div class="sub">{len(clients)} client{"s" if len(clients)!=1 else ""} &nbsp;·&nbsp; {total_leads} total leads &nbsp;·&nbsp; {total_chats} total chats</div>
  </div>
  <button class="analyze-all" onclick="analyzeAll(this)">Analyze all FAQs</button>
</div>

<div class="summary">
  <div class="scard"><div class="n">{len(clients)}</div><div class="l">Clients</div></div>
  <div class="scard"><div class="n">{sum(1 for c in clients if c['plan']=='starter')}</div><div class="l">Starter</div></div>
  <div class="scard"><div class="n">{sum(1 for c in clients if c['plan']=='growth')}</div><div class="l">Growth</div></div>
  <div class="scard"><div class="n">{sum(1 for c in clients if c['plan']=='pro')}</div><div class="l">Pro</div></div>
  <div class="scard"><div class="n">{total_chats}</div><div class="l">Total chats</div></div>
  <div class="scard"><div class="n">{total_leads}</div><div class="l">Total leads</div></div>
</div>

<div class="grid">
{cards_html}
</div>

<script>
const KEY = {repr(admin_key)};

async function analyzeFAQs(bid, btn) {{
  btn.disabled = true; btn.textContent = 'Analyzing…';
  const msg = document.getElementById('msg-' + bid);
  msg.className = 'msg'; msg.style.display = 'none';
  try {{
    const r = await fetch('/admin/analyze-faqs/' + bid, {{
      method:'POST', headers:{{'X-Admin-Key': KEY}}
    }});
    const d = await r.json();
    if(r.ok) {{
      msg.textContent = d.count + ' questions found'; msg.className='msg ok'; msg.style.display='';
      // Refresh the FAQ section in the card
      const card = document.getElementById('card-' + bid);
      const faqDiv = card.querySelector('.faqs');
      if(faqDiv && d.faqs && d.faqs.length) {{
        const rows = d.faqs.slice(0,10).map(f =>
          '<div class="faq-item"><span class="faq-count">×' + f.count + '</span>' + f.question + '</div>'
        ).join('');
        faqDiv.innerHTML = '<div class="faq-label">Top FAQs</div>' + rows;
      }}
    }} else {{
      msg.textContent = d.error || 'Failed'; msg.className='msg bad'; msg.style.display='';
    }}
  }} catch(e) {{
    msg.textContent = 'Error'; msg.className='msg bad'; msg.style.display='';
  }}
  btn.disabled = false; btn.textContent = 'Analyze FAQs';
}}

async function analyzeAll(btn) {{
  btn.disabled = true; btn.textContent = 'Analyzing all…';
  try {{
    const r = await fetch('/admin/analyze-all-faqs', {{
      method:'POST', headers:{{'X-Admin-Key': KEY}}
    }});
    const d = await r.json();
    if(r.ok) {{ btn.textContent = 'Done — ' + d.processed + ' processed'; }}
    else {{ btn.textContent = 'Failed'; }}
  }} catch(e) {{ btn.textContent = 'Error'; }}
  setTimeout(()=>{{ btn.disabled=false; btn.textContent='Analyze all FAQs'; }}, 3000);
}}
</script>
</body>
</html>"""

    from flask import Response
    return Response(html, mimetype="text/html")


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
        # New businesses start unpaid — the chatbot stays blocked until Paddle
        # confirms payment via the /webhooks/paddle endpoint (sets plan_status=active).
        f.write("plan_status = pending\n")
        if password:
            f.write(f"owner_password_hash = {generate_password_hash(password)}\n")

    # Always write structured owner info (used by bot and admin panel)
    acct_data    = load_accounts().get(account_email or owner_email, {})
    owner_name   = acct_data.get("name",  "") or (data.get("owner_name",  "") or "").strip()
    owner_phone  = acct_data.get("phone", "") or (data.get("phone",       "") or "").strip()
    meta_lines   = [
        f"Owner Name: {owner_name}",
        f"Email: {owner_email}",
        f"Company: {business_name}",
        f"Website: {website_url}",
    ]
    if owner_phone:
        meta_lines.append(f"Phone: {owner_phone}")
    meta_lines += [f"Plan: {plan}", f"Joined: {datetime.now().strftime('%Y-%m-%d')}"]
    owner_info_content = "\n".join(meta_lines)
    if owner_info:
        owner_info_content += "\n\n" + owner_info
    with open(os.path.join(config_dir, "owner_info.txt"), "w", encoding="utf-8") as f:
        f.write(owner_info_content)

    if not greeting:
        greeting = f"Hi! I'm {data.get('bot_name', 'Assistant')}, the virtual assistant for {business_name}. How can I help you today?"

    chatbot_cfg = {
        "primary_color":   primary_color,
        "secondary_color": "#f3f4f6",
        "icon":            "default",
        "greeting":        greeting,
        "language_lock":   None,
        "business_name":   business_name,
        "bot_name":        data.get("bot_name", "Assistant"),
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
# PADDLE BILLING WEBHOOK
# Verifies payment before a chatbot is allowed to serve live chats.
# ═══════════════════════════════════════════════════════════════════════════════

WEBHOOK_LOG = os.path.join(DATA_DIR, "webhook_log.txt")

# Paddle price id → Atlyz plan tier. Mirrors the PLANS map in setup.html.
# Override/extend via env: PADDLE_PRICE_STARTER / _GROWTH / _PRO.
PADDLE_PRICE_TO_PLAN = {
    os.getenv("PADDLE_PRICE_STARTER", "pri_01ktv8m6sc51nacw4t6afpkbrr"): "starter",
    os.getenv("PADDLE_PRICE_GROWTH",  "pri_01ktv9m3efz4bn96xk3sfs9g67"): "growth",
    os.getenv("PADDLE_PRICE_PRO",     "pri_01ktvads8c13403e4efh8yhqhs"): "pro",
}


def _log_webhook(line: str):
    try:
        os.makedirs(os.path.dirname(WEBHOOK_LOG), exist_ok=True)
        stamp = datetime.now().isoformat()
        with _disk_lock:
            with open(WEBHOOK_LOG, "a", encoding="utf-8") as f:
                f.write(f"[{stamp}] {line}\n")
    except Exception as e:
        print(f"[WEBHOOK LOG ERROR] {e}")


def verify_paddle_signature(raw_body: bytes, signature_header: str, secret: str) -> bool:
    """Verify a Paddle Billing 'Paddle-Signature' header.

    Header format:  ts=<unix>;h1=<hex hmac-sha256>
    Signed payload: "<ts>:<raw request body>" hashed with the webhook secret.
    """
    if not secret or not signature_header:
        return False
    ts, h1 = "", ""
    for part in signature_header.split(";"):
        k, _, v = part.partition("=")
        k = k.strip()
        if k == "ts":
            ts = v.strip()
        elif k == "h1":
            h1 = v.strip()
    if not ts or not h1:
        return False
    signed_payload = ts.encode("utf-8") + b":" + raw_body
    expected = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, h1)


def _extract_customer_email(data: dict) -> str:
    """Pull a customer email out of a Paddle event payload (best effort)."""
    obj = data.get("data", {}) or {}
    for path in (("customer", "email"), ("billing_details", "email")):
        node = obj
        for key in path:
            node = (node or {}).get(key) if isinstance(node, dict) else None
        if node:
            return str(node).strip().lower()
    # transaction.completed includes a top-level customer email in some payloads
    cust  = obj.get("customer")
    email = obj.get("email") or (cust.get("email", "") if isinstance(cust, dict) else "")
    return str(email or "").strip().lower()


def _plan_from_paddle_items(data: dict) -> str:
    """Map the purchased price id(s) to an Atlyz plan, or '' if unknown."""
    obj   = data.get("data", {}) or {}
    items = obj.get("items", []) or []
    for it in items:
        price = it.get("price", {}) if isinstance(it, dict) else {}
        pid   = price.get("id") or it.get("price_id") or ""
        if pid in PADDLE_PRICE_TO_PLAN:
            return PADDLE_PRICE_TO_PLAN[pid]
    return ""


def _resolve_webhook_business(data: dict, email: str) -> str:
    """Find which business a Paddle event applies to.

    Priority: custom_data.business_id (most reliable) → account.businesses by
    email (prefer a pending one) → any business whose owner_email matches.
    """
    obj         = data.get("data", {}) or {}
    custom_data = obj.get("custom_data") or {}
    bid = (custom_data.get("business_id") or "").strip()
    if bid and business_exists(bid):
        return bid

    if email:
        account = load_accounts().get(email)
        if account:
            owned = [b for b in account.get("businesses", []) if business_exists(b)]
            pending = [b for b in owned
                       if (load_business_config(b).get("plan_status", "") or "").strip().lower() == "pending"]
            candidates = pending or owned
            if candidates:
                # Most recently created/modified wins when several exist.
                candidates.sort(key=lambda b: os.path.getmtime(
                    os.path.join(client_dir(b), "config", "business_config.txt")), reverse=True)
                return candidates[0]
        # Fallback: scan every business for a matching owner_email.
        for b in list_all_bids():
            if load_business_config(b).get("owner_email", "").strip().lower() == email:
                return b
    return ""


@app.route("/webhooks/paddle", methods=["POST"])
def paddle_webhook():
    raw_body  = request.get_data()
    signature = request.headers.get("Paddle-Signature", "")
    secret    = os.getenv("PADDLE_WEBHOOK_SECRET", "")

    if not secret:
        # Fail closed in production — never trust an unverified billing event.
        if not DEV_MODE:
            _log_webhook("REJECTED: PADDLE_WEBHOOK_SECRET not configured")
            return jsonify({"error": "Webhook secret not configured"}), 500
        print("[PADDLE] DEV_MODE: skipping signature verification (no secret set)")
    elif not verify_paddle_signature(raw_body, signature, secret):
        _log_webhook("REJECTED: invalid Paddle-Signature")
        return jsonify({"error": "Invalid signature"}), 400

    try:
        data = json.loads(raw_body.decode("utf-8") or "{}")
    except Exception:
        _log_webhook("REJECTED: malformed JSON body")
        return jsonify({"error": "Malformed JSON"}), 400

    event_type = data.get("event_type", "unknown")
    sub_status = ((data.get("data") or {}).get("status") or "").strip().lower()
    email      = _extract_customer_email(data)
    bid        = _resolve_webhook_business(data, email)
    _log_webhook(f"event={event_type} status={sub_status or '-'} email={email or '-'} business={bid or 'UNRESOLVED'}")

    SUB_ACTIVATE  = {"subscription.activated", "subscription.created", "subscription.resumed"}
    DEACTIVATE    = {"subscription.canceled": "canceled", "subscription.past_due": "past_due",
                     "subscription.paused": "paused"}
    LIVE_STATUSES = {"active", "trialing"}

    if not bid:
        # Acknowledge so Paddle stops retrying, but record that we couldn't match it.
        return jsonify({"ok": True, "matched": False})

    # transaction.completed = payment captured → always live.
    # subscription.* activation events only go live when the subscription is
    # genuinely active/trialing — a sub created in past_due/paused (e.g. a failed
    # first charge) must NOT serve chats, or checkout could be bypassed.
    if event_type == "transaction.completed":
        go_live = True
    elif event_type in SUB_ACTIVATE:
        go_live = (not sub_status) or (sub_status in LIVE_STATUSES)
    else:
        go_live = None

    if go_live is True:
        plan = _plan_from_paddle_items(data) or business_plan(bid)
        set_business_config_field(bid, "plan", plan)
        set_business_config_field(bid, "plan_status", "active")
        _log_webhook(f"ACTIVATED business={bid} plan={plan}")
    elif go_live is False:
        set_business_config_field(bid, "plan_status", sub_status or "inactive")
        _log_webhook(f"BLOCKED business={bid} status={sub_status or 'inactive'}")
    elif event_type in DEACTIVATE:
        status = DEACTIVATE[event_type]
        set_business_config_field(bid, "plan_status", status)
        _log_webhook(f"DEACTIVATED business={bid} status={status}")

    return jsonify({"ok": True, "matched": True, "business_id": bid})


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
    ATLYZ_BID   = "atlyz"
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
            sections_cache.pop(ATLYZ_BID, None)
            print(f"[AIS] Knowledge auto-updated from {len(pages_content)} pages ✓")

        except Exception as e:
            print(f"[AIS] Auto-scrape failed: {e}")
        finally:
            _ais_ready = True

    threading.Thread(target=run, daemon=True, name="ais-auto-scrape").start()


# ═══════════════════════════════════════════════════════════════════════════════
# DEMO STORE SEED (startup)
# Guarantees the public demo business (atlyz.com/demo) always exists and is live —
# even on a fresh Railway deploy with an empty/ephemeral filesystem. Without this,
# the demo's /chat/start returns 404 "Unknown business" and the widget loops on
# "Reconnecting…". The content is version-controlled here (not in the gitignored
# clients/ dir) so it can never drift or be wiped between deploys.
# ═══════════════════════════════════════════════════════════════════════════════

DEMO_BID = "stride_sneakers"

_DEMO_BUSINESS_CONFIG = (
    "business_name = Stride Sneakers\n"
    "owner_email = hello@stridesneakers.com\n"
    "website = https://www.stridesneakers.com\n"
    "plan = pro\n"
    "plan_status = active\n"
)

_DEMO_CHATBOT_CONFIG = {
    "primary_color":   "#00C2FF",
    "secondary_color": "#f3f4f6",
    "icon":            "default",
    "greeting":        "Hi! I'm Maya from Stride Sneakers 👟 Looking for a particular pair, a size, or a brand — or just browsing? Ask me anything.",
    "language_lock":   None,
    "business_name":   "Stride Sneakers",
    "bot_name":        "Maya",
    "bot_tagline":     "Stride Sneakers Assistant",
    "collect_leads":   True,
    "widget_position": "bottom-right",
    "white_label":     False,
    "color_mode":      "manual",
    "logo_mode":       "default",
}

_DEMO_KNOWLEDGE = """Source: Stride Sneakers (Atlyz demo store)
Business: Stride Sneakers

ABOUT
Stride Sneakers is a specialty sneaker store based in Portland, Oregon, fitting
runners, collectors, and everyday wearers since 2015. We sell online worldwide
and from our Portland flagship. Our team knows sneakers inside out and helps you
find the right pair for your feet, your sport, and your style.

BRANDS & PRODUCTS
We carry Nike (Air Max, Air Force 1, Pegasus), Jordan, Adidas (Ultraboost,
Samba, Gazelle), New Balance (550, 990 series), ASICS, Puma, Vans, and Converse.
Categories: running shoes, lifestyle/casual sneakers, basketball shoes, and
limited-edition releases. Available in men's, women's, and kids' sizes.

POPULAR & IN STOCK
- Nike Air Max 90 and Air Max 270 — in stock in most sizes including US size 10,
  in several colorways.
- Air Jordan 1 — select colorways.
- Adidas Ultraboost and Samba — wide size range.
- New Balance 550 and 990v6.
Sizes run US 4-14 (men's) and US 5-12 (women's). If a size is running low we can
hold a pair for you or notify you when it's back.

PRICING
Sneakers from $90. Most running and lifestyle models are $90-$180. Limited and
premium releases are $200+. We price-match authorized retailers.

SHIPPING
Free standard shipping on US orders over $75 (2-4 business days). Express
shipping is available at checkout. We ship internationally to most countries;
international delivery is typically 5-12 business days, with any duties or taxes
shown at checkout.

RETURNS & EXCHANGES
30-day free returns and exchanges on unworn shoes in their original box. Return
shipping is free within the US. Ordered the wrong size? We'll send the right one
at no extra cost.

PAYMENT
We accept all major cards, Apple Pay, Google Pay, and Klarna (pay in 4).

STORE HOURS (Portland flagship)
Monday-Saturday 9am-8pm, Sunday 11am-6pm. The online store is open 24/7.

CONTACT
Phone: (503) 555-0148. Email: hello@stridesneakers.com.
Flagship: 120 SW Stride Ave, Portland, OR.
For order status, sizing help, holds, or anything you can't find here, just ask
to speak to someone and we'll take your details so the team can follow up.
"""


def seed_demo_business():
    """Write the demo store's config + knowledge into clients/<DEMO_BID> at startup.

    Idempotent: rewrites the files every boot so the demo content always matches
    what ships in this file, regardless of the deploy's filesystem state.
    """
    try:
        base = client_dir(DEMO_BID)
        if not base:
            print(f"[DEMO] Invalid demo bid {DEMO_BID!r} — skipping seed")
            return
        config_dir = os.path.join(base, "config")
        os.makedirs(config_dir, exist_ok=True)
        with open(os.path.join(config_dir, "business_config.txt"), "w", encoding="utf-8") as f:
            f.write(_DEMO_BUSINESS_CONFIG)
        with open(os.path.join(config_dir, "chatbot_config.json"), "w", encoding="utf-8") as f:
            json.dump(_DEMO_CHATBOT_CONFIG, f, indent=2)
        with open(os.path.join(config_dir, "knowledge.txt"), "w", encoding="utf-8") as f:
            f.write(_DEMO_KNOWLEDGE)
        knowledge_cache.pop(DEMO_BID, None)
        sections_cache.pop(DEMO_BID, None)
        print(f"[DEMO] Seeded demo business {DEMO_BID!r} ✓")
    except Exception as e:
        print(f"[DEMO] Seed failed: {e}")


# Runs at import (works under both `python chatbot_server.py` and gunicorn).
seed_demo_business()
auto_scrape_atlyz_website()


if __name__ == "__main__":
    print("=" * 50)
    print("  ATLYZ — Chat Server")
    print(f"  API:  http://localhost:{os.environ.get('PORT', 5002)}")
    print("  Test: /chat/test/quickfix_plumbing")
    print("=" * 50)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5002)), debug=DEV_MODE)
