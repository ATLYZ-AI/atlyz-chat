# scraper.py — Atlyz Website Scraper
# Automatically reads a client's website and extracts business knowledge
# Used during onboarding — owner enters URL, Atlyz learns everything

import os
import re
import json
import time
import hashlib
from collections import Counter
import requests
from urllib.parse import urljoin, urlparse
from dotenv import load_dotenv

load_dotenv()

# Pages worth scraping — most shops have these
TARGET_PATHS = [
    "/", "/faq", "/faqs", "/about", "/about-us",
    "/contact", "/contact-us", "/shipping", "/shipping-policy",
    "/returns", "/return-policy", "/refund-policy",
    "/products", "/services", "/pricing", "/offers",
    "/terms", "/privacy", "/help", "/support",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; AtlyzBot/1.0; +https://atlyz.ai/bot)"
}


# ══════════════════════════════════════════════
# CLEAN HTML → PLAIN TEXT
# ══════════════════════════════════════════════
def clean_html(html: str) -> str:
    """Strip HTML tags and clean up whitespace."""
    # Remove script and style blocks
    html = re.sub(r'<(script|style)[^>]*>.*?</(script|style)>', '', html, flags=re.DOTALL | re.IGNORECASE)
    # Remove HTML tags
    html = re.sub(r'<[^>]+>', ' ', html)
    # Decode common entities
    html = html.replace('&nbsp;', ' ').replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>').replace('&quot;', '"')
    # Clean whitespace
    html = re.sub(r'\s+', ' ', html).strip()
    return html


# ══════════════════════════════════════════════
# FETCH ONE PAGE
# ══════════════════════════════════════════════
def fetch_page(url: str, timeout: int = 8) -> str:
    """Fetch a single page and return clean text."""
    try:
        response = requests.get(url, headers=HEADERS, timeout=timeout)
        if response.status_code == 200:
            text = clean_html(response.text)
            # Limit per page to avoid huge prompts
            return text[:3000]
        return ""
    except Exception:
        return ""


# ══════════════════════════════════════════════
# SCRAPE ENTIRE WEBSITE
# ══════════════════════════════════════════════
def extract_links(html: str, base_url: str) -> list:
    """Extract all internal links from a page."""
    links = []
    # Find all href attributes
    hrefs = re.findall(r'href=["\']([^"\']+)["\']', html)
    parsed_base = urlparse(base_url)

    for href in hrefs:
        href = href.strip()
        if not href or href.startswith("#") or href.startswith("javascript"):
            continue
        # Make absolute URL
        if href.startswith("http"):
            full_url = href
        elif href.startswith("/"):
            full_url = f"{parsed_base.scheme}://{parsed_base.netloc}{href}"
        else:
            full_url = f"{base_url.rstrip('/')}/{href}"

        # Only keep same domain links
        if parsed_base.netloc in full_url:
            # Clean URL — remove query params and fragments
            clean = full_url.split("?")[0].split("#")[0].rstrip("/")
            if clean not in links and clean != base_url.rstrip("/"):
                links.append(clean)

    return links


def is_useful_page(url: str) -> bool:
    """Check if a URL is worth scraping."""
    url_lower = url.lower()
    # Skip these
    skip = [".jpg", ".jpeg", ".png", ".gif", ".svg", ".ico", ".css", ".js",
            ".pdf", ".zip", ".mp4", ".woff", "cart", "checkout", "login",
            "register", "account", "wishlist", "compare", "tag/", "page/",
            "wp-admin", "wp-content", "wp-json", "feed", "sitemap", "cdn"]
    if any(s in url_lower for s in skip):
        return False
    # Prefer these
    useful = ["faq", "about", "contact", "shipping", "return", "refund",
              "policy", "product", "service", "pricing", "offer", "deal",
              "help", "support", "collection", "category", "store", "shop"]
    # Accept if useful keyword found OR if it's a short path (main pages)
    path = urlparse(url).path
    path_depth = len([p for p in path.split("/") if p])
    return any(u in url_lower for u in useful) or path_depth <= 2


