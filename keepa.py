"""
Keepa API Integration
---------------------
Wraps the Keepa /product endpoint for the sourcing pipeline.

Usage:
    client = KeepaClient(api_key="...", domain=1)  # domain=1 → amazon.com US

    # UPC → ASIN(s) → full match data (one call, no separate lookup needed)
    match = client.lookup_by_upc("753950002869")
    if match:
        print(match.asin, match.buy_box_price, match.fbm_seller_count, match.bsr)

    # Bulk ASIN fetch (up to 100 per call — most efficient)
    matches = client.lookup_asins(["B08N5WRWNW", "B01ABC1234", ...])

Token cost:
    * UPC lookup with stats+offers: ~6-8 tokens per product
    * Bulk ASINs same cost per product, but far fewer HTTP round-trips

Environment variable:
    Set KEEPA_API_KEY to avoid passing it on the CLI.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

import requests

log = logging.getLogger("scraper.keepa")

API_BASE = "https://api.keepa.com"
DEFAULT_DOMAIN = 1  # amazon.com (US)

# Keepa's stats.current array uses positional indexes. These are the ones
# we care about for sourcing. Field position is stable — has been for years.
CSV_INDEX = {
    "AMAZON_PRICE":   0,   # -1 if Amazon not on listing
    "NEW_PRICE":      1,
    "USED_PRICE":     2,
    "SALES_RANK":     3,   # BSR in main category
    "LIST_PRICE":     4,
    "AMAZON_ON":      7,   # historical Amazon-on-listing (0/1)
    "NEW_FBM":        11,
    "NEW_FBM_COUNT":  12,  # count of FBM sellers
    "RATING":         16,  # 0-50, divide by 10 for stars
    "REVIEW_COUNT":   17,
    "BUY_BOX_PRICE":  18,
}


class KeepaError(Exception):
    """Raised for Keepa API failures — auth errors, quota, malformed responses."""


@dataclass
class KeepaMatch:
    """Flattened, clean subset of a Keepa product record — what our pipeline uses."""
    # Identity
    asin: str
    title: str | None = None
    brand: str | None = None
    manufacturer: str | None = None
    category: str | None = None
    category_tree: list[str] = field(default_factory=list)
    upc: str | None = None
    ean: str | None = None

    # Pricing (dollars, converted from Keepa cents)
    amazon_price: float | None = None      # None = Amazon not selling
    new_price: float | None = None         # cheapest new offer
    buy_box_price: float | None = None
    avg_price_90d: float | None = None
    list_price: float | None = None

    # Amazon-on-listing flag (client-required)
    amazon_on_listing: bool = False

    # Sellers — full breakdown so the client can filter as they choose
    fbm_seller_count_live: int = 0        # currently active FBM sellers
    fba_seller_count_live: int = 0        # currently active FBA sellers
    total_offer_count_live: int = 0       # live FBA + live FBM (excluding Amazon-direct)
    total_historical_sellers: int = 0     # every seller ever seen (proxy for market churn)
    used_seller_count_live: int = 0       # used-condition sellers, if any

    # Performance signals
    bsr: int | None = None
    bsr_avg_90d: int | None = None
    rating: float | None = None            # 0-5 stars
    review_count: int | None = None

    # Image
    image_url: str | None = None

    # Debug/audit
    raw: dict | None = None                # full Keepa response for inspection


class KeepaClient:
    """
    Thin, retry-safe wrapper around Keepa's /product endpoint.

    - Handles rate-limiting via `tokensLeft` / `refillIn` from every response
    - Batches ASIN lookups (up to 100 per call — dramatic cost saving)
    - Returns None gracefully on no-match, raises KeepaError on API failure
    """

    MAX_BATCH_SIZE = 100  # Keepa's hard limit

    def __init__(
        self,
        api_key: str | None = None,
        domain: int = DEFAULT_DOMAIN,
        stats_days: int = 90,
        offers_count: int = 20,
        timeout: int = 30,
        max_retries: int = 3,
    ) -> None:
        key = api_key or os.environ.get("KEEPA_API_KEY")
        if not key:
            raise KeepaError(
                "No Keepa API key. Pass api_key=... or set KEEPA_API_KEY env var."
            )
        self.api_key = key
        self.domain = domain
        self.stats_days = stats_days
        self.offers_count = offers_count
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        self._tokens_left: int | None = None
        self._refill_ms: int | None = None

    # ---- Public API ------------------------------------------------- #

    def lookup_by_upc(self, upc: str) -> KeepaMatch | None:
        """
        UPC → single best-match KeepaMatch (or None if no products found).

        Keepa's /product?code=UPC returns *all* ASINs sharing that UPC.
        We pick the best listability candidate (see _rank_candidates).
        """
        if not upc or not upc.strip():
            return None
        products = self._get_products(code=upc.strip())
        if not products:
            log.info("No Amazon products for UPC %s", upc)
            return None
        matches = [self._to_match(p) for p in products if p.get("asin")]
        if not matches:
            return None
        return self._rank_candidates(matches)[0]

    def lookup_asins(self, asins: list[str]) -> list[KeepaMatch]:
        """
        Bulk fetch up to 100 ASINs per call. Chunks automatically.
        Returns one KeepaMatch per ASIN found (skips missing ones).
        """
        results: list[KeepaMatch] = []
        for chunk in _chunks(asins, self.MAX_BATCH_SIZE):
            products = self._get_products(asin=",".join(chunk))
            results.extend(
                self._to_match(p) for p in products if p.get("asin")
            )
        return results

    @property
    def tokens_left(self) -> int | None:
        """Last-known Keepa token balance. None until first request made."""
        return self._tokens_left

    # ---- Internal HTTP ---------------------------------------------- #

    def _get_products(self, **params) -> list[dict]:
        """
        Call /product with our defaults + provided identifiers.
        Returns the list of product dicts (empty if none).
        """
        query = {
            "key": self.api_key,
            "domain": self.domain,
            "stats": self.stats_days,
            "offers": self.offers_count,
            **params,
        }
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.session.get(
                    f"{API_BASE}/product",
                    params=query,
                    timeout=self.timeout,
                )
            except requests.RequestException as e:
                log.warning("Keepa request error (attempt %d): %s", attempt, e)
                time.sleep(2 ** attempt)
                continue

            if resp.status_code == 200:
                data = resp.json()
                self._tokens_left = data.get("tokensLeft")
                self._refill_ms = data.get("refillIn")
                log.debug("Keepa tokens left: %s", self._tokens_left)
                return data.get("products") or []

            if resp.status_code == 429:
                # Out of tokens — wait for refill, then retry.
                wait_s = 30
                try:
                    body = resp.json()
                    if "refillIn" in body:
                        wait_s = max(5, int(body["refillIn"] / 1000) + 1)
                except json.JSONDecodeError:
                    pass
                log.warning("Keepa rate-limited (429), waiting %ss …", wait_s)
                time.sleep(wait_s)
                continue

            if resp.status_code in (401, 403):
                raise KeepaError(f"Auth failed ({resp.status_code}): "
                                 "check your API key")

            log.warning("Keepa returned %s (attempt %d): %s",
                        resp.status_code, attempt, resp.text[:200])
            time.sleep(2 ** attempt)

        raise KeepaError(f"Keepa request failed after {self.max_retries} attempts")

    # ---- Response → KeepaMatch ------------------------------------- #

    def _to_match(self, p: dict) -> KeepaMatch:
        """Convert one Keepa product dict into our clean KeepaMatch dataclass."""
        m = KeepaMatch(asin=p.get("asin", ""))
        m.title = p.get("title")
        m.brand = p.get("brand")
        m.manufacturer = p.get("manufacturer")
        m.raw = p

        # UPC / EAN
        upcs = p.get("upcList") or []
        eans = p.get("eanList") or []
        m.upc = upcs[0] if upcs else None
        m.ean = eans[0] if eans else None

        # Category
        cat_tree = p.get("categoryTree") or []
        m.category_tree = [c.get("name") for c in cat_tree if c.get("name")]
        m.category = m.category_tree[-1] if m.category_tree else None  # most specific

        # Image
        img_csv = p.get("imagesCSV")
        if img_csv:
            first = img_csv.split(",")[0].strip()
            if first:
                m.image_url = f"https://images-na.ssl-images-amazon.com/images/I/{first}"

        # Stats-derived fields (the meat)
        stats = p.get("stats") or {}
        current = stats.get("current") or []
        avg90 = stats.get("avg90") or []

        m.amazon_price = _cents_to_dollars(_safe_get(current, CSV_INDEX["AMAZON_PRICE"]))
        m.new_price = _cents_to_dollars(_safe_get(current, CSV_INDEX["NEW_PRICE"]))
        m.list_price = _cents_to_dollars(_safe_get(current, CSV_INDEX["LIST_PRICE"]))
        m.avg_price_90d = _cents_to_dollars(_safe_get(avg90, CSV_INDEX["NEW_PRICE"]))

        # Buy Box: prefer stats.buyBoxPrice (Keepa's direct field), fall back to CSV
        bbp = stats.get("buyBoxPrice")
        if bbp is not None and bbp > 0:
            m.buy_box_price = bbp / 100.0
        else:
            m.buy_box_price = _cents_to_dollars(_safe_get(current, CSV_INDEX["BUY_BOX_PRICE"]))

        # BSR
        m.bsr = _safe_positive_int(_safe_get(current, CSV_INDEX["SALES_RANK"]))
        m.bsr_avg_90d = _safe_positive_int(_safe_get(avg90, CSV_INDEX["SALES_RANK"]))

        # Rating (Keepa stores 0-50, we want 0-5)
        rating_raw = _safe_get(current, CSV_INDEX["RATING"])
        if rating_raw is not None and rating_raw > 0:
            m.rating = round(rating_raw / 10.0, 1)
        m.review_count = _safe_positive_int(_safe_get(current, CSV_INDEX["REVIEW_COUNT"]))

        # Amazon on listing — use the authoritative Keepa flag first
        m.amazon_on_listing = bool(
            stats.get("buyBoxIsAmazon") or (m.amazon_price is not None)
        )

        # === Seller counts — full breakdown ===
        # Keepa's stats.offerCount* fields are the authoritative "live sellers"
        # source. The offers array includes historical sellers, so we don't
        # count it directly for live counts — we use it for the historical total.
        m.fba_seller_count_live = _int_or_zero(stats.get("offerCountFBA"))
        m.fbm_seller_count_live = _int_or_zero(stats.get("offerCountFBM"))
        m.total_offer_count_live = _int_or_zero(stats.get("totalOfferCount"))

        # Historical total — every seller ever seen on this listing.
        # High number = this listing has had lots of churn / competition over time.
        offers = p.get("offers") or []
        m.total_historical_sellers = len([
            o for o in offers
            if not o.get("isAmazon") and o.get("condition", 1) == 1
        ])

        # Used-condition sellers currently live (rare but worth flagging)
        live_indexes = set(p.get("liveOffersOrder") or [])
        m.used_seller_count_live = len([
            i for i in live_indexes
            if i < len(offers) and offers[i].get("condition", 1) != 1
        ])

        return m

    # ---- Candidate ranking (multi-ASIN per UPC) --------------------- #

    def _rank_candidates(self, matches: list[KeepaMatch]) -> list[KeepaMatch]:
        """
        Score candidates for "listability" and return best-first.

        Higher score = better sourcing candidate:
          + Not Amazon-on-listing (client rule)
          + More FBM sellers (validated demand)
          + Lower BSR (better selling)
          + More reviews
          + Higher rating
        """
        def score(m: KeepaMatch) -> float:
            s = 0.0
            if not m.amazon_on_listing:
                s += 100
            s += min(m.fbm_seller_count_live, 20) * 5
            if m.bsr and m.bsr > 0:
                # Lower BSR = better; map 1..1M to 100..0
                s += max(0, 100 - (m.bsr / 10000))
            if m.rating:
                s += m.rating * 10
            if m.review_count:
                s += min(m.review_count, 500) / 10
            return s

        return sorted(matches, key=score, reverse=True)


# ---- Helpers ------------------------------------------------------- #

def _safe_get(arr: list, idx: int) -> int | None:
    if not isinstance(arr, list) or idx >= len(arr):
        return None
    val = arr[idx]
    if val is None or val == -1 or val == -2:
        return None
    return val


def _safe_positive_int(val: int | None) -> int | None:
    if val is None:
        return None
    try:
        v = int(val)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def _int_or_zero(val: Any) -> int:
    """Convert to int, treating None/negative/errors as 0."""
    if val is None:
        return 0
    try:
        v = int(val)
        return v if v > 0 else 0
    except (TypeError, ValueError):
        return 0


def _cents_to_dollars(cents: int | None) -> float | None:
    if cents is None:
        return None
    try:
        return round(int(cents) / 100.0, 2)
    except (TypeError, ValueError):
        return None


def _chunks(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]
