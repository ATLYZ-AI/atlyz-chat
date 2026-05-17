# CLAUDE.md — Atlyz Project Guide

> This file is the single source of truth for Claude Code working on this project.
> Read this fully before making any changes.

---

## What is Atlyz

Atlyz is an AI SaaS company being built by a solo 16-year-old developer from Peshawar, Pakistan.

The company has three products planned:

| Product | Status | Description |
|---|---|---|
| **Atlyz Chat** | ✅ Built, testing | AI chatbot widget for any website |
| **Atlyz Voice** | 🔧 Built, needs telephony | AI phone receptionist for businesses |
| **Atlyz Agent** | 📋 Roadmap | Full AI automation agent |

**Target market:** Small service businesses in USA, UK, Europe — plumbers, salons, clinics, e-commerce shops.

**Pricing:** $49/month Starter, $99/month Growth, $149/month Pro.

**Tech stack:** Python, Flask, Flask-SocketIO, OpenAI API (GPT-4.1-nano, Whisper, TTS nova voice), vanilla JS widget, HTML/CSS dashboard.

---

## Project Structure

```
/home/syed/PROJECTS/
├── Chat Bot/              ← Atlyz Chat (current working directory)
│   ├── chatbot_server.py  ← Main Flask server, all API routes
│   ├── scraper.py         ← Website scraper + GPT summarizer
│   ├── config.py          ← Central config, loads from .env
│   ├── widget.js          ← THIS IS IN static/ folder
│   ├── static/
│   │   └── widget.js      ← Embeddable JS widget for any website
│   ├── templates/
│   │   └── chat_test.html ← Test page showing widget embedded
│   ├── notifications/
│   │   ├── email.py       ← Email alerts to owner
│   │   ├── whatsapp.py    ← WhatsApp alerts to owner
│   │   └── notification.py← Central notification handler
│   ├── clients/           ← Per-client data (DO NOT COMMIT)
│   │   └── <business_id>/
│   │       ├── config/
│   │       │   ├── knowledge.txt       ← Scraped website knowledge
│   │       │   ├── business_config.txt ← business_name, owner info
│   │       │   └── chatbot_config.json ← Widget colors, icon, greeting
│   │       └── data/
│   │           └── leads.csv           ← Captured customer leads
│   ├── ATLYZ website/     ← Company website HTML files
│   │   ├── index.html
│   │   ├── about.html
│   │   ├── blog.html
│   │   ├── careers.html
│   │   ├── contact.html
│   │   ├── privacy.html
│   │   ├── terms.html
│   │   ├── cookies.html
│   │   ├── chat-product.html
│   │   ├── voice-product.html
│   │   └── agent-product.html
│   ├── venv/              ← Virtual environment (DO NOT COMMIT)
│   ├── .env               ← API keys (DO NOT COMMIT EVER)
│   ├── Procfile           ← Railway deployment: web: python chatbot_server.py
│   └── requirements_chatbot.txt
│
└── receptionist/          ← Atlyz Voice (separate product)
    ├── receptionist_core.py   ← AI conversation engine
    ├── ai_summary.py          ← GPT data cleaner
    ├── ai_receptionist_engine.py
    ├── voice_server.py        ← WebSocket voice server
    ├── web_app_finalv4.py     ← Owner dashboard
    ├── config.py
    ├── core/
    │   ├── voice.py
    │   └── ai_receptionist_engine.py
    └── notifications/
```

---

## Running Atlyz Chat

```bash
# Activate virtual environment
source venv/bin/activate

# Install dependencies (first time only)
pip install -r requirements_chatbot.txt

# Run server
python chatbot_server.py

# Test in browser
# http://localhost:5002/chat/test/test_shop
```

---

## Environment Variables (.env)

```
OPENAI_API_KEY=sk-proj-...
EMAIL_FROM=hello@atlyz.com
EMAIL_PASSWORD=...
DEV_MODE=true
SECRET_KEY=...
```

---

## How Atlyz Chat Works

### Full flow:

```
Owner signs up
        ↓
Enters website URL → scraper.py reads up to 15 pages
        ↓
GPT summarizes into knowledge.txt
        ↓
Owner copies embed code from dashboard:
<script src="atlyz.com/widget.js?id=business_id"></script>
        ↓
Customer visits their website → widget appears bottom right
        ↓
Customer asks question → POST /chat/message
        ↓
ai_chat_response() sends message + knowledge + history to GPT-4.1-nano
        ↓
GPT returns JSON: {reply, action, language}
        ↓
If action="collect_lead" → lead form appears
        ↓
Lead saved to leads.csv → owner notified via email + WhatsApp
```

### API Routes in chatbot_server.py:

| Route | Method | Purpose |
|---|---|---|
| `/chat/start` | POST | Start session, return greeting |
| `/chat/message` | POST | Handle customer message |
| `/chat/lead` | POST | Save customer lead |
| `/chat/config/<id>` | GET | Get widget config |
| `/chatbot/scrape/<id>` | POST | Trigger website scrape |
| `/chatbot/config/<id>` | POST | Save widget appearance |
| `/chatbot/stats/<id>` | GET | Chat stats |
| `/chat/test/<id>` | GET | Test page |
| `/widget.js` | GET | Serve widget script |

---

## What's Already Built ✅

