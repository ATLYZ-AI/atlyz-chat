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
from bs4 import BeautifulSoup, NavigableString, Tag
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

# Tags that never hold reader-facing content — dropped before text/section extraction
NOISE_TAGS = ["script", "style", "noscript", "template", "svg", "iframe"]


def _normalize_ws(text: str) -> str:
    """Collapse all runs of whitespace to single spaces and trim."""
    return re.sub(r"\s+", " ", text or "").strip()


# ══════════════════════════════════════════════
# CLEAN HTML → PLAIN TEXT  (BeautifulSoup + lxml)
# ══════════════════════════════════════════════
def clean_html(html: str) -> str:
    """Strip HTML to clean, whitespace-normalized plain text.

    BeautifulSoup (lxml parser) replaces the old regex approach: it decodes
    entities, drops noise tags, and yields cleaner text. Same contract as before
    (HTML string in → plain text out), so the knowledge.txt / scraped_pages.txt
    fallback artifacts keep working unchanged.
    """
    if not html:
        return ""
    try:
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(NOISE_TAGS):
            tag.decompose()
        return _normalize_ws(soup.get_text(" "))
    except Exception:
        # Last-resort regex fallback so a parser hiccup never kills a scrape
        stripped = re.sub(r"<[^>]+>", " ", html)
        return _normalize_ws(stripped)


# ══════════════════════════════════════════════
# PAGE STRUCTURE → SECTIONS  (BeautifulSoup + lxml)
# ══════════════════════════════════════════════
HEADING_TAGS = {"h1", "h2", "h3"}


def _within_heading(node) -> bool:
    """True when a text node lives inside an h1/h2/h3 — i.e. it is the heading's
    own label, not the body text that follows the heading."""
    return any(getattr(parent, "name", None) in HEADING_TAGS for parent in node.parents)


def extract_page_sections(html: str, page_url: str, fallback_title: str = "") -> dict:
    """Parse one page into structured sections.

    Returns:
        {
          "page_url":   str,
          "page_title": str,                       # <title>, else fallback_title
          "sections": [
            {"heading": str, "level": int,         # level = 1|2|3 (0 = lead/fallback)
             "subheadings": [str, ...],            # deeper headings nested under this one
             "text": str},                         # body text up to the next heading
            ...
          ]
        }

    Each h1/h2/h3 opens a section whose `text` is the body content following it up
    to the next heading of any level (the literal "heading + following text" model).
    `subheadings` cross-references the deeper-level headings that fall under a
    section. A page with no headings falls back to one section titled by the page
    title holding the whole page's text.
    """
    try:
        soup = BeautifulSoup(html or "", "lxml")
        for tag in soup(NOISE_TAGS):
            tag.decompose()

        title_tag = soup.title
        page_title = (title_tag.get_text(" ", strip=True) if title_tag else "") or fallback_title

        # Drop <head> so meta/title text can't leak in as body when <body> is absent
        if soup.head:
            soup.head.decompose()
        body = soup.body or soup

        raw = []          # flat sections in document order
        current = None

        def close(sec):
            if sec is None:
                return
            sec["text"] = _normalize_ws(" ".join(sec.pop("_parts")))
            if sec["heading"] or sec["text"]:
                raw.append(sec)

        for node in body.descendants:
            if isinstance(node, Tag):
                if node.name in HEADING_TAGS:
                    close(current)
                    current = {"heading": node.get_text(" ", strip=True),
                               "level": int(node.name[1]), "_parts": []}
                continue
            if isinstance(node, NavigableString):
                chunk = str(node).strip()
                if not chunk or _within_heading(node):
                    continue
                if current is None:                       # text before the first heading
                    current = {"heading": "", "level": 0, "_parts": []}
                current["_parts"].append(chunk)
        close(current)

        # No real heading anywhere → treat the whole page as a single section
        if not any(sec["level"] > 0 for sec in raw):
            whole = _normalize_ws(body.get_text(" "))
            sections = ([{"heading": page_title, "level": 0, "subheadings": [], "text": whole}]
                        if whole else [])
            return {"page_url": page_url, "page_title": page_title, "sections": sections}

        # Cross-reference deeper headings as each section's subheadings
        n = len(raw)
        for i, sec in enumerate(raw):
            if sec["level"] == 0:
                sec["subheadings"] = []
                continue
            subs = []
            for j in range(i + 1, n):
                if raw[j]["level"] <= sec["level"]:
                    break
                if raw[j]["heading"]:
                    subs.append(raw[j]["heading"])
            sec["subheadings"] = subs

        sections = [{"heading": sec["heading"], "level": sec["level"],
                     "subheadings": sec["subheadings"], "text": sec["text"]} for sec in raw]
        return {"page_url": page_url, "page_title": page_title, "sections": sections}

    except Exception as e:
        # One malformed page must never crash the whole crawl
        print(f"[SCRAPER SECTIONS ERROR] {page_url}: {e}")
        text = clean_html(html)
        title = fallback_title or page_url
        sections = [{"heading": title, "level": 0, "subheadings": [], "text": text}] if text else []
        return {"page_url": page_url, "page_title": title, "sections": sections}


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


