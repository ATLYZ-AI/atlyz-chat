# scraper.py — Atlyz Website Scraper
# Automatically reads a client's website and extracts business knowledge
# Used during onboarding — owner enters URL, Atlyz learns everything

import os
import re
import json
import time
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


def scrape_website(base_url: str) -> dict:
    """
    Smart website scraper:
    1. Tries common paths first
    2. Crawls links found on homepage
    3. Summarizes everything with GPT
    """
    # Normalize URL
    if not base_url.startswith("http"):
        base_url = "https://" + base_url
    base_url = base_url.rstrip("/")

    parsed = urlparse(base_url)
    if not parsed.netloc:
        return {"status": "error", "error": "Invalid URL", "content": "", "pages_scraped": 0}

    all_content = []
    visited = set()
    pages_scraped = 0
    max_pages = 15  # cap to avoid huge prompts

    print(f"[SCRAPER] Starting smart scrape of {base_url}")

    # ── Phase 1: Try common paths ──
    priority_urls = [base_url + p for p in TARGET_PATHS]

    # ── Phase 2: Crawl homepage links ──
    try:
        homepage_html = requests.get(base_url, headers=HEADERS, timeout=8).text
        found_links = extract_links(homepage_html, base_url)
        useful_links = [l for l in found_links if is_useful_page(l)]
        print(f"[SCRAPER] Found {len(useful_links)} useful links on homepage")
    except Exception:
        useful_links = []

    # Combine: priority first, then discovered links, deduplicated
    all_urls = []
    seen = set()
    for url in priority_urls + useful_links:
        if url not in seen:
            all_urls.append(url)
            seen.add(url)

    # ── Scrape each URL ──
    for url in all_urls:
        if pages_scraped >= max_pages:
            print(f"[SCRAPER] Reached {max_pages} page limit")
            break

        if url in visited:
            continue
        visited.add(url)

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
            pages_scraped += 1
            print(f"[SCRAPER] ✅ {path} ({len(text)} chars)")
        else:
            print(f"[SCRAPER] ⬜ {path} — empty or not found")

        time.sleep(0.3)

    if not all_content:
        return {
            "status": "error",
            "error": "Could not read any pages from this website.",
            "content": "",
            "pages_scraped": 0,
            "url": base_url
        }

    combined = "\n".join(all_content)
    summarized = summarize_with_ai(combined, base_url)

    return {
        "status": "ok",
        "error": None,
        "url": base_url,
        "pages_scraped": pages_scraped,
        "content": summarized or combined[:8000]
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
            model="gpt-4.1-nano",
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
def save_scraped_knowledge(business_id: str, scraped: dict) -> bool:
    """Save scraped knowledge to client config directory."""
    try:
        config_dir = os.path.join("clients", business_id, "config")
        os.makedirs(config_dir, exist_ok=True)

        # Save knowledge file
        knowledge_path = os.path.join(config_dir, "knowledge.txt")
        with open(knowledge_path, "w", encoding="utf-8") as f:
            f.write(f"Source: {scraped['url']}\n")
            f.write(f"Scraped: {scraped['pages_scraped']} pages\n\n")
            f.write(scraped["content"])

        # Save scrape metadata
        meta_path = os.path.join(config_dir, "scrape_meta.json")
        with open(meta_path, "w") as f:
            json.dump({
                "url": scraped["url"],
                "pages_scraped": scraped["pages_scraped"],
                "status": scraped["status"],
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
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