### Atlyz Chat:
- [x] Full chatbot server (Flask)
- [x] Smart AI response — answers from knowledge base
- [x] Auto-detects customer language, responds in it
- [x] Lead capture when AI can't answer
- [x] Website scraper — reads any site in 60 seconds
- [x] Embeddable widget.js — one script tag
- [x] Widget customization — color, icon, position
- [x] Knowledge caching for performance
- [x] Conversation history (last 6 exchanges)
- [x] Test page with fake shop
- [x] ngrok tested and working

### Atlyz Voice:
- [x] Full conversation engine (receptionist_core.py)
- [x] 7-question call flow
- [x] GPT-based input classification
- [x] Knowledge base Q&A
- [x] Call storage to CSV
- [x] Email + WhatsApp notifications
- [x] Owner dashboard with analytics
- [x] Web voice interface (WebSockets + Whisper + TTS)
- [x] Console test mode working
- [x] Blocked: needs US telephony (Telnyx/Twilio) — age/payment barrier

### Company Website (ATLYZ website/ folder):
- [x] index.html — homepage with hero, bento grid, pricing
- [x] about.html
- [x] blog.html
- [x] careers.html (has Telecom Specialist job listing)
- [x] contact.html — form POSTs to `/contact` route on chatbot_server.py
- [x] privacy.html
- [x] terms.html
- [x] cookies.html
- [x] chat-product.html
- [x] voice-product.html
- [x] agent-product.html
- [x] 404.html
- [x] Mobile hamburger menu — all pages

---

## What's NOT Done Yet ❌

### Immediate priorities:

1. ~~**Mobile hamburger menu**~~ ✅ Done — all 8 pages have hamburger nav
2. ~~**404 page**~~ ✅ Done — `404.html` created
3. ~~**Contact form backend**~~ ✅ Done — `POST /contact` route in chatbot_server.py; sends email via Gmail SMTP if `EMAIL_FROM`/`EMAIL_PASSWORD` are set
4. ~~**Fix index.html product links**~~ ✅ Done — links point to `chat-product.html`, `voice-product.html`, `agent-product.html` (all created)
5. ~~**Demo page**~~ ✅ Done — `demo.html` created with self-contained interactive demo (QuickFix Plumbing simulation, no server required)
6. **GitHub setup** — needs new account with atlyz.com email
7. **Railway deployment** — not deployed yet, running on ngrok only
8. **Domain purchase** — atlyz.com not bought yet
9. **Business email** — needs atlyz.com domain first

### Atlyz Chat improvements needed:
1. Session persistence — sessions lost on server restart
2. Rate limiting — no protection against spam
3. Analytics — total chats counter not implemented (returns 0)
4. Multi-business dashboard — currently one business per install

### Atlyz Voice:
1. Telephony integration — needs US partner or Payoneer card
2. Production deployment

---

## Deployment Plan

### Target: Railway.app

```
Procfile already created: web: python chatbot_server.py

Steps:
1. Create GitHub account with atlyz.com email
2. Push Chat Bot project to GitHub (with .gitignore)
3. Connect Railway to GitHub repo
4. Add environment variables in Railway dashboard
5. Deploy — get URL like atlyz-chat.up.railway.app
6. Update widget.js SERVER_URL to Railway URL
7. Buy atlyz.com domain on Namecheap
8. Point domain to Railway
```

### .gitignore must include:
```
.env
venv/
clients/
__pycache__/
*.pyc
*.pyo
.DS_Store
*.log
```

---

## Important Rules When Editing Code

1. **Never hardcode API keys** — always use `os.getenv()` and `.env` file
2. **GPT model** — use `gpt-4.1-nano` for chat/classification, it supports `max_completion_tokens` not `max_tokens`
3. **JSON parsing** — always use 3-layer parsing (direct → strip markdown → regex) for GPT responses
4. **Temperature** — gpt-4.1-nano only supports default temperature, never set `temperature=0`
5. **Session key** — dashboard uses `session[business_id] = True` not `session['user']`
6. **Virtual environment** — always activate `venv/` before running, not `atlyz-env`
7. **Port** — chatbot runs on 5002, dashboard (receptionist) runs on 5000

---

## Business Context

- Solo developer, 16 years old, Pakistan
- No telephony access yet (Telnyx/Twilio blocked by age/payment)
- Budget: ~Rs.9,000 PKR for infrastructure
- Goal: First paying client ASAP
- Monthly costs when live: Claude Pro $20 + Railway $5 + Domain $1 + Email $1 = ~$27/month
- Break-even: 1 client at $49/month covers everything

---

## Key Files to Know

| File | What it does |
|---|---|
| `chatbot_server.py` | Main brain — all routes and AI logic |
| `scraper.py` | Website auto-reader |
| `static/widget.js` | What gets embedded on client websites |
| `templates/chat_test.html` | Demo/test page |
| `notifications/notification.py` | Central notification handler |
| `ATLYZ website/index.html` | Company homepage |
| `.env` | API keys — never touch in code |

---

## Current Status Summary

Atlyz Chat is **working and tested**. The chatbot:
- Reads any website automatically
- Answers customer questions in any language
- Captures leads and notifies owner
- Widget embeds on any website with 1 line of code
- Publicly accessible via ngrok

**Next milestone:** Deploy to Railway, buy atlyz.com, get first client.
