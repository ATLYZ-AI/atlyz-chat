#!/usr/bin/env python3
"""
cleanup_test_clients.py — remove duplicate test/junk client entries from storage.

Mirrors chatbot_server.py's storage layout:
    DATA_DIR      = os.getenv("DATA_DIR", <this script's dir>)
    CLIENTS_DIR   = DATA_DIR/clients/<bid>/...
    ACCOUNTS_FILE = DATA_DIR/accounts/accounts.json

Run on Railway (where DATA_DIR=/data is set in the environment).

    DRY RUN (default — deletes NOTHING, just lists matches):
        python cleanup_test_clients.py

    ACTUALLY DELETE:
        python cleanup_test_clients.py --delete

Matches (any one is enough to flag a client as junk):
  * owner email (or owning-account email) == e8recp91@gmail.com
  * bid / business_name / website is a variant of nh mushroom / nh mashroom
    (nhmushrooms, nhmashroom, nh-mushrooms-7, etc.)
  * an entry named "name" with website ourfashionboutique.com

PROTECTED bids are hard-excluded by id and can NEVER be deleted, no matter what.
"""

import os
import re
import sys
import json
import shutil

# ─── Storage paths — identical logic to chatbot_server.py ──────────────────────
BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
DATA_DIR      = os.getenv("DATA_DIR", BASE_DIR)
CLIENTS_DIR   = os.path.join(DATA_DIR, "clients")
ACCOUNTS_DIR  = os.path.join(DATA_DIR, "accounts")
ACCOUNTS_FILE = os.path.join(ACCOUNTS_DIR, "accounts.json")

# ─── Hard protection — these can NEVER be deleted ──────────────────────────────
# Both hyphen and underscore spellings of atlyz-website are listed defensively.
PROTECTED_BIDS = {"atlyz", "atlyz-website", "atlyz_website", "stride_sneakers"}

# ─── Match criteria ────────────────────────────────────────────────────────────
TEST_EMAIL       = "e8recp91@gmail.com"
MUSHROOM_RE      = re.compile(r"nhm[au]shroom")      # nh mushroom / mashroom variants
FASHION_HOST     = "ourfashionboutique"


