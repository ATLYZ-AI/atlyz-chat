# API Keys & Secrets — Reference

> ⚠️ **This file is documentation only. It must NEVER contain real keys.**
> Real secret values live ONLY in `.env` (which is gitignored and never pushed to GitHub).
> Putting a real key in any committed file = bots find it on GitHub within minutes and
> run up charges / get the key revoked. Always use placeholders here.

This lists every key the project needs, where to get it, and where it goes.
To set up: copy the values into your local `.env` file (create it if missing).

---

## Required

### OpenAI — `OPENAI_API_KEY`
- **What it's for:** all AI (GPT-5-nano chat + replies, website summarization). Without it nothing works.
- **Where to get it:** https://platform.openai.com/api-keys → "Create new secret key"
- **Format:** `sk-proj-...`
- **.env line:** `OPENAI_API_KEY=sk-proj-your-real-key-here`
- **Cost:** pay-as-you-go per token. Set a monthly usage limit in the OpenAI dashboard to avoid surprises.

### Flask session secret — `SECRET_KEY`
- **What it's for:** signs login sessions. Required in production.
- **Where to get it:** generate your own — run:
  `python -c "import secrets; print(secrets.token_hex(32))"`
- **.env line:** `SECRET_KEY=paste-the-generated-hex-here`
- **Cost:** free (you generate it).

### Admin key — `ATLYZ_ADMIN_KEY`
- **What it's for:** gates `/setup/create` and the scrape route in production. Pick any long random string.
- **Where to get it:** make one up (or use the same generator as SECRET_KEY).
- **.env line:** `ATLYZ_ADMIN_KEY=some-long-random-string`
- **Note:** if you set this in production, the public signup→setup flow must send it too, or `/setup/create` will reject. Leave unset only in dev.
- **Cost:** free.

---

## Email notifications (Gmail)

### `EMAIL_FROM` and `EMAIL_PASSWORD`
- **What it's for:** sends lead alerts and contact-form messages to the owner.
- **Where to get it:** Gmail account → enable 2-Step Verification → create an **App Password**
  at https://myaccount.google.com/apppasswords (NOT your normal Gmail password).
- **.env lines:**
  `EMAIL_FROM=contact@atlyz.com`
  `EMAIL_PASSWORD=your-16-char-gmail-app-password`
- **Cost:** free.

---

## Optional tuning (not secrets — safe defaults exist)

| Variable | Purpose | Default |
|---|---|---|
| `DEV_MODE` | `true` enables Flask debug. Set `false` in production. | `false` |
| `DATA_DIR` | Where accounts + client data are stored. **On Railway set to `/data`** (a mounted volume) so data survives deploys. | `.` (project dir) |
| `PORT` | Server port (Railway sets this automatically). | `5002` |
| `MAX_MESSAGE_CHARS` | Max chars per customer message. | `2000` |
| `MAX_KNOWLEDGE_CHARS` | Max chars of scraped knowledge sent to GPT. | `12000` |
| `RATE_LIMIT_MAX` | Messages allowed per session per window. | `20` |
| `RATE_LIMIT_WINDOW` | Rate-limit window in seconds. | `60` |
| `IP_RATE_LIMIT_MAX` | Messages allowed per IP. | `40` |

---

## Not used yet (future)

- **Stripe** (payments) — when real card payments replace the placeholder, keys will be
  `STRIPE_SECRET_KEY` / `STRIPE_PUBLISHABLE_KEY` from https://dashboard.stripe.com/apikeys
- **Telephony** (Atlyz Voice) — Twilio/Telnyx keys, once telephony access is sorted.

---

## Setup checklist

1. Create a file named `.env` in the project root (same folder as `chatbot_server.py`).
2. Add the **Required** keys above with your real values.
3. Add the **Email** keys if you want lead notifications.
4. Never commit `.env` — it's already in `.gitignore`. This `API_KEYS.md` (placeholders only) is fine to commit.
