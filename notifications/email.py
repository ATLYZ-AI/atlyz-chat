# notifications/email.py
import smtplib
from email.message import EmailMessage
from config import DEV_MODE, SMTP_SERVER, SMTP_PORT, EMAIL_FROM, EMAIL_PASSWORD


def send_email_alert(email, business_name, call):
    """Send email alert to owner for new calls."""

    if DEV_MODE:
        print(f"\n[DEV MODE] Email alert simulated")
        print(f"To: {email}")
        print(f"URGENT CALL – {business_name}")
        return True

    if not EMAIL_FROM or not EMAIL_PASSWORD:
        print("[EMAIL ERROR] EMAIL_FROM or EMAIL_PASSWORD not set in environment.")
        return False

    try:
        msg = EmailMessage()
        msg["From"]    = EMAIL_FROM
        msg["To"]      = email
        msg["Subject"] = f"📞 New Call – {business_name}"

        msg.set_content(f"""
NEW CALL RECEIVED
═══════════════════════════════
Business:       {business_name}
Service:        {call.get("service", "")}
Address:        {call.get("address", "Not provided")}
Name:           {call.get("name", "")}
Phone:          {call.get("phone", "")}
Email:          {call.get("email", "Not provided")}
Preferred Time: {call.get("preferred_time", "")}
Urgent:         {call.get("urgent", "")}
Call ID:        {call.get("call_id", "")}
═══════════════════════════════
""")

        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(EMAIL_FROM, EMAIL_PASSWORD)
            server.send_message(msg)

        return True

    except Exception as e:
        print(f"[EMAIL ERROR] {e}")
        return False