def norm(s: str) -> str:
    """Lowercase and strip everything but a-z0-9 — for fuzzy id/name/host matching."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


# ─── Storage readers — mirror chatbot_server.py ────────────────────────────────
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
    with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        json.dump(accounts, f, indent=2)


def load_business_config(bid: str) -> dict:
    """Parse clients/<bid>/config/business_config.txt (key = value lines)."""
    config = {}
    path = os.path.join(CLIENTS_DIR, bid, "config", "business_config.txt")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                if "=" in line:
                    k, v = line.split("=", 1)
                    config[k.strip()] = v.strip()
    return config


def read_total_chats(bid: str) -> int:
    path = os.path.join(CLIENTS_DIR, bid, "data", "stats.json")
    if os.path.exists(path):
        try:
            with open(path) as f:
                return int(json.load(f).get("total_chats", 0) or 0)
        except Exception:
            pass
    return 0


def build_bid_to_email(accounts: dict) -> dict:
    """Reverse-map every bid to the account email that owns it."""
    out = {}
    for email, acct in accounts.items():
        for bid in (acct.get("businesses") or []):
            out[bid] = email
    return out


# ─── Matching ──────────────────────────────────────────────────────────────────
def match_reasons(bid, business_name, website, owner_email, account_email):
    reasons = []

    emails = {e.strip().lower() for e in (owner_email, account_email) if e}
    if TEST_EMAIL in emails:
        reasons.append(f"email {TEST_EMAIL}")

    nb, nn, nw = norm(bid), norm(business_name), norm(website)
    if MUSHROOM_RE.search(nb) or MUSHROOM_RE.search(nn) or MUSHROOM_RE.search(nw):
        reasons.append("nh-mushroom/mashroom variant")

    name_is_name = (business_name or "").strip().lower() == "name" or bid.lower() == "name"
    if FASHION_HOST in nw and name_is_name:
        reasons.append('"name" + ourfashionboutique.com')

    return reasons


def main():
    do_delete = "--delete" in sys.argv[1:]

    if not os.path.isdir(CLIENTS_DIR):
        print(f"[ERROR] CLIENTS_DIR does not exist: {CLIENTS_DIR}")
        sys.exit(1)

    print(f"DATA_DIR      = {DATA_DIR}")
    print(f"CLIENTS_DIR   = {CLIENTS_DIR}")
    print(f"ACCOUNTS_FILE = {ACCOUNTS_FILE}")
    print(f"PROTECTED     = {sorted(PROTECTED_BIDS)}")
    print(f"MODE          = {'DELETE' if do_delete else 'DRY RUN (no changes)'}")
    print("=" * 78)

    accounts     = load_accounts()
    bid_to_email = build_bid_to_email(accounts)

    bids = sorted(
        d for d in os.listdir(CLIENTS_DIR)
        if os.path.isdir(os.path.join(CLIENTS_DIR, d))
    )

    candidates = []  # (bid, name, email, website, chats, reasons)
    for bid in bids:
        if bid in PROTECTED_BIDS:
            continue
        cfg           = load_business_config(bid)
        business_name = cfg.get("business_name", "")
        website       = cfg.get("website", "")
        owner_email   = cfg.get("owner_email", "")
        account_email = bid_to_email.get(bid, "")
        chats         = read_total_chats(bid)

        reasons = match_reasons(bid, business_name, website, owner_email, account_email)
        if reasons:
            email = owner_email or account_email or "(none)"
            candidates.append((bid, business_name or "(none)", email,
                               website or "(none)", chats, reasons))

    if not candidates:
        print("No matching test/junk clients found. Nothing to do.")
        return

    print(f"Found {len(candidates)} matching client(s):\n")
    for bid, name, email, website, chats, reasons in candidates:
        print(f"  • bid={bid}")
        print(f"      name    : {name}")
        print(f"      email   : {email}")
        print(f"      website : {website}")
        print(f"      chats   : {chats}")
        print(f"      matched : {', '.join(reasons)}")
    print(f"\nTOTAL matching: {len(candidates)}")

    if not do_delete:
        print("\nDRY RUN — nothing was deleted. Re-run with --delete to remove these.")
        return

    # ─── Deletion ──────────────────────────────────────────────────────────────
    print("\n" + "=" * 78)
    print("DELETING…")
    deleted = []
    for bid, name, email, website, chats, reasons in candidates:
        # Belt-and-suspenders: never delete a protected bid.
        if bid in PROTECTED_BIDS:
            print(f"  [SKIP] {bid} is protected — refusing to delete.")
            continue
        path = os.path.join(CLIENTS_DIR, bid)
        try:
            shutil.rmtree(path)
            deleted.append(bid)
            print(f"  [DELETED] client dir: {path}")
        except Exception as e:
            print(f"  [ERROR] could not delete {path}: {e}")

    # Unlink deleted bids from any accounts, and drop now-empty test accounts.
    if deleted:
        changed = False
        for email in list(accounts.keys()):
            acct = accounts[email]
            before = acct.get("businesses") or []
            after  = [b for b in before if b not in deleted]
            if after != before:
                acct["businesses"] = after
                changed = True
                print(f"  [ACCOUNT] removed {len(before) - len(after)} bid(s) "
                      f"from account {email}")
            # Remove the empty test account itself.
            if email.strip().lower() == TEST_EMAIL and not acct.get("businesses"):
                del accounts[email]
                changed = True
                print(f"  [ACCOUNT] deleted empty test account {email}")
        if changed:
            save_accounts(accounts)
            print(f"  [SAVED] {ACCOUNTS_FILE}")

    print("\n" + "=" * 78)
    print(f"DELETED {len(deleted)} client(s): {deleted if deleted else '(none)'}")


if __name__ == "__main__":
    main()