# Overall wall-clock budget for a single scrape. Without this, an unreachable
# but slow-to-time-out host could block for (seed paths × per-request timeout)
# ≈ 150s, hanging the synchronous /setup/create request. Tunable via env.
MAX_SCRAPE_SECONDS = int(os.getenv("MAX_SCRAPE_SECONDS", 40))


def scrape_website(base_url: str, max_pages: int = 50, max_seconds: int = MAX_SCRAPE_SECONDS) -> dict:
    """
    Smart website scraper:
    1. Seeds from common paths + homepage links
    2. Breadth-first crawls internal pages up to max_pages
    3. Captures raw page text + brand color, then summarizes with GPT

    Bounded by both max_pages and an overall max_seconds wall-clock budget so a
    slow or unreachable host can never hang the caller.
    """
    if not base_url.startswith("http"):
        base_url = "https://" + base_url
    base_url = base_url.rstrip("/")

    parsed = urlparse(base_url)
    if not parsed.netloc:
        return {"status": "error", "error": "Invalid URL", "content": "", "pages_scraped": 0}

    deadline = time.time() + max_seconds
    print(f"[SCRAPER] Starting crawl of {base_url} (up to {max_pages} pages, {max_seconds}s budget)")

    # Fetch homepage once — used for seeding links + brand color
    homepage_html = ""
    try:
        homepage_html = requests.get(base_url, headers=HEADERS, timeout=8).text
    except Exception as e:
        print(f"[SCRAPER] Homepage fetch failed: {e}")
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
    page_sections = []   # structured {page_url, page_title, sections:[...]} per page
    pages_scraped = 0

    while queue and pages_scraped < max_pages:
        if time.time() > deadline:
            print(f"[SCRAPER] ⏱ Time budget ({max_seconds}s) reached — stopping at {pages_scraped} pages")
            break
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
            # Structured sections artifact (additive — does not affect the text above)
            page_sections.append(extract_page_sections(raw_html, url, fallback_title=path))
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
        "sections": page_sections,
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

        # Structured per-page sections — NEW artifact for upcoming section-selection.
        # Additive only: the bot still answers from knowledge.txt for now.
        sections = scraped.get("sections", [])
        sections_path = os.path.join(config_dir, "knowledge_sections.json")
        if sections:
            with open(sections_path, "w", encoding="utf-8") as f:
                json.dump(sections, f, indent=2, ensure_ascii=False)
        elif os.path.exists(sections_path):
            os.remove(sections_path)

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

    pages = result.get("sections", [])
    total = sum(len(p["sections"]) for p in pages)
    print(f"\nSections artifact: {len(pages)} pages, {total} sections total")
    if pages:
        print("\nSample (first page) ─────────────────────────────")
        print(json.dumps(pages[0], indent=2, ensure_ascii=False)[:1500])
        # Optional: dump the full artifact for inspection
        if "--dump" in sys.argv:
            out = "knowledge_sections.sample.json"
            with open(out, "w", encoding="utf-8") as f:
                json.dump(pages, f, indent=2, ensure_ascii=False)
            print(f"\nFull artifact written to {out}")
