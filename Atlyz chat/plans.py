# plans.py — Atlyz Chat plan tiers
# Feature matrix mirrors the pricing section on the Atlyz website.
# monthly_chats = None means unlimited.

PLANS = {
    "starter": {
        "label":         "Starter",
        "max_websites":  1,
        "monthly_chats": 500,
        "lead_capture":  False,
        "analytics":     False,
        "white_label":   False,
        "custom_logo":   True,
        "auto_color":    True,
        "scrape_pages":  25,
    },
    "growth": {
        "label":         "Growth",
        "max_websites":  1,
        "monthly_chats": 1000,
        "lead_capture":  True,
        "analytics":     True,
        "white_label":   False,
        "custom_logo":   True,
        "auto_color":    True,
        "scrape_pages":  50,
    },
    "pro": {
        "label":         "Pro",
        "max_websites":  1,
        "monthly_chats": 3000,
        "lead_capture":  True,
        "analytics":     True,
        "white_label":   True,
        "custom_logo":   True,
        "auto_color":    True,
        "scrape_pages":  50,
    },
}

DEFAULT_PLAN = "starter"


def normalize_plan(plan: str) -> str:
    plan = (plan or "").strip().lower()
    return plan if plan in PLANS else DEFAULT_PLAN


def get_plan(plan: str) -> dict:
    """Return the feature dict for a plan name (falls back to Starter)."""
    return PLANS[normalize_plan(plan)]


def feature(plan: str, key: str):
    return get_plan(plan).get(key)
