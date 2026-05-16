# config.py — Atlyz environment config
import os
from dotenv import load_dotenv

load_dotenv()

OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY", "")
EMAIL_FROM      = os.getenv("EMAIL_FROM", "")
EMAIL_PASSWORD  = os.getenv("EMAIL_PASSWORD", "")
SECRET_KEY      = os.getenv("SECRET_KEY", "atlyz-chat-secret")
ATLYZ_ADMIN_KEY = os.getenv("ATLYZ_ADMIN_KEY", "")
DEV_MODE        = os.getenv("DEV_MODE", "false").lower() == "true"
PORT            = int(os.getenv("PORT", 5002))
