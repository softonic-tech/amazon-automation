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

    # Buy Box seller (the "Ships from" name on Amazon)
    buy_box_seller_id: str | None = None     # Keepa/Amazon seller ID (e.g. "A1B2...")
    buy_box_seller_name: str | None = None   # Human-readable name (e.g. "AmeriStyle")
    buy_box_is_fba: bool | None = None       # True = FBA, False = FBM, None = unknown

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

        Kept for backwards compatibility. Callers that want ALL variations
        sharing a UPC (e.g. different sizes/colours of the same product)
        should use lookup_all_by_upc instead.
        """
        matches = self.lookup_all_by_upc(upc)
        return matches[0] if matches else None

    def lookup_all_by_upc(self, upc: str) -> list[KeepaMatch]:
        """
        UPC → every ASIN variation sharing that UPC on Amazon, ranked so
        the best sourcing candidate is first (see _rank_candidates).

        Keepa's /product?code=UPC returns all matching ASINs in a single
        response — costing the same as one lookup — so returning them all
        is free of extra token cost.

        Returns [] if the UPC is blank or Amazon has no match.
        """
        if not upc or not upc.strip():
            return []
        products = self._get_products(code=upc.strip())
        if not products:
            log.info("No Amazon products for UPC %s", upc)
            return []
        matches = [self._to_match(p) for p in products if p.get("asin")]
        if not matches:
            return []
        return self._rank_candidates(matches)

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

    def search_by_brand_title(
        self,
        brand: str,
        title: str,
        limit: int = 8,
        max_title_words: int = 6,
    ) -> list[KeepaMatch]:
        """
        Text-search Amazon via Keepa /search using a clean brand+title
        query, then filter results strictly to the supplier's brand.

        This is the fallback path for Amazon listings that can't be
        reached via UPC — either the supplier has no UPC, or Amazon's
        seller never registered a barcode on the listing (very common
        for pack-size variants). Verified working on B0BLHRM711 case
        (Alter Eco Dark Chocolate Granola 6-pack, UPC-less on Amazon).

        Cost: ~10 Keepa tokens per unique query, cached across the
        process so repeated brands/titles within a run are free.
        Results are ranked by _rank_candidates so best-listability
        candidates come first.
        """
        if not brand or not title:
            return []
        query = _build_search_query(brand, title, max_title_words)
        if not query:
            return []

        if not hasattr(self, "_search_cache"):
            self._search_cache: dict[str, list[KeepaMatch]] = {}
        if query in self._search_cache:
            return self._search_cache[query]

        try:
            products = self._search_products(query)
        except KeepaError as e:
            log.warning("Keepa /search failed for %r: %s", query, e)
            return []

        supplier_brand_norm = _normalize_brand(brand)
        matches: list[KeepaMatch] = []
        skipped_brands: set[str] = set()
        for p in products:
            if not p.get("asin"):
                continue
            keepa_brand = p.get("brand") or ""
            if _normalize_brand(keepa_brand) != supplier_brand_norm:
                skipped_brands.add(keepa_brand)
                continue
            matches.append(self._to_match(p))

        log.debug("Keepa /search %r → %d hits, %d after brand filter %r "
                  "(dropped brands: %s)",
                  query, len(products), len(matches), brand,
                  ", ".join(sorted(skipped_brands)) or "none")

        matches = self._rank_candidates(matches)[:limit]
        self._search_cache[query] = matches
        return matches

    def lookup_seller_names(self, seller_ids: list[str]) -> dict[str, str]:
        """
        Resolve Amazon seller IDs → seller names via Keepa's /seller endpoint.

        Batches up to 100 IDs per HTTP call and caches results across calls,
        so re-querying the same seller costs nothing. Cost: ~1 Keepa token
        per unique seller (very cheap — Amazon's active third-party seller
        pool is small relative to product count).

        Returns a dict {seller_id: seller_name}. Missing/unresolved IDs
        are omitted rather than mapped to None, so callers can `.get(id)`
        with confidence.
        """
        # Cache on the client instance so repeat runs in the same process
        # don't re-pay for the same sellers.
        if not hasattr(self, "_seller_name_cache"):
            self._seller_name_cache: dict[str, str] = {}

        # Dedup and filter to only IDs we haven't resolved yet.
        needed = sorted({
            sid for sid in seller_ids
            if isinstance(sid, str) and sid and sid != "-1"
            and sid not in self._seller_name_cache
        })
        if not needed:
            return {sid: self._seller_name_cache[sid]
                    for sid in seller_ids if sid in self._seller_name_cache}

        log.info("Keepa: resolving %d seller name(s) via /seller endpoint", len(needed))
        for chunk in _chunks(needed, self.MAX_BATCH_SIZE):
            try:
                sellers = self._get_sellers(chunk)
            except KeepaError as e:
                log.warning("Keepa /seller lookup failed for chunk: %s", e)
                continue
            for sid, sdata in sellers.items():
                name = sdata.get("sellerName") if isinstance(sdata, dict) else None
                if isinstance(name, str) and name.strip():
                    self._seller_name_cache[sid] = name.strip()

        return {sid: self._seller_name_cache[sid]
                for sid in seller_ids if sid in self._seller_name_cache}

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

    def _search_products(self, term: str) -> list[dict]:
        """
        Call /search?type=product with stats+offers so the returned products
        have the same structure as /product responses (usable by _to_match
        directly, so no follow-up ASIN lookup is needed). Same retry /
        rate-limit semantics as _get_products.
        """
        query = {
            "key": self.api_key,
            "domain": self.domain,
            "type": "product",
            "term": term,
            "stats": self.stats_days,
            "offers": self.offers_count,
            "page": 0,
        }
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.session.get(
                    f"{API_BASE}/search",
                    params=query,
                    timeout=self.timeout,
                )
            except requests.RequestException as e:
                log.warning("Keepa /search request error (attempt %d): %s", attempt, e)
                time.sleep(2 ** attempt)
                continue

            if resp.status_code == 200:
                data = resp.json()
                self._tokens_left = data.get("tokensLeft")
                self._refill_ms = data.get("refillIn")
                # /search returns full product records in `products` when
                # stats=N is set, otherwise just ASINs in `asinList`. We
                # request stats so we always get the rich form.
                return data.get("products") or []

            if resp.status_code == 429:
                wait_s = 30
                try:
                    body = resp.json()
                    if "refillIn" in body:
                        wait_s = max(5, int(body["refillIn"] / 1000) + 1)
                except json.JSONDecodeError:
                    pass
                log.warning("Keepa /search rate-limited (429), waiting %ss …", wait_s)
                time.sleep(wait_s)
                continue

            if resp.status_code in (401, 403):
                raise KeepaError(f"Auth failed on /search ({resp.status_code})")

            log.warning("Keepa /search returned %s (attempt %d): %s",
                        resp.status_code, attempt, resp.text[:200])
            time.sleep(2 ** attempt)

        raise KeepaError(f"Keepa /search failed after {self.max_retries} attempts")

    def _get_sellers(self, seller_ids: list[str]) -> dict[str, dict]:
        """
        Call /seller and return the raw {seller_id: seller_data} dict.
        Same retry/rate-limit semantics as _get_products.
        """
        query = {
            "key": self.api_key,
            "domain": self.domain,
            "seller": ",".join(seller_ids),
        }
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.session.get(
                    f"{API_BASE}/seller",
                    params=query,
                    timeout=self.timeout,
                )
            except requests.RequestException as e:
                log.warning("Keepa /seller request error (attempt %d): %s", attempt, e)
                time.sleep(2 ** attempt)
                continue

            if resp.status_code == 200:
                data = resp.json()
                self._tokens_left = data.get("tokensLeft")
                self._refill_ms = data.get("refillIn")
                return data.get("sellers") or {}

            if resp.status_code == 429:
                wait_s = 30
                try:
                    body = resp.json()
                    if "refillIn" in body:
                        wait_s = max(5, int(body["refillIn"] / 1000) + 1)
                except json.JSONDecodeError:
                    pass
                log.warning("Keepa /seller rate-limited (429), waiting %ss …", wait_s)
                time.sleep(wait_s)
                continue

            if resp.status_code in (401, 403):
                raise KeepaError(f"Auth failed on /seller ({resp.status_code})")

            log.warning("Keepa /seller returned %s (attempt %d): %s",
                        resp.status_code, attempt, resp.text[:200])
            time.sleep(2 ** attempt)

        raise KeepaError(f"Keepa /seller failed after {self.max_retries} attempts")

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

        # Buy Box seller identity ("Ships from" name on Amazon)
        # Keepa gives us the sellerId here; the human-readable name needs a
        # separate /seller lookup which the caller batches (see
        # KeepaClient.lookup_seller_names).
        bb_seller_id = stats.get("buyBoxSellerId")
        if isinstance(bb_seller_id, str) and bb_seller_id and bb_seller_id != "-1":
            m.buy_box_seller_id = bb_seller_id
        if stats.get("buyBoxIsAmazon"):
            # No point calling /seller — this is Amazon itself.
            m.buy_box_seller_name = "Amazon.com"
        # FBA-vs-FBM flag for the Buy Box winner, if Keepa exposes it.
        bb_is_fba = stats.get("buyBoxIsFBA")
        if isinstance(bb_is_fba, bool):
            m.buy_box_is_fba = bb_is_fba

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


def _normalize_brand(s: str) -> str:
    """
    Case- and punctuation-insensitive brand key.
    "Bob's Red Mill" → "bobsredmill",  "Alter Eco" → "altereco"
    Used for strict brand-match filtering on Keepa /search results
    (protects against false positives from Keepa's fuzzy title index).
    """
    if not s:
        return ""
    import re as _re
    return _re.sub(r"[^a-z0-9]", "", s.lower())


def _build_search_query(brand: str, title: str, max_words: int = 6) -> str:
    """
    Compose a clean Keepa /search term from supplier brand + title.

    - Strips punctuation that confuses text search (commas, parens, pipes)
    - Skips duplicate brand words at the start of the title
      (iHerb titles often start with the brand)
    - Caps title portion to `max_words` for a focused query

    Example:
      brand = "Alter Eco"
      title = "Alter Eco, Organic Granola, Dark Chocolate, 8 oz (227 g)"
      → "Alter Eco Organic Granola Dark Chocolate 8 oz"
    """
    if not brand or not title:
        return ""
    import re as _re
    clean_title = _re.sub(r"[,()\[\]|/]", " ", title)
    clean_title = _re.sub(r"\s+", " ", clean_title).strip()
    clean_brand = _re.sub(r"[,()\[\]|/]", " ", brand).strip()

    title_words = clean_title.split()
    brand_words_lower = [w.lower() for w in clean_brand.split()]

    # Skip a leading brand prefix in the title so we don't repeat it.
    i = 0
    while i < len(title_words) and i < len(brand_words_lower):
        if title_words[i].lower() == brand_words_lower[i]:
            i += 1
        else:
            break

    keep = title_words[i : i + max_words]
    if not keep:
        return clean_brand
    return f"{clean_brand} {' '.join(keep)}".strip()
