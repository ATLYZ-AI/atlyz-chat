# config.py — Atlyz Chat environment config
import os
from dotenv import load_dotenv

load_dotenv()

OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY", "")
EMAIL_FROM      = os.getenv("EMAIL_FROM", "")
EMAIL_PASSWORD  = os.getenv("EMAIL_PASSWORD", "")
SECRET_KEY      = os.getenv("SECRET_KEY", "")
ATLYZ_ADMIN_KEY = os.getenv("ATLYZ_ADMIN_KEY", "")
DEV_MODE        = os.getenv("DEV_MODE", "false").lower() == "true"
PORT            = int(os.getenv("PORT", 5002))

# ── Abuse / cost guards ─────────────────────────────────────────────────────────
MAX_MESSAGE_CHARS   = int(os.getenv("MAX_MESSAGE_CHARS", 2000))   # per customer message
MAX_KNOWLEDGE_CHARS = int(os.getenv("MAX_KNOWLEDGE_CHARS", 12000)) # injected into prompt
RATE_LIMIT_MAX      = int(os.getenv("RATE_LIMIT_MAX", 20))   # messages per window per session
RATE_LIMIT_WINDOW   = int(os.getenv("RATE_LIMIT_WINDOW", 60))
IP_RATE_LIMIT_MAX   = int(os.getenv("IP_RATE_LIMIT_MAX", 40))  # messages per window per IP
