"""
Retailer registry
-----------------
Single source of truth for supplier sites shown in the UI and used by
the sitemap crawler / HTTP client picker.

Adding a new retailer later (goal: 15) means one entry here plus, if
needed, a SITE_SELECTORS fallback in scraper.ProductParser. JSON-LD
covers Shopify and most modern PDP pages without extra selectors.
"""

from __future__ import annotations

from typing import Any

# Hosts that fingerprint Python-requests TLS. curl-impersonate (and WARP
# when HTTP_PROXY_URL is set) is the same path that already works for iHerb.
CURL_HOSTS = (
    "iherb.com",
    "walmart.com",
    "samsclub.com",
    "tractorsupply.com",
    "farmandfleet.com",
    "maxwarehouse.com",
)

# Headed Chromium only — Akamai JS sensor, not a TLS fingerprint issue.
PLAYWRIGHT_HOSTS = ("zoro.com",)

# Product URL regex per registrable domain (no www.).
PRODUCT_PATTERNS: dict[str, str] = {
    "zoro.com":           r"/i/G\d+/?$",
    "grainger.com":       r"/product/",
    "iherb.com":          r"/pr/[^/?#]+/\d+/?$",
    "walmart.com":        r"/ip/[^/?#]+/\d+/?$",
    "samsclub.com":       r"/(?:ip/[^/?#]+/(?:prod)?\d+|p/prod\d+)/?$",
    "tractorsupply.com":  r"/tsc/product/[^/?#]+",
    "farmandfleet.com":   r"/products/\d+-[^/?#]+\.html$",
    "maxwarehouse.com":   r"/products/[^/?#]+/?$",
}

# UI dropdown. iHerb first — it is the proven production path on this VPS.
RETAILERS: list[dict[str, Any]] = [
    {
        "value": "https://www.iherb.com",
        "label": "iHerb",
        "sub": "supplements | US pricing forced",
        "extra_cost": 0,
    },
    {
        "value": "https://www.tractorsupply.com",
        "label": "Tractor Supply",
        "sub": "farm & ranch | prices in USD",
        "extra_cost": 0,
    },
    {
        "value": "https://www.walmart.com",
        "label": "Walmart",
        "sub": "general merchandise | prices in USD",
        "extra_cost": 0,
    },
    {
        "value": "https://www.farmandfleet.com",
        "label": "Farm & Fleet",
        "sub": "farm & fleet | prices in USD",
        "extra_cost": 0,
    },
    {
        "value": "https://www.maxwarehouse.com",
        "label": "Max Warehouse",
        "sub": "industrial | prices in USD",
        "extra_cost": 0,
    },
    {
        "value": "https://www.samsclub.com",
        "label": "Sam's Club",
        "sub": "warehouse club | prices in USD",
        "extra_cost": 0,
    },
    {
        "value": "https://www.zoro.com",
        "label": "Zoro",
        "sub": "US industrial | prices in USD",
        "extra_cost": 0,
    },
]


def host_of(url: str) -> str:
    from urllib.parse import urlparse
    return (urlparse(url or "").netloc or "").replace("www.", "").lower()


def uses_curl(url: str) -> bool:
    h = host_of(url)
    return any(h == d or h.endswith("." + d) for d in CURL_HOSTS)


def uses_playwright(url: str) -> bool:
    h = host_of(url)
    return any(h == d or h.endswith("." + d) for d in PLAYWRIGHT_HOSTS)


def extra_cost_for(url: str) -> float:
    u = (url or "").lower()
    for r in RETAILERS:
        if r["value"].split("//", 1)[-1].replace("www.", "") in u.replace("www.", ""):
            return float(r.get("extra_cost") or 0)
    return 0.0
