# notifications/whatsapp.py
# Currently: DEV MODE prints to console
# Production: plug in WhatsApp Business API (Meta) or Twilio WhatsApp sandbox
import os

DEV_MODE = os.getenv("DEV_MODE", "true").lower() == "true"


def send_whatsapp_alert(business_name, call, number):
    """Send WhatsApp notification to owner."""

    message = (
        f"📞 *New Missed Call — {business_name}*\n\n"
        f"🆔 Call ID : {call.get('call_id', 'N/A')}\n"
        f"👤 Name    : {call.get('name', 'N/A')}\n"
        f"📱 Phone   : {call.get('phone', 'N/A')}\n"
        f"🛠 Service : {call.get('service', 'N/A')}\n"
        f"⏰ Time    : {call.get('preferred_time', 'N/A')}\n"
        f"🚨 Urgent  : {call.get('urgent', 'NO')}\n\n"
        f"📝 {call.get('summary', '')}"
    )

    if DEV_MODE:
        print(f"\n[WHATSAPP — DEV MODE]")
        print(f"  To: {number}")
        print(message)
        return True

    # ── PRODUCTION: Add your WhatsApp API here ──
    # Option 1: Meta WhatsApp Business API
    # Option 2: Twilio WhatsApp (separate from voice)
    # We will plug this in once telephony is working
    print(f"[WHATSAPP] Production not configured yet")
    return False