def extract_brand_color(html: str) -> str:
    """Best-effort brand color from a page: theme-color meta first, else the most
    common saturated (non-neutral) hex color. Returns '#rrggbb' or '' if none."""
    if not html:
        return ""

    # 1. <meta name="theme-color"> is the strongest signal
    m = re.search(r'<meta[^>]+name=["\']theme-color["\'][^>]+content=["\']\s*(#[0-9a-fA-F]{3,6})', html, re.I)
    if not m:
        m = re.search(r'<meta[^>]+content=["\']\s*(#[0-9a-fA-F]{3,6})["\'][^>]+name=["\']theme-color["\']', html, re.I)
    if m:
        c = _norm_hex(m.group(1))
        if c:
            return c

    # 2. Frequency of saturated hex colors across the markup/CSS
    counts = Counter()
    for raw in re.findall(r'#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b', html):
        c = _norm_hex(raw)
        if c and _is_brandy(c):
            counts[c] += 1
    if counts:
        return counts.most_common(1)[0][0]
    return ""


def _norm_hex(hex_str: str) -> str:
    h = hex_str.strip().lstrip("#").lower()
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    if len(h) != 6 or any(ch not in "0123456789abcdef" for ch in h):
        return ""
    return "#" + h


def _is_brandy(hex_color: str) -> bool:
    """True for colors with enough saturation/contrast to be a brand color
    (excludes white, black, and grays)."""
    r = int(hex_color[1:3], 16)
    g = int(hex_color[3:5], 16)
    b = int(hex_color[5:7], 16)
    mx, mn = max(r, g, b), min(r, g, b)
    if mx > 240 and mn > 240:   # near white
        return False
    if mx < 28:                 # near black
        return False
    if mx - mn < 28:            # gray
        return False
    return True


def scrape_website(base_url: str, max_pages: int = 50) -> dict:
    """
    Smart website scraper:
    1. Seeds from common paths + homepage links
    2. Breadth-first crawls internal pages up to max_pages
    3. Captures raw page text + brand color, then summarizes with GPT
    """
    if not base_url.startswith("http"):
        base_url = "https://" + base_url
    base_url = base_url.rstrip("/")

    parsed = urlparse(base_url)
    if not parsed.netloc:
        return {"status": "error", "error": "Invalid URL", "content": "", "pages_scraped": 0}

    print(f"[SCRAPER] Starting crawl of {base_url} (up to {max_pages} pages)")

    # Fetch homepage once — used for seeding links + brand color
    homepage_html = ""
    try:
        homepage_html = requests.get(base_url, headers=HEADERS, timeout=8).text
    except Exception:
        pass
    brand_color = extract_brand_color(homepage_html)
    if brand_color:
        print(f"[SCRAPER] Detected brand color: {brand_color}")

    # Seed queue: common paths first, then homepage links (BFS frontier)
    queue = [base_url + p for p in TARGET_PATHS]
    for link in extract_links(homepage_html, base_url):
        if is_useful_page(link):
            queue.append(link)

    visited = set()
    all_content = []
    raw_pages   = []
    pages_scraped = 0

    while queue and pages_scraped < max_pages:
        url = queue.pop(0)
        norm = url.split("#")[0].split("?")[0].rstrip("/")
        if norm in visited:
            continue
        visited.add(norm)

        raw_html = ""
        try:
            response = requests.get(url, headers=HEADERS, timeout=8)
            if response.status_code == 200:
                raw_html = response.text
        except Exception:
            pass

        text = clean_html(raw_html)
        path = urlparse(url).path or "/"

        if text and len(text) > 150:
            all_content.append(f"=== Page: {path} ===\n{text[:3000]}\n")
            raw_pages.append(f"=== {url} ===\n{text}\n")
            pages_scraped += 1
            print(f"[SCRAPER] ✅ {path} ({len(text)} chars) [{pages_scraped}/{max_pages}]")

            # Expand frontier with links discovered on this page
            for link in extract_links(raw_html, base_url):
                ln = link.split("#")[0].split("?")[0].rstrip("/")
                if ln not in visited and is_useful_page(link):
                    queue.append(link)

        time.sleep(0.2)

    if not all_content:
        return {
            "status": "error",
            "error": "Could not read any pages from this website.",
            "content": "",
            "pages_scraped": 0,
            "url": base_url
        }

    combined = "\n".join(all_content)
    raw_text = "\n".join(raw_pages)
    summarized = summarize_with_ai(combined, base_url)
    content_hash = hashlib.sha256(raw_text.encode("utf-8", "ignore")).hexdigest()

    return {
        "status": "ok",
        "error": None,
        "url": base_url,
        "pages_scraped": pages_scraped,
        "content": summarized or combined[:8000],
        "raw_pages": raw_text,
        "brand_color": brand_color,
        "content_hash": content_hash,
    }


