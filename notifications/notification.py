# notifications/notification.py
import os
from datetime import datetime
from notifications.email import send_email_alert
from notifications.whatsapp import send_whatsapp_alert

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def log_notification(business_id, message):
    """Write to local log file."""
    log_path = os.path.join(
        BASE_DIR, "clients", business_id, "logs", "owner_notifications.txt"
    )
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"\n{message}\n")


def notify_owner(business_id, call):
    """
    Central notification function.
    Reads contacts from business_config.txt and sends all alerts.
    """
    config_path = os.path.join(
        BASE_DIR, "clients", business_id, "config", "business_config.txt"
    )

    # Load config
    config = {}
    if os.path.exists(config_path):
        with open(config_path) as f:
            for line in f:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    config[k.strip()] = v.strip()

    business_name  = config.get("business_name", business_id)
    owner_emails   = [e.strip() for e in config.get("owner_emails", "").split(",") if e.strip()]
    owner_whatsapp = [n.strip() for n in config.get("owner_whatsapp_numbers", "").split(",") if n.strip()]

    # Add timestamp if missing
    if "timestamp" not in call:
        call["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    summary = (
        f"\nNEW MISSED CALL\n"
        f"Business : {business_name}\n"
        f"Time     : {call['timestamp']}\n"
        f"Service  : {call.get('service','N/A')}\n"
        f"Name     : {call.get('name','N/A')}\n"
        f"Phone    : {call.get('phone','N/A')}\n"
        f"Urgent   : {call.get('urgent','NO')}\n"
        f"Status   : {call.get('status','NEW')}\n"
    )

    # Always log
    log_notification(business_id, summary)

    # Email all owners
    for email in owner_emails:
        send_email_alert(email, business_name, call)

    # WhatsApp all owners
    for number in owner_whatsapp:
        send_whatsapp_alert(business_name, call, number)
