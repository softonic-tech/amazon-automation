"""
Product Web Scraper
-------------------
A polite, configurable scraper for e-commerce product pages
(Zoro.com, Grainger, Home Depot, etc.).

Usage:
    python scraper.py --url https://www.zoro.com/some-product/i/G1234567/
    python scraper.py --file urls.txt --out products.json
    python scraper.py --url https://... --format csv --out products.csv

Notes on responsible use:
  * Always check the site's robots.txt and Terms of Service.
  * Keep request rates low (this tool defaults to 1 request every 2s).
  * Identify your scraper honestly in the User-Agent when appropriate.
  * Many large retailers use anti-bot services (Akamai, Cloudflare, PerimeterX).
    If you're blocked, consider their official API/affiliate feed instead.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

from sitemap import SitemapCrawler
from amazon import AmazonSearcher, compare_zoro_to_amazon, ComparisonRow
from sourcing import SourcingConfig, build_sourcing_rows, save_sourcing_xlsx, save_sourcing_csv
from dataclasses import asdict

# Optional clients — only imported when actually used
def _brand_to_url_slugs(brand: str) -> list[str]:
    """Return every URL-slug variant iHerb might use for this brand name.

    iHerb's actual convention (verified from real product URLs like
    ``/pr/doctor-s-best-msm-...`` and ``/pr/nature-s-way-...``):
      * spaces        → hyphen
      * apostrophe    → hyphen  (kept as a separator, NOT stripped)
      * ``&``         → ``and``
      * ``.``, ``,``  → dropped
      * runs of hyphens collapsed to a single hyphen

    Because retailers aren't 100% consistent, we also emit an
    apostrophe-stripped fallback (``Nature's Path`` → ``natures-path``) so
    older or inconsistently-slugged URLs still match.
    """
    if not brand:
        return []

    def _collapse(s: str) -> str:
        s = s.strip().lower().replace("&", "and")
        for ch in (".", ","):
            s = s.replace(ch, "")
        s = s.replace(" ", "-")
        while "--" in s:
            s = s.replace("--", "-")
        return s.strip("-")

    variants: list[str] = []
    # Primary: iHerb convention — apostrophe becomes a hyphen separator.
    primary = _collapse(brand.replace("'", "-").replace("\u2019", "-"))
    if primary:
        variants.append(primary)
    # Fallback: apostrophe stripped entirely.
    if "'" in brand or "\u2019" in brand:
        stripped = _collapse(brand.replace("'", "").replace("\u2019", ""))
        if stripped and stripped not in variants:
            variants.append(stripped)
    return variants


# Kept for backwards compatibility with anything that imported the old name.
def _brand_to_url_slug(brand: str) -> str:
    variants = _brand_to_url_slugs(brand)
    return variants[0] if variants else ""


def _prefilter_urls_by_brand(urls: list[str], brand_include: list[str],
                              base_host: str) -> tuple[list[str], int]:
    """
    Filter URLs by brand slug before scraping. Only works reliably for
    retailers that put brand names in the URL slug (iHerb does; Zoro doesn't).
    Returns (filtered_urls, num_dropped).
    """
    if not brand_include:
        return urls, 0
    # Only apply URL pre-filtering for iHerb — its URLs contain brand slugs
    if "iherb.com" not in base_host.lower():
        return urls, 0

    # Flatten: every brand may produce several candidate slugs.
    slugs: list[str] = []
    for b in brand_include:
        slugs.extend(_brand_to_url_slugs(b))
    # De-dupe while preserving order.
    seen: set[str] = set()
    slugs = [s for s in slugs if not (s in seen or seen.add(s))]
    logging.getLogger("scraper").info(
        "Brand URL slugs to match: %s", ", ".join(slugs) or "(none)"
    )

    kept = []
    for url in urls:
        url_lower = url.lower()
        if any(f"/pr/{s}-" in url_lower or f"/pr/{s}/" in url_lower for s in slugs):
            kept.append(url)
    dropped = len(urls) - len(kept)
    return kept, dropped


def _to_price(v) -> float | None:
    """Parse a price string like '$25.99' / '25.99' / '1,234.56' → float."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("$", "").replace(",", "").replace("USD", "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _normalize_brand_for_compare(s: str) -> str:
    """Lowercase + strip punctuation + collapse whitespace so ``Bob's Red Mill``
    and ``Bobs Red Mill`` compare equal (retailers aren't consistent about
    apostrophes in JSON-LD).

    Note: apostrophes are stripped, NOT converted to whitespace, so
    ``Bob's`` and ``Bobs`` both become ``bobs``.
    """
    if not s:
        return ""
    s = s.lower().replace("&", "and").replace("\u2019", "'")
    s = s.replace("'", "")  # strip apostrophes so "bob's" == "bobs"
    # Turn any remaining non-alnum char into a space, then collapse.
    cleaned = "".join(c if (c.isalnum() or c == " ") else " " for c in s)
    return " ".join(cleaned.split())


def _matches_brand_filter(product_dict: dict, include: list[str],
                           exclude: list[str]) -> bool:
    """Return True if the product's brand passes the include/exclude lists.
    Comparison is punctuation- and case-insensitive."""
    brand = _normalize_brand_for_compare(product_dict.get("brand") or "")
    if not brand:
        return False if include else True
    if include:
        if not any(_normalize_brand_for_compare(inc) in brand for inc in include):
            return False
    if exclude:
        if any(_normalize_brand_for_compare(exc) in brand for exc in exclude):
            return False
    return True


def _split_csv_arg(val: str | None) -> list[str]:
    if not val:
        return []
    return [x.strip() for x in val.split(",") if x.strip()]


def _load_playwright_client():
    try:
        from browser import PlaywrightClient
        return PlaywrightClient
    except ImportError as e:
        raise SystemExit(
            "\n--playwright requested but browser.py is missing from this folder.\n"
            "Save browser.py alongside scraper.py, or drop the --playwright flag.\n"
            f"Underlying error: {e}"
        )

def _load_curl_client():
    try:
        from curl_client import CurlClient
        return CurlClient
    except ImportError as e:
        raise SystemExit(
            "\n--curl requested but curl_client.py is missing from this folder.\n"
            "Save curl_client.py alongside scraper.py, or drop the --curl flag.\n"
            f"Underlying error: {e}"
        )

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("scraper")


# ---------- Data model ---------------------------------------------------- #

@dataclass
class Product:
    url: str
    title: str | None = None
    price: str | None = None
    currency: str | None = None
    sku: str | None = None
    brand: str | None = None
    availability: str | None = None
    description: str | None = None
    image: str | None = None
    breadcrumbs: list[str] = field(default_factory=list)
    specs: dict[str, str] = field(default_factory=dict)
    raw_jsonld: list[dict] = field(default_factory=list)


# ---------- HTTP client --------------------------------------------------- #

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


class HttpClient:
    """Thin requests wrapper with retries, jittered delay, and robots.txt check."""

    def __init__(
        self,
        delay: float = 2.0,
        timeout: int = 20,
        max_retries: int = 3,
        respect_robots: bool = True,
        headers: dict | None = None,
    ) -> None:
        self.session = requests.Session()
        self.session.headers.update(headers or DEFAULT_HEADERS)
        self.delay = delay
        self.timeout = timeout
        self.max_retries = max_retries
        self.respect_robots = respect_robots
        self._robots_cache: dict[str, RobotFileParser] = {}
        self._last_request = 0.0

    def _robots_ok(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parsed = urlparse(url)
        # robots.txt and sitemaps exist FOR crawlers — never gate them through
        # robots.txt itself (which would be circular for robots.txt, and defeats
        # the purpose of sitemaps).
        path_lower = parsed.path.lower()
        if path_lower.endswith("/robots.txt") or "sitemap" in path_lower:
            return True
        base = f"{parsed.scheme}://{parsed.netloc}"
        rp = self._robots_cache.get(base)
        if rp is None:
            rp = RobotFileParser()
            rp.set_url(f"{base}/robots.txt")
            try:
                rp.read()
            except Exception as e:  # noqa: BLE001
                log.warning("robots.txt read failed for %s: %s", base, e)
                self._robots_cache[base] = rp
                return True
            self._robots_cache[base] = rp
        return rp.can_fetch(self.session.headers.get("User-Agent", "*"), url)

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_request
        wait = self.delay - elapsed
        if wait > 0:
            time.sleep(wait + random.uniform(0, 0.5))  # jitter
        self._last_request = time.time()

    def _request(self, url: str):
        """Shared retry/throttle logic; returns a Response or None."""
        if not self._robots_ok(url):
            log.warning("Blocked by robots.txt: %s", url)
            return None

        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            try:
                resp = self.session.get(url, timeout=self.timeout)
                if resp.status_code == 200:
                    return resp
                if resp.status_code in (403, 429, 503):
                    backoff = 2 ** attempt + random.uniform(0, 1)
                    log.warning(
                        "%s on %s (attempt %d) — backing off %.1fs",
                        resp.status_code, url, attempt, backoff,
                    )
                    time.sleep(backoff)
                    continue
                log.error("HTTP %s for %s", resp.status_code, url)
                return None
            except requests.RequestException as e:
                log.warning("Request error (attempt %d): %s", attempt, e)
                time.sleep(2 ** attempt)
        return None

    def get(self, url: str) -> str | None:
        """Return response body as text, or None on failure."""
        resp = self._request(url)
        return resp.text if resp is not None else None

    def get_bytes(self, url: str) -> bytes | None:
        """Return response body as raw bytes — for gzipped sitemaps etc."""
        resp = self._request(url)
        return resp.content if resp is not None else None

    def set_cookie(self, name: str, value: str, domain: str | None = None) -> None:
        """Interface-parity with PlaywrightClient. Domain is ignored by requests."""
        self.session.cookies.set(name, value)

    def set_header(self, name: str, value: str) -> None:
        self.session.headers[name] = value

    def close(self) -> None:
        self.session.close()


# ---------- Parsing ------------------------------------------------------- #

class ProductParser:
    """
    Extract product data. Strategy:
      1. Prefer JSON-LD (schema.org/Product) — most reliable across sites.
      2. Fall back to OpenGraph and site-specific selectors.
    """

    # Site-specific selector overrides. Add more as you encounter new sites.
    SITE_SELECTORS: dict[str, dict[str, str]] = {
        "zoro.com": {
            "title": "h1[data-za='product-title'], h1.product-title, h1",
            "price": "[data-za='product-price'], .product-price, [class*='price']",
            "sku": "[data-za='product-sku'], .product-sku",
            "brand": "[data-za='product-brand'], .product-brand",
            "description": "[data-za='product-description'], .product-description",
            "specs_row": "table.specs tr, .product-specs tr, [class*='spec'] tr",
        },
        "iherb.com": {
            # JSON-LD covers most of this reliably; these are HTML fallbacks
            "title":       "h1#name, h1[data-part-number], h1",
            "price":       ".price-inner-text-wrapper .price, "
                           "#price, [data-testid='price'], .price",
            "sku":         "[data-testid='product-id'], #product-specs-list .part-no",
            "brand":       "#brand a, [data-testid='brand'] a, .brand-name",
            "description": "#product-overview, .product-description-content",
            "specs_row":   "#product-specs-list li, "
                           ".product-specs-list li, "
                           "table.product-details tr",
        },
        # Add other retailers here.
    }

    def parse(self, html: str, url: str) -> Product:
        soup = BeautifulSoup(html, "html.parser")
        product = Product(url=url)

        # 1. JSON-LD (most reliable)
        jsonld_blocks = self._extract_jsonld(soup)
        product.raw_jsonld = jsonld_blocks
        self._apply_jsonld(product, jsonld_blocks)

        # 2. OpenGraph fallbacks
        self._apply_opengraph(product, soup)

        # 3. Site-specific selectors
        host = urlparse(url).netloc.replace("www.", "")
        selectors = self.SITE_SELECTORS.get(host, {})
        self._apply_selectors(product, soup, selectors)

        # 4. Breadcrumbs (generic)
        product.breadcrumbs = self._extract_breadcrumbs(soup)

        return product

    # --- helpers ---------------------------------------------------------- #

    @staticmethod
    def _extract_jsonld(soup: BeautifulSoup) -> list[dict]:
        blocks: list[dict] = []
        for tag in soup.find_all("script", type="application/ld+json"):
            raw = tag.string or tag.get_text() or ""
            raw = raw.strip()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                # Some sites embed JS-escaped JSON; skip quietly
                continue
            if isinstance(data, list):
                blocks.extend(d for d in data if isinstance(d, dict))
            elif isinstance(data, dict):
                # Handle @graph wrapper
                if "@graph" in data and isinstance(data["@graph"], list):
                    blocks.extend(d for d in data["@graph"] if isinstance(d, dict))
                else:
                    blocks.append(data)
        return blocks

    @staticmethod
    def _apply_jsonld(product: Product, blocks: list[dict]) -> None:
        def is_product(b: dict) -> bool:
            t = b.get("@type")
            if isinstance(t, list):
                return any("Product" in str(x) for x in t)
            return "Product" in str(t or "")

        for b in blocks:
            if not is_product(b):
                continue
            product.title = product.title or b.get("name")
            product.sku = product.sku or b.get("sku") or b.get("mpn")
            product.description = product.description or b.get("description")

            # Model number (separate from SKU — used for Keepa matching)
            if not product.specs.get("Model"):
                model = b.get("mpn") or b.get("model")
                if model:
                    product.specs["Model"] = str(model)

            # UPC / GTIN — critical for Amazon ASIN matching via Keepa
            for gtin_key in ("gtin", "gtin13", "gtin12", "gtin14", "gtin8"):
                gtin = b.get(gtin_key)
                if gtin and not product.specs.get("UPC"):
                    product.specs["UPC"] = str(gtin)

            brand = b.get("brand")
            if isinstance(brand, dict):
                product.brand = product.brand or brand.get("name")
            elif isinstance(brand, str):
                product.brand = product.brand or brand

            image = b.get("image")
            if isinstance(image, list) and image:
                product.image = product.image or str(image[0])
            elif isinstance(image, str):
                product.image = product.image or image

            offers = b.get("offers")
            if isinstance(offers, list) and offers:
                offers = offers[0]
            if isinstance(offers, dict):
                product.price = product.price or str(
                    offers.get("price") or offers.get("lowPrice") or ""
                ) or None
                product.currency = product.currency or offers.get("priceCurrency")
                avail = offers.get("availability", "")
                if avail:
                    product.availability = product.availability or str(avail).split("/")[-1]
            break

    @staticmethod
    def _apply_opengraph(product: Product, soup: BeautifulSoup) -> None:
        def og(prop: str) -> str | None:
            tag = soup.find("meta", property=prop)
            return tag["content"].strip() if tag and tag.get("content") else None

        product.title = product.title or og("og:title")
        product.description = product.description or og("og:description")
        product.image = product.image or og("og:image")
        product.price = product.price or og("product:price:amount")
        product.currency = product.currency or og("product:price:currency")

    def _apply_selectors(
        self, product: Product, soup: BeautifulSoup, selectors: dict[str, str]
    ) -> None:
        def first_text(css: str) -> str | None:
            if not css:
                return None
            for sel in css.split(","):
                el = soup.select_one(sel.strip())
                if el:
                    return el.get_text(strip=True)
            return None

        product.title = product.title or first_text(selectors.get("title", ""))
        product.brand = product.brand or first_text(selectors.get("brand", ""))
        product.sku = product.sku or first_text(selectors.get("sku", ""))
        product.description = product.description or first_text(selectors.get("description", ""))

        if not product.price:
            raw = first_text(selectors.get("price", ""))
            if raw:
                # Extract numeric price if there's currency/text noise
                m = re.search(r"([£$€¥]?\s?\d[\d,]*\.?\d*)", raw)
                product.price = m.group(1).strip() if m else raw

        # Specs table
        spec_sel = selectors.get("specs_row", "")
        if spec_sel:
            for sel in spec_sel.split(","):
                for row in soup.select(sel.strip()):
                    cells = row.find_all(["th", "td"])
                    if len(cells) >= 2:
                        key = cells[0].get_text(strip=True)
                        val = cells[1].get_text(strip=True)
                        if key and val:
                            product.specs.setdefault(key, val)

    @staticmethod
    def _extract_breadcrumbs(soup: BeautifulSoup) -> list[str]:
        # Try schema.org BreadcrumbList in JSON-LD or microdata
        crumbs: list[str] = []
        for tag in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(tag.string or "")
            except (json.JSONDecodeError, TypeError):
                continue
            items = data if isinstance(data, list) else [data]
            for item in items:
                if isinstance(item, dict) and "BreadcrumbList" in str(item.get("@type", "")):
                    for el in item.get("itemListElement", []):
                        name = None
                        if isinstance(el, dict):
                            name = el.get("name") or (
                                el.get("item", {}).get("name")
                                if isinstance(el.get("item"), dict) else None
                            )
                        if name:
                            crumbs.append(name)
        if crumbs:
            return crumbs

        # Fallback: nav.breadcrumb / ol.breadcrumb
        nav = soup.select_one("nav.breadcrumb, ol.breadcrumb, .breadcrumbs")
        if nav:
            crumbs = [a.get_text(strip=True) for a in nav.find_all("a") if a.get_text(strip=True)]
        return crumbs


# ---------- Orchestrator -------------------------------------------------- #

class Scraper:
    def __init__(self, client: HttpClient | None = None) -> None:
        self.client = client or HttpClient()
        self.parser = ProductParser()

    def scrape_one(self, url: str) -> Product | None:
        log.info("Fetching %s", url)
        html = self.client.get(url)
        if html is None:
            return None
        return self.parser.parse(html, url)

    def scrape_many(self, urls: Iterable[str]) -> list[Product]:
        results: list[Product] = []
        for url in urls:
            p = self.scrape_one(url)
            if p:
                results.append(p)
        return results


# ---------- Output -------------------------------------------------------- #

def save_json(products: list[Product], path: Path) -> None:
    path.write_text(
        json.dumps([asdict(p) for p in products], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    log.info("Wrote %d products to %s", len(products), path)


def save_csv(products: list[Product], path: Path) -> None:
    if not products:
        log.warning("No products to write.")
        return
    fields = ["url", "title", "brand", "sku", "price", "currency",
              "availability", "image", "description", "breadcrumbs", "specs"]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for p in products:
            row = asdict(p)
            row["breadcrumbs"] = " > ".join(row["breadcrumbs"])
            row["specs"] = json.dumps(row["specs"], ensure_ascii=False)
            row.pop("raw_jsonld", None)
            w.writerow({k: row.get(k, "") for k in fields})
    log.info("Wrote %d products to %s", len(products), path)


def save_comparison_json(rows: list[ComparisonRow], path: Path) -> None:
    payload = []
    for r in rows:
        d = asdict(r)
        payload.append(d)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Wrote %d comparison rows to %s", len(rows), path)


def save_comparison_csv(rows: list[ComparisonRow], path: Path) -> None:
    """Wide CSV: one row per Zoro product with up to N Amazon columns."""
    if not rows:
        log.warning("No comparison rows to write.")
        return
    max_amz = max(len(r.amazon_results) for r in rows) or 1
    fields = ["zoro_title", "zoro_brand", "zoro_price", "zoro_sku",
              "zoro_url", "search_query", "captcha_hit",
              "cheapest_amazon_price", "price_delta_vs_zoro"]
    for i in range(1, max_amz + 1):
        fields += [f"amazon_{i}_title", f"amazon_{i}_price",
                   f"amazon_{i}_rating", f"amazon_{i}_reviews",
                   f"amazon_{i}_asin", f"amazon_{i}_url"]

    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            row = {
                "zoro_title": r.zoro_title,
                "zoro_brand": r.zoro_brand,
                "zoro_price": r.zoro_price,
                "zoro_sku": r.zoro_sku,
                "zoro_url": r.zoro_url,
                "search_query": r.search_query,
                "captcha_hit": r.captcha_hit,
                "cheapest_amazon_price": r.cheapest_amazon_price,
                "price_delta_vs_zoro": r.price_delta_vs_zoro,
            }
            for i, ar in enumerate(r.amazon_results, 1):
                row[f"amazon_{i}_title"] = ar.title
                row[f"amazon_{i}_price"] = ar.price
                row[f"amazon_{i}_rating"] = ar.rating
                row[f"amazon_{i}_reviews"] = ar.review_count
                row[f"amazon_{i}_asin"] = ar.asin
                row[f"amazon_{i}_url"] = ar.url
            w.writerow(row)
    log.info("Wrote %d comparison rows to %s", len(rows), path)


# ---------- CLI ----------------------------------------------------------- #

def read_urls(url: str | None, file: str | None) -> list[str]:
    urls: list[str] = []
    if url:
        urls.append(url)
    if file:
        urls.extend(
            line.strip() for line in Path(file).read_text().splitlines()
            if line.strip() and not line.startswith("#")
        )
    return urls


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Polite product page scraper.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Single product (any supported site):
  python scraper.py --url "https://pk.iherb.com/pr/some-product/12345"

  # Zoro sitemap crawl:
  python scraper.py --sitemap https://www.zoro.com --limit 15 --random

  # iHerb Pakistan subdomain — auto-uses curl, prices in PKR:
  python scraper.py --sitemap https://pk.iherb.com --limit 25 --no-robots \\
                    --out iherb_batch.json

  # iHerb explicit curl mode:
  python scraper.py --sitemap https://pk.iherb.com --limit 10 --curl \\
                    --no-robots --out iherb_batch.json

  # Discover URLs only (no scraping):
  python scraper.py --sitemap https://pk.iherb.com --limit 50 --urls-only \\
                    --no-robots --out iherb_urls.txt

  # Full FBA sourcing pipeline (needs Keepa for FBM/BSR fields):
  python scraper.py --sitemap https://pk.iherb.com --limit 25 --no-robots \\
                    --sourcing --out sourcing.xlsx
""",
    )
    src = ap.add_argument_group("URL source (choose one)")
    src.add_argument("--url", help="Single product URL")
    src.add_argument("--file", help="File with one URL per line")
    src.add_argument("--sitemap", metavar="BASE_URL",
                     help="Discover URLs from this site's sitemap.xml (e.g. https://www.zoro.com)")
    src.add_argument("--from-scraped", metavar="JSON_FILE",
                     help="Load already-scraped products; skip Zoro scraping (use with --compare-amazon)")

    sm = ap.add_argument_group("Sitemap options (used with --sitemap)")
    sm.add_argument("--limit", type=int, default=25, help="Max products to scrape (default: 25)")
    sm.add_argument("--random", action="store_true",
                    help="Randomly sample URLs from sitemap (else take first N)")
    sm.add_argument("--pattern", help="Regex to filter product URLs (overrides host default)")
    sm.add_argument("--urls-only", action="store_true",
                    help="Just discover and save URLs — don't scrape them")
    sm.add_argument("--max-sitemaps", type=int, default=None,
                    help="Max number of sitemap files to crawl (default: 20 for "
                         "normal runs, 300 when --brands is set — narrow brand "
                         "queries need a much deeper crawl to surface niche "
                         "brands buried in later sitemap files)")

    amz = ap.add_argument_group("Amazon comparison")
    amz.add_argument("--compare-amazon", action="store_true",
                     help="After scraping, search Amazon for each product and produce a comparison")
    amz.add_argument("--amazon-results", type=int, default=3,
                     help="Amazon results to fetch per product (default: 3)")
    amz.add_argument("--amazon-delay", type=float, default=6.0,
                     help="Extra-slow delay for Amazon requests (default: 6s — CAPTCHA avoidance)")

    src2 = ap.add_argument_group("FBA sourcing analysis")
    src2.add_argument("--sourcing", action="store_true",
                      help="Produce an FBA sourcing spreadsheet (matches demo_sourcing template)")
    src2.add_argument("--config", metavar="FILE",
                      help="Load filter settings from a JSON config file "
                           "(overridden by any --min-* flags you also pass)")
    src2.add_argument("--save-config", metavar="FILE",
                      help="Write the current effective config to a JSON file and exit")
    src2.add_argument("--no-enrich", action="store_true",
                      help="Skip fetching Amazon product pages (faster; less data)")

    # Financial
    src2.add_argument("--fee-pct", type=float, default=None,
                      help="Amazon referral fee (default: 0.15)")
    src2.add_argument("--extra-cost", type=float, default=None,
                      help="Per-unit shipping/prep costs (default: 0)")
    src2.add_argument("--min-supplier-price", type=float, default=None,
                      help="Skip supplier products below this price. Auto-oversamples "
                           "the sitemap by 3x to compensate. Use 25 for iHerb "
                           "(free-shipping threshold). Default: 0 (disabled).")
    src2.add_argument("--brands", type=str, default=None,
                      help='Comma-separated brand list to include, e.g. "Now Foods,Jarrow Formulas". '
                           "Case-insensitive contains match. Auto-oversamples 5x.")
    src2.add_argument("--exclude-brands", type=str, default=None,
                      help='Comma-separated brands to EXCLUDE, e.g. "Now Foods". '
                           "Applied after --brands filter.")

    # Profitability filters
    src2.add_argument("--min-profit", type=float, default=None,
                      help="Minimum profit $ for approval (default: 0.01)")
    src2.add_argument("--min-margin", type=float, default=None,
                      help="Minimum margin, e.g. 0.10 for 10%% (default: 0)")
    src2.add_argument("--min-roi", type=float, default=None,
                      help="Minimum ROI, e.g. 0.15 for 15%% (default: 0)")

    # Competition filters
    src2.add_argument("--min-fbm", type=int, default=None,
                      help="Minimum live FBM sellers (default: 4)")
    src2.add_argument("--max-fbm", type=int, default=None,
                      help="Maximum live FBM sellers (default: no cap)")
    src2.add_argument("--min-historical-sellers", type=int, default=None,
                      help="Minimum historical sellers ever seen (default: 0 = disabled)")
    src2.add_argument("--allow-amazon-on-listing", action="store_true",
                      help="Don't reject when Amazon is on the listing (default: reject)")

    # Product quality filters
    src2.add_argument("--min-rating", type=float, default=None,
                      help="Minimum Amazon rating (default: 3.5)")
    src2.add_argument("--require-rating", action="store_true",
                      help="Reject products with no rating data at all")
    src2.add_argument("--min-reviews", type=int, default=None,
                      help="Minimum review count (default: 0 = disabled)")
    src2.add_argument("--require-reviews", action="store_true",
                      help="Reject products with no reviews at all")

    # Velocity filter
    src2.add_argument("--max-bsr", type=int, default=None,
                      help="Maximum BSR — reject slow movers (default: no cap)")

    # Currency
    src2.add_argument("--supplier-currency", default=None,
                      help="Currency of supplier prices (e.g. PKR, EUR, GBP). "
                           "Default: USD (no conversion). Auto-detected for pk.iherb.com.")
    src2.add_argument("--fx-rate", type=float, default=None,
                      help="How many supplier-currency units equal 1 USD. "
                           "Example: --fx-rate 280 for PKR.")

    keepa_g = ap.add_argument_group("Keepa (Amazon-side enrichment)")
    keepa_g.add_argument("--keepa-key", default=None,
                        help="Keepa API key (or set KEEPA_API_KEY env var)")
    keepa_g.add_argument("--keepa-domain", type=int, default=1,
                        help="Keepa marketplace domain (1=amazon.com US, default)")

    out_g = ap.add_argument_group("Output")
    out_g.add_argument("--out", default="products.json", help="Output file")
    out_g.add_argument("--format", choices=["json", "csv"], default="json")

    net = ap.add_argument_group("Network")
    net.add_argument("--delay", type=float, default=2.0, help="Seconds between requests")
    net.add_argument("--no-robots", action="store_true",
                     help="Ignore robots.txt (not recommended)")
    net.add_argument("--country", default=None,
                     help="Force country/currency (e.g. 'US' for iHerb USD prices from any IP)")
    net.add_argument("--playwright", action="store_true",
                     help="Use real Chromium browser (bypasses Cloudflare/anti-bot). "
                          "Requires: pip install playwright && playwright install chromium")
    net.add_argument("--show-browser", action="store_true",
                     help="With --playwright, show the browser window (useful for debugging)")
    net.add_argument("--curl", action="store_true",
                     help="Use curl subprocess (faster than Playwright; works for iHerb). "
                          "Auto-enabled for iHerb URLs.")
    net.add_argument("--no-auto-curl", action="store_true",
                     help="Disable curl auto-selection for iHerb")
    args = ap.parse_args()

    sources_given = sum(bool(x) for x in (args.url, args.file, args.sitemap, args.from_scraped))
    if sources_given == 0:
        ap.error("Provide --url, --file, --sitemap, or --from-scraped")
    if sources_given > 1:
        ap.error("Use only one of --url / --file / --sitemap / --from-scraped")

    if args.from_scraped and not args.compare_amazon:
        ap.error("--from-scraped only makes sense with --compare-amazon")

    if args.playwright and args.curl:
        ap.error("Choose --playwright OR --curl, not both")

    # Auto-select curl for iHerb unless user overrides
    target_url = args.url or args.sitemap or args.file
    auto_curl = False
    if (not args.playwright and not args.curl and not args.no_auto_curl
            and target_url and "iherb.com" in target_url.lower()):
        auto_curl = True
        log.info("iHerb detected — auto-selecting --curl "
                 "(disable with --no-auto-curl, override with --playwright)")

    if args.playwright:
        PlaywrightClient = _load_playwright_client()
        client = PlaywrightClient(
            delay=args.delay,
            respect_robots=not args.no_robots,
            headless=not args.show_browser,
        )
    elif args.curl or auto_curl:
        CurlClient = _load_curl_client()
        client = CurlClient(
            delay=args.delay,
            respect_robots=not args.no_robots,
        )
    else:
        client = HttpClient(delay=args.delay, respect_robots=not args.no_robots)

    # Apply country/currency cookies for iHerb.
    # iHerb geo-detects by IP; without these cookies, users in non-US countries
    # get local prices (e.g. PKR from Pakistan). These cookies force US/USD from
    # any IP. Verified working via curl testing.
    target_url_l = (args.url or args.sitemap or "").lower()
    is_iherb = "iherb.com" in target_url_l
    if args.country:
        cc = args.country.upper()
    elif is_iherb:
        cc = "US"  # Default US pricing for iHerb — matches the arbitrage target
    else:
        cc = None

    if cc:
        curr = "USD" if cc == "US" else cc
        lang = "en-US"
        # These are the ACTUAL working cookie names iHerb reads (verified via
        # curl testing). Value uses & and = as inner delimiters — curl passes
        # the string through verbatim, iHerb parses it server-side.
        client.set_cookie(
            "ih-preference",
            f"country={cc}&currency={curr}&language={lang}&store=0",
            domain=".iherb.com",
        )
        client.set_cookie(
            "iher-pref1",
            f"lan={lang}&sccode={cc}&scurcode={curr}&storeid=0",
            domain=".iherb.com",
        )
        client.set_header("Accept-Language", "en-US,en;q=0.9")
        log.info("iHerb region: country=%s currency=%s", cc, curr)

    try:
        _run(args, client)
    finally:
        client.close()


def _run(args, client) -> None:

    # --- Load or scrape Zoro products ------------------------------------- #
    if args.from_scraped:
        log.info("Loading pre-scraped products from %s", args.from_scraped)
        loaded = json.loads(Path(args.from_scraped).read_text(encoding="utf-8"))
        product_dicts = loaded  # already list-of-dicts
        products = None  # signal that we came from disk

        # Post-filter by min-supplier-price if requested
        min_price = args.min_supplier_price or 0.0
        if min_price > 0:
            before = len(product_dicts)
            product_dicts = [
                p for p in product_dicts
                if (pr := _to_price(p.get("price"))) is not None and pr >= min_price
            ]
            log.info("Filtered by min supplier price $%.2f: %d → %d products",
                     min_price, before, len(product_dicts))
    else:
        # Parse filter args
        min_price = args.min_supplier_price or 0.0
        brand_include = _split_csv_arg(args.brands)
        brand_exclude = _split_csv_arg(args.exclude_brands)
        any_filter = bool(min_price or brand_include or brand_exclude)

        # Oversample multiplier — scale with filter aggressiveness.
        # Brand filter pre-drops 80-90% of URLs (for iHerb), so we need
        # a much larger initial pool.
        if brand_include:
            oversample_mult = 30
        elif min_price > 0:
            oversample_mult = 3
        else:
            oversample_mult = 1
        target_limit = args.limit * oversample_mult

        if args.sitemap:
            # Narrow brand queries need to crawl many more sitemap files —
            # niche brands like "Bob's Red Mill" often live past the first
            # 20 sub-sitemaps. URL pre-filtering makes deeper crawling cheap.
            if args.max_sitemaps is not None:
                max_sm = args.max_sitemaps
            elif brand_include:
                max_sm = 300
            else:
                max_sm = 20

            # When brand filter is active on iHerb, apply it INSIDE the
            # crawler so it keeps walking sitemaps until it has enough
            # brand-matching URLs — not just enough total URLs. Without this,
            # niche brands starve because they represent <1% of iHerb's
            # catalog and the target_pool fills up with unrelated URLs.
            url_filter = None
            from urllib.parse import urlparse as _urlp
            host = _urlp(args.sitemap).netloc.lower()
            if brand_include and "iherb.com" in host:
                all_slugs: list[str] = []
                for b in brand_include:
                    all_slugs.extend(_brand_to_url_slugs(b))
                # De-dup preserving order.
                _seen: set[str] = set()
                all_slugs = [s for s in all_slugs if not (s in _seen or _seen.add(s))]
                log.info("In-crawl brand filter active; slugs: %s",
                         ", ".join(all_slugs))
                def url_filter(u: str, _slugs=tuple(all_slugs)) -> bool:
                    ul = u.lower()
                    return any(f"/pr/{s}-" in ul or f"/pr/{s}/" in ul
                               for s in _slugs)

            log.info("Crawling up to %d sitemap file(s)%s",
                     max_sm,
                     " (bumped for brand-narrow query)" if brand_include and args.max_sitemaps is None else "")
            crawler = SitemapCrawler(client, max_sitemaps=max_sm)
            urls = crawler.collect_urls(
                base_url=args.sitemap,
                limit=target_limit,
                pattern=args.pattern,
                sample_random=args.random,
                url_filter=url_filter,
            )
            if not urls:
                log.error("No product URLs found.")
                log.error("Common causes:")
                log.error("  1. robots.txt disallowed product pages for this UA")
                log.error("     → retry with --no-robots")
                log.error("  2. URL pattern doesn't match this site")
                log.error("     → retry with --pattern '.*'")
                return
            if any_filter:
                filter_desc = []
                if min_price > 0:
                    filter_desc.append(f"price ≥ ${min_price:.2f}")
                if brand_include:
                    filter_desc.append(f"brands: {', '.join(brand_include)}")
                if brand_exclude:
                    filter_desc.append(f"excluding: {', '.join(brand_exclude)}")
                log.info("Sitemap yielded %d URL(s). Filters: %s. "
                         "Will keep first %d qualifying.",
                         len(urls), " / ".join(filter_desc), args.limit)

                # OPTIMIZATION — iHerb encodes brand in URL slug, so we can
                # pre-filter URLs and skip scraping products from wrong brands.
                # Cuts wasted fetches by 80-90% for narrow brand targeting.
                if brand_include:
                    from urllib.parse import urlparse
                    host = urlparse(args.sitemap).netloc
                    urls, dropped = _prefilter_urls_by_brand(urls, brand_include, host)
                    if dropped > 0:
                        log.info("URL pre-filter by brand: dropped %d URLs, "
                                 "%d remain for scraping", dropped, len(urls))
            else:
                log.info("Sitemap yielded %d URL(s) to scrape", len(urls))
        else:
            urls = read_urls(args.url, args.file)

        # URLs-only mode: stop early
        if args.urls_only:
            out = Path(args.out)
            out.write_text("\n".join(urls), encoding="utf-8")
            log.info("Wrote %d URLs to %s", len(urls), out)
            return

        # Scrape — with incremental filtering when any filter is active
        scraper = Scraper(client=client)
        if any_filter and args.sitemap:
            products = []
            skipped_price = 0
            skipped_brand = 0
            for i, url in enumerate(urls, 1):
                p = scraper.scrape_one(url)
                if p is None:
                    continue
                p_dict = asdict(p)

                # Filter 1: price
                if min_price > 0:
                    price = _to_price(p_dict.get("price"))
                    if price is None or price < min_price:
                        skipped_price += 1
                        continue

                # Filter 2: brand include/exclude
                if brand_include or brand_exclude:
                    if not _matches_brand_filter(p_dict, brand_include, brand_exclude):
                        skipped_brand += 1
                        continue

                products.append(p)
                if len(products) >= args.limit:
                    log.info(
                        "Reached target of %d qualifying products "
                        "(scanned %d, skipped: price=%d brand=%d)",
                        args.limit, i, skipped_price, skipped_brand,
                    )
                    break
            if len(products) < args.limit:
                log.warning(
                    "Only found %d qualifying products (target: %d). "
                    "Skipped: price=%d brand=%d. "
                    "Consider raising --limit or loosening filters.",
                    len(products), args.limit,
                    skipped_price, skipped_brand,
                )
        else:
            products = scraper.scrape_many(urls)
        product_dicts = [asdict(p) for p in products]

    # --- Sourcing analysis branch (most specific — do first) ------------- #
    if args.sourcing:
        # Step A: start from config file if provided, else defaults
        if args.config:
            cfg = SourcingConfig.from_file(args.config)
            log.info("Loaded sourcing config from %s", cfg.config_source)
        else:
            cfg = SourcingConfig()

        # Step B: apply any CLI overrides (only for flags the user actually set)
        if args.fee_pct is not None:              cfg.fee_pct = args.fee_pct
        if args.extra_cost is not None:            cfg.extra_cost = args.extra_cost
        if args.min_supplier_price is not None:    cfg.min_supplier_price = args.min_supplier_price
        if args.min_profit is not None:            cfg.min_profit = args.min_profit
        if args.min_margin is not None:            cfg.min_margin = args.min_margin
        if args.min_roi is not None:               cfg.min_roi = args.min_roi
        if args.min_fbm is not None:               cfg.min_fbm_sellers = args.min_fbm
        if args.max_fbm is not None:               cfg.max_fbm_sellers = args.max_fbm
        if args.min_historical_sellers is not None: cfg.min_historical_sellers = args.min_historical_sellers
        if args.allow_amazon_on_listing:           cfg.reject_if_amazon_on_listing = False
        if args.min_rating is not None:            cfg.min_rating = args.min_rating
        if args.require_rating:                    cfg.require_rating = True
        if args.min_reviews is not None:           cfg.min_reviews = args.min_reviews
        if args.require_reviews:                   cfg.require_reviews = True
        if args.max_bsr is not None:               cfg.max_bsr = args.max_bsr

        # Auto-detect currency
        # Since we now default iHerb to US pricing via cookies (see main()),
        # iHerb prices come back in USD by default. Only fall back to PKR when
        # the user explicitly forced --country PK.
        supplier_currency = args.supplier_currency
        fx_rate = args.fx_rate
        if supplier_currency is None:
            target = (args.url or args.sitemap or "").lower()
            if not target and product_dicts:
                target = (product_dicts[0].get("url") or "").lower()
            forced_pk = args.country and args.country.upper() == "PK"
            if "iherb.com" in target and forced_pk:
                supplier_currency = "PKR"
                if fx_rate is None:
                    fx_rate = 280.0
                log.info("iHerb with --country PK: converting PKR to USD at "
                         "rate=%s (override with --fx-rate)", fx_rate)
            # else: iHerb defaults to USD (no conversion needed) — the US
            # cookies force USD prices regardless of source subdomain
        if supplier_currency is not None:
            cfg.supplier_currency = supplier_currency
        if fx_rate is not None:
            cfg.supplier_to_usd_rate = fx_rate
        if cfg.supplier_currency != "USD" and cfg.supplier_to_usd_rate == 1.0:
            log.warning("Supplier currency %s but no --fx-rate set; "
                        "profit math will be wrong.", cfg.supplier_currency)

        # Step C: --save-config just writes the effective config and exits
        if args.save_config:
            cfg.save_to_file(args.save_config)
            log.info("Saved effective config to %s", args.save_config)
            return

        log.info("Effective config: min_profit=$%s min_fbm=%d min_rating=%s min_reviews=%d "
                 "max_bsr=%s currency=%s fx=%s",
                 cfg.min_profit, cfg.min_fbm_sellers, cfg.min_rating, cfg.min_reviews,
                 cfg.max_bsr, cfg.supplier_currency, cfg.supplier_to_usd_rate)

        # Prefer Keepa when a key is available
        keepa_key = args.keepa_key or os.environ.get("KEEPA_API_KEY")
        if keepa_key:
            from keepa import KeepaClient
            from sourcing import build_sourcing_rows_from_supplier, enrich_rows_with_keepa
            log.info("Keepa key detected → using Keepa for Amazon-side enrichment "
                     "(recommended path)")
            keepa_client = KeepaClient(api_key=keepa_key, domain=args.keepa_domain)
            rows = build_sourcing_rows_from_supplier(product_dicts, cfg)
            rows = enrich_rows_with_keepa(rows, keepa_client, cfg)
        else:
            log.error("=" * 70)
            log.error("NO KEEPA API KEY DETECTED")
            log.error("Set your key first — one of these three ways:")
            log.error("  1. This session:     $env:KEEPA_API_KEY = \"your_key\"")
            log.error("  2. Command flag:     --keepa-key \"your_key\"")
            log.error("  3. Permanent:        [Environment]::SetEnvironmentVariable(")
            log.error("                         \"KEEPA_API_KEY\", \"your_key\", \"User\")")
            log.error("Without Keepa, the tool falls back to unreliable Amazon web")
            log.error("scraping — matches will be inaccurate and many fields empty.")
            log.error("=" * 70)
            amazon_client = HttpClient(delay=args.amazon_delay, respect_robots=False)
            searcher = AmazonSearcher(client=amazon_client, top_n=args.amazon_results)
            rows = build_sourcing_rows(
                zoro_products=product_dicts,
                searcher=searcher,
                cfg=cfg,
                enrich_top_match=not args.no_enrich,
            )

        # Summary log
        by_status: dict[str, int] = {}
        for r in rows:
            by_status[r.status] = by_status.get(r.status, 0) + 1
        log.info("Sourcing done: %s", by_status)

        out = Path(args.out)
        # Auto-select format based on extension
        if out.suffix.lower() == ".xlsx" or args.format == "json":
            if out.suffix.lower() != ".xlsx":
                out = out.with_suffix(".xlsx")
            save_sourcing_xlsx(rows, out)
        else:
            save_sourcing_csv(rows, out)
        return

    # --- Amazon comparison branch ----------------------------------------- #
    if args.compare_amazon:
        # Use a separate slower client for Amazon
        amazon_client = HttpClient(
            delay=args.amazon_delay,
            respect_robots=False,  # module bypasses; see amazon.py docstring
        )
        searcher = AmazonSearcher(client=amazon_client, top_n=args.amazon_results)
        rows = compare_zoro_to_amazon(product_dicts, searcher)

        captcha_count = sum(1 for r in rows if r.captcha_hit)
        empty_count = sum(1 for r in rows if not r.amazon_results and not r.captcha_hit)
        log.info(
            "Amazon comparison done: %d rows | %d CAPTCHA hits | %d empty results",
            len(rows), captcha_count, empty_count,
        )

        out = Path(args.out)
        if args.format == "csv":
            save_comparison_csv(rows, out)
        else:
            save_comparison_json(rows, out)
        return

    # --- Plain Zoro output ------------------------------------------------ #
    if products is None:
        log.warning("Nothing to output — --from-scraped without --compare-amazon.")
        return
    out = Path(args.out)
    if args.format == "csv":
        save_csv(products, out)
    else:
        save_json(products, out)


if __name__ == "__main__":
    main()