# ══════════════════════════════════════════════
# AI SUMMARIZER — clean scraped content
# ══════════════════════════════════════════════
def summarize_with_ai(raw_content: str, website_url: str) -> str:
    """Use GPT to summarize scraped content into clean knowledge."""
    try:
        from openai import OpenAI
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

        prompt = f"""You are extracting business knowledge from a scraped website.

Website: {website_url}
Scraped content:
{raw_content[:6000]}

Extract and organize all useful business information into clear sections:
- Business name and description
- Products or services offered
- Pricing (if mentioned)
- Shipping and delivery info
- Return/refund policy
- Contact information
- FAQs
- Current offers or promotions
- Hours of operation
- Any other important customer information

Write it clearly so an AI chatbot can use it to answer customer questions.
Remove any navigation text, cookie notices, or irrelevant content.
Keep it factual and concise."""

        response = client.chat.completions.create(
            model="gpt-5-nano",
            messages=[
                {"role": "system", "content": "You extract and organize business information from website content. Be factual and concise."},
                {"role": "user", "content": prompt}
            ],
            max_completion_tokens=1500
        )
        return response.choices[0].message.content.strip()

    except Exception as e:
        print(f"[SCRAPER AI ERROR] {e}")
        return ""


# ══════════════════════════════════════════════
# SAVE SCRAPED KNOWLEDGE
# ══════════════════════════════════════════════
def save_scraped_knowledge(business_id: str, scraped: dict, clients_dir: str = "clients") -> bool:
    """Replace the client's scraped knowledge with a fresh scrape.

    Old knowledge.txt and scraped_pages.txt are overwritten (deleted + rewritten),
    so a re-scrape after a website update always reflects the latest content.
    """
    try:
        config_dir = os.path.join(clients_dir, business_id, "config")
        os.makedirs(config_dir, exist_ok=True)

        # AI-ready summary (what the bot reads as scraped knowledge)
        knowledge_path = os.path.join(config_dir, "knowledge.txt")
        with open(knowledge_path, "w", encoding="utf-8") as f:
            f.write(f"Source: {scraped['url']}\n")
            f.write(f"Scraped: {scraped['pages_scraped']} pages\n\n")
            f.write(scraped["content"])

        # Raw per-page text — "scraped pages (knowledge)" archive
        raw_pages = scraped.get("raw_pages", "")
        scraped_path = os.path.join(config_dir, "scraped_pages.txt")
        if raw_pages:
            with open(scraped_path, "w", encoding="utf-8") as f:
                f.write(raw_pages)
        elif os.path.exists(scraped_path):
            os.remove(scraped_path)

        meta_path = os.path.join(config_dir, "scrape_meta.json")
        with open(meta_path, "w") as f:
            json.dump({
                "url":           scraped["url"],
                "pages_scraped": scraped["pages_scraped"],
                "status":        scraped["status"],
                "brand_color":   scraped.get("brand_color", ""),
                "content_hash":  scraped.get("content_hash", ""),
                "timestamp":     time.strftime("%Y-%m-%d %H:%M:%S")
            }, f, indent=2)

        return True
    except Exception as e:
        print(f"[SCRAPER SAVE ERROR] {e}")
        return False


# ══════════════════════════════════════════════
# MAIN — test from command line
# ══════════════════════════════════════════════
if __name__ == "__main__":
    import sys
    url = sys.argv[1] if len(sys.argv) > 1 else "https://example.com"
    result = scrape_website(url)
    print(f"\nStatus: {result['status']}")
    print(f"Pages scraped: {result['pages_scraped']}")
    print(f"\nContent preview:\n{result['content'][:500]}")
