"""
Amazon Search
-------------
Best-effort Amazon search + product comparison.

IMPORTANT — READ BEFORE USING:
  * Amazon's robots.txt disallows /s? (search endpoints). This module
    deliberately bypasses robots.txt for those requests.
  * Amazon serves CAPTCHAs / "Robot Check" pages after modest usage from
    a single IP. This module detects them and skips gracefully, but
    reliability at scale is poor.
  * For production use, switch to Amazon PA-API (official) or a paid
    scraping service (ScraperAPI, Bright Data, Oxylabs).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import quote_plus

from bs4 import BeautifulSoup

log = logging.getLogger("scraper.amazon")


@dataclass
class AmazonResult:
    asin: str | None = None
    title: str | None = None
    price: str | None = None
    currency: str = "USD"
    rating: str | None = None
    review_count: str | None = None
    url: str | None = None
    image: str | None = None
    sponsored: bool = False


@dataclass
class AmazonProductDetails:
    """Additional data pulled from the /dp/{ASIN} product page."""
    asin: str
    title: str | None = None
    price: str | None = None
    brand: str | None = None
    model: str | None = None
    upc: str | None = None
    rating: str | None = None
    review_count: str | None = None
    category: str | None = None
    breadcrumbs: list[str] = field(default_factory=list)
    bsr: str | None = None
    bsr_top_category: str | None = None
    sold_by: str | None = None
    ships_from: str | None = None
    amazon_on_listing: bool | None = None  # None = couldn't determine
    fetch_ok: bool = False
    captcha_hit: bool = False


@dataclass
class ComparisonRow:
    """A Zoro product paired with its top Amazon matches."""
    zoro_url: str
    zoro_title: str | None
    zoro_brand: str | None
    zoro_price: str | None
    zoro_sku: str | None
    search_query: str
    amazon_results: list[AmazonResult] = field(default_factory=list)
    cheapest_amazon_price: float | None = None
    price_delta_vs_zoro: float | None = None
    captcha_hit: bool = False


class AmazonSearcher:
    SEARCH_URL = "https://www.amazon.com/s?k={query}"
    PRODUCT_URL = "https://www.amazon.com/dp/{asin}"

    # Selectors — Amazon rotates these; kept flexible with multiple fallbacks
    RESULT_SELECTORS = [
        'div[data-component-type="s-search-result"]',
        'div.s-result-item[data-asin]',
    ]

    def __init__(self, client, top_n: int = 3) -> None:
        """
        `client` is an HttpClient from scraper.py.
        We deliberately do NOT respect robots.txt for Amazon search — see
        the docstring at top of this file.
        """
        self.client = client
        self.top_n = top_n
        self._warned_robots = False

    # ------- public API ---------------------------------------------- #

    def search(self, query: str) -> tuple[list[AmazonResult], bool]:
        """
        Return (results, captcha_hit).
        `captcha_hit` is True if Amazon served us a robot-check page.
        """
        url = self.SEARCH_URL.format(query=quote_plus(query))
        html = self._fetch_bypassing_robots(url)
        if html is None:
            return [], False

        if self._is_captcha(html):
            log.warning("Amazon served CAPTCHA / robot check for query: %s", query)
            return [], True

        results = self._parse_search_page(html)
        return results[: self.top_n], False

    def build_query(self, title: str | None, brand: str | None, sku: str | None) -> str:
        """
        Construct a search query from Zoro product data.
        Strategy:
          1. If title has a model-number-looking token at the end, use "brand + model".
          2. Otherwise, use "brand + first 6 words of title", truncated.
        """
        title = (title or "").strip()
        brand = (brand or "").strip()

        # Extract likely model number: uppercase-alphanumeric with digits, near end
        model_match = re.search(r"\b([A-Z0-9][A-Z0-9\-\/]{3,})\b\s*$", title)
        if model_match and brand:
            return f"{brand} {model_match.group(1)}"[:120]

        words = title.split()
        core = " ".join(words[:6])
        if brand and brand.lower() not in core.lower():
            core = f"{brand} {core}"
        return core[:120] or (brand or "") or (sku or "")

    # ------- internals ----------------------------------------------- #

    def _fetch_bypassing_robots(self, url: str) -> str | None:
        """Fetch a URL, temporarily suspending robots.txt enforcement."""
        if not self._warned_robots:
            log.warning(
                "Amazon: bypassing robots.txt (Amazon disallows /s? for all bots). "
                "Use responsibly and expect rate-limiting/CAPTCHAs."
            )
            self._warned_robots = True

        original = self.client.respect_robots
        self.client.respect_robots = False
        try:
            return self.client.get(url)
        finally:
            self.client.respect_robots = original

    @staticmethod
    def _is_captcha(html: str) -> bool:
        markers = [
            "Robot Check",
            "Sorry, we just need to make sure you're not a robot",
            "Enter the characters you see below",
            "/errors/validateCaptcha",
            "api-services-support@amazon.com",
        ]
        head = html[:5000]  # only need to check the top
        return any(m in head for m in markers)

    def _parse_search_page(self, html: str) -> list[AmazonResult]:
        soup = BeautifulSoup(html, "html.parser")

        containers = []
        for sel in self.RESULT_SELECTORS:
            containers = soup.select(sel)
            if containers:
                break

        results: list[AmazonResult] = []
        for c in containers:
            asin = c.get("data-asin") or ""
            if not asin or len(asin) < 5:
                continue  # sponsored slot or non-product row
            r = self._parse_result(c, asin)
            if r.title:  # only keep parseable results
                results.append(r)
        return results

    def _parse_result(self, container, asin: str) -> AmazonResult:
        r = AmazonResult(asin=asin, url=self.PRODUCT_URL.format(asin=asin))

        # Title — try several selectors. Skip anything that looks like just a
        # brand link (e.g., "QMARK") — real product titles are longer with
        # a mix of words. Prefer the longest candidate.
        candidates: list[str] = []
        for sel in [
            'h2 a span',
            'h2 span',
            '[data-cy="title-recipe"] h2 a span',
            '[data-cy="title-recipe"] span',
            'a.a-link-normal h2 span',
            'h2',
        ]:
            for el in container.select(sel):
                txt = el.get_text(" ", strip=True)
                if txt and 8 <= len(txt) <= 400:  # exclude brand-only or garbage
                    candidates.append(txt)
        if candidates:
            # Prefer the candidate with the most words (real titles are wordy)
            r.title = max(candidates, key=lambda s: (len(s.split()), len(s)))

        # Price — sanity-check to avoid picking up SKU-like numbers.
        # Real product prices on amazon.com are in USD; the parser now
        # rejects anything > $50,000 or with too many digits.
        price_el = container.select_one(".a-price .a-offscreen")
        if price_el:
            raw = price_el.get_text(strip=True)
            m = re.search(r"(?:USD|US)?\s*\$?\s*([\d,]+\.\d{2})", raw)
            if m:
                try:
                    val = float(m.group(1).replace(",", ""))
                    if 0.10 <= val <= 50000:
                        r.price = f"{val:.2f}"
                except ValueError:
                    pass

        # Rating (e.g. "4.5 out of 5 stars")
        rating_el = container.select_one(
            "i.a-icon-star-small span.a-icon-alt, "
            "i.a-icon-star span.a-icon-alt, "
            "span[aria-label*='out of 5']"
        )
        if rating_el:
            txt = rating_el.get_text(strip=True) or rating_el.get("aria-label", "")
            m = re.match(r"([\d.]+)", txt)
            if m:
                r.rating = m.group(1)

        # Review count
        review_el = container.select_one(
            "span.a-size-base.s-underline-text, "
            "a[href*='#customerReviews'] span.a-size-base"
        )
        if review_el:
            txt = review_el.get_text(strip=True).replace(",", "")
            if txt.isdigit():
                r.review_count = txt

        # Image
        img = container.select_one("img.s-image")
        if img and img.get("src"):
            r.image = img["src"]

        # Sponsored flag
        if container.select_one("span.puis-sponsored-label-text, .s-sponsored-label-info-icon"):
            r.sponsored = True

        return r

    # ------- product page enrichment --------------------------------- #

    def fetch_product_details(self, asin: str) -> AmazonProductDetails:
        """
        Visit /dp/{asin} and extract the fields that need the full product
        page — category, BSR, sold-by, UPC, model, etc.
        """
        details = AmazonProductDetails(asin=asin)
        url = self.PRODUCT_URL.format(asin=asin)
        html = self._fetch_bypassing_robots(url)
        if html is None:
            return details

        if self._is_captcha(html):
            details.captcha_hit = True
            log.warning("Amazon product page CAPTCHA for %s", asin)
            return details

        soup = BeautifulSoup(html, "html.parser")

        # Full clean title
        t = soup.select_one("#productTitle")
        if t:
            details.title = t.get_text(strip=True)

        # Price (product page has multiple layouts)
        for sel in [
            "#corePrice_feature_div .a-price .a-offscreen",
            "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
            ".a-price .a-offscreen",
        ]:
            el = soup.select_one(sel)
            if el:
                raw = el.get_text(strip=True)
                m = re.search(r"\$?\s*([\d,]+\.\d{2})", raw)
                if m:
                    val = float(m.group(1).replace(",", ""))
                    if 0.10 <= val <= 50000:
                        details.price = f"{val:.2f}"
                        break

        # Brand (from byline "Visit the X Store" or product detail table)
        byline = soup.select_one("#bylineInfo")
        if byline:
            txt = byline.get_text(strip=True)
            m = re.search(r"(?:Visit the |Brand: )(.+?)(?: Store|$)", txt)
            if m:
                details.brand = m.group(1).strip()
            elif txt:
                details.brand = txt

        # Rating & review count (product page)
        r = soup.select_one("#acrPopover .a-icon-alt, [data-hook='rating-out-of-text']")
        if r:
            m = re.match(r"([\d.]+)", r.get_text(strip=True))
            if m:
                details.rating = m.group(1)
        rc = soup.select_one("#acrCustomerReviewText")
        if rc:
            txt = rc.get_text(strip=True).replace(",", "").split()[0]
            if txt.isdigit():
                details.review_count = txt

        # Breadcrumbs → category
        details.breadcrumbs = [
            a.get_text(strip=True)
            for a in soup.select("#wayfinding-breadcrumbs_feature_div a")
            if a.get_text(strip=True)
        ]
        if details.breadcrumbs:
            details.category = details.breadcrumbs[0]

        # BSR + top-category BSR — from the product details tables.
        # Amazon renders this as free text like:
        #   "Best Sellers Rank: #4,200 in Tools & Home Improvement (#12 in Cordless Drills)"
        page_text = soup.get_text(" ", strip=True)
        bsr_match = re.search(
            r"Best Sellers Rank:?\s*#?([\d,]+)\s+in\s+([^(#\n]+?)(?:\s*\(#([\d,]+)\s+in\s+([^)]+)\))?",
            page_text,
        )
        if bsr_match:
            details.bsr = bsr_match.group(1).replace(",", "")
            details.bsr_top_category = bsr_match.group(2).strip()

        # Sold by / Ships from — used for "Amazon on listing" detection
        # Modern layout: #tabular-buybox with rows for "Ships from" and "Sold by"
        for row in soup.select("#tabular-buybox .tabular-buybox-text, .offer-display-feature-text"):
            label_el = row.select_one(".tabular-buybox-text-truncate, .offer-display-feature-text-heading")
            val_el = row.select_one(".tabular-buybox-text, .offer-display-feature-text-message")
            # Fallback: read whole row text
            txt = row.get_text(" ", strip=True)
            if "Sold by" in txt:
                m = re.search(r"Sold by\s+(.+?)(?:$|Ships from|Return)", txt)
                if m:
                    details.sold_by = m.group(1).strip()
            if "Ships from" in txt:
                m = re.search(r"Ships from\s+(.+?)(?:$|Sold by|Return)", txt)
                if m:
                    details.ships_from = m.group(1).strip()

        # Fallback: look in merchant-info div (older layout)
        if not details.sold_by:
            merchant = soup.select_one("#merchant-info")
            if merchant:
                txt = merchant.get_text(" ", strip=True)
                m = re.search(r"sold by\s+(.+?)(?:\.|,|$)", txt, re.IGNORECASE)
                if m:
                    details.sold_by = m.group(1).strip()

        # Amazon on listing? Yes if "Amazon.com" is the seller or shipper
        markers = [details.sold_by or "", details.ships_from or ""]
        joined = " ".join(markers).lower()
        if joined.strip():
            details.amazon_on_listing = "amazon.com" in joined or "amazon " in joined
        # If neither field was found, we leave amazon_on_listing = None (unknown)

        # UPC and Model from the product details / spec tables
        for row in soup.select(
            "#productDetails_detailBullets_sections1 tr, "
            "#productDetails_techSpec_section_1 tr, "
            "#detailBullets_feature_div li, "
            ".prodDetTable tr"
        ):
            row_text = row.get_text(" ", strip=True)
            m = re.search(r"UPC[:\s]+(\d{12,13})", row_text)
            if m and not details.upc:
                details.upc = m.group(1)
            m = re.search(r"(?:Item model number|Model Number|Model)[:\s]+([A-Z0-9][A-Z0-9\-\/]+)",
                          row_text, re.IGNORECASE)
            if m and not details.model:
                details.model = m.group(1).strip()

        details.fetch_ok = bool(details.title or details.price)
        return details


# ---------- Comparison orchestration --------------------------------- #

def compare_zoro_to_amazon(
    zoro_products: list[dict],
    searcher: AmazonSearcher,
) -> list[ComparisonRow]:
    """
    For each Zoro product dict, run an Amazon search and build a ComparisonRow.
    `zoro_products` is a list of dicts as produced by scraper.py (json output).
    """
    rows: list[ComparisonRow] = []
    for i, p in enumerate(zoro_products, 1):
        query = searcher.build_query(p.get("title"), p.get("brand"), p.get("sku"))
        log.info("[%d/%d] Amazon search: %r", i, len(zoro_products), query)

        amazon_results, captcha = searcher.search(query)

        row = ComparisonRow(
            zoro_url=p.get("url", ""),
            zoro_title=p.get("title"),
            zoro_brand=p.get("brand"),
            zoro_price=p.get("price"),
            zoro_sku=p.get("sku"),
            search_query=query,
            amazon_results=amazon_results,
            captcha_hit=captcha,
        )

        # Compute price delta if we have both
        zoro_price = _to_float(p.get("price"))
        amazon_prices = [_to_float(r.price) for r in amazon_results]
        amazon_prices = [x for x in amazon_prices if x is not None]
        if amazon_prices:
            row.cheapest_amazon_price = min(amazon_prices)
            if zoro_price is not None:
                row.price_delta_vs_zoro = round(row.cheapest_amazon_price - zoro_price, 2)

        rows.append(row)
    return rows


def _to_float(price: str | float | None) -> float | None:
    if price is None:
        return None
    if isinstance(price, (int, float)):
        return float(price)
    m = re.search(r"[\d,]+\.?\d*", str(price))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None