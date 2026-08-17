"""
Diagnose why a specific Amazon ASIN isn't being matched from an iHerb page.

Answers these questions in order:
  1. What UPC/GTIN does the iHerb product page actually publish (JSON-LD)?
  2. What UPC(s) does Keepa know about for the target Amazon ASIN?
  3. Do those UPCs match?
  4. If not, is the ASIN findable via a Keepa text search (title fallback)?

Run on the VPS from /opt/amazon-sourcing/app:

    sudo -u sourcing bash -c '
        cd /opt/amazon-sourcing/app
        source <(grep -E "^(KEEPA_API_KEY|HTTP_PROXY_URL)=" .env | sed "s/^/export /")
        ./venv/bin/python diagnose_missing_asin.py \
            --iherb-url "https://www.iherb.com/pr/alter-eco-organic-truffles-...." \
            --amazon-asin B0BLHRM711
    '
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("diagnose")


def _extract_iherb_ids(url: str) -> dict:
    """Fetch the iHerb product page and pull every identifier from JSON-LD."""
    from curl_client import CurlClient
    # Skip robots.txt — matches how the main pipeline runs (--no-robots).
    client = CurlClient(respect_robots=False)
    html = client.get(url)
    if not html:
        log.error("Failed to fetch iHerb page")
        return {}

    ids: dict = {"page_title": None, "brand": None, "identifiers": {}, "specs": {}}

    try:
        from bs4 import BeautifulSoup  # type: ignore
    except ImportError:
        log.error("bs4 not installed in this venv")
        return ids

    soup = BeautifulSoup(html, "lxml")
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        if not script.string:
            continue
        try:
            data = json.loads(script.string)
        except json.JSONDecodeError:
            continue

        blocks = data if isinstance(data, list) else [data]
        if isinstance(data, dict) and "@graph" in data and isinstance(data["@graph"], list):
            blocks = data["@graph"]

        for b in blocks:
            if not isinstance(b, dict):
                continue
            t = b.get("@type")
            is_product = "Product" in str(t if isinstance(t, str) else (t or ""))
            if isinstance(t, list):
                is_product = any("Product" in str(x) for x in t)
            if not is_product:
                continue

            ids["page_title"] = ids["page_title"] or b.get("name")
            brand = b.get("brand")
            if isinstance(brand, dict):
                ids["brand"] = ids["brand"] or brand.get("name")
            elif isinstance(brand, str):
                ids["brand"] = ids["brand"] or brand

            for key in ("gtin", "gtin8", "gtin12", "gtin13", "gtin14",
                        "mpn", "sku", "productID", "identifier"):
                val = b.get(key)
                if val:
                    ids["identifiers"].setdefault(key, str(val))

    return ids


def _keepa_lookup_asin(asin: str, key: str) -> dict:
    """Pull the raw Keepa product record for an ASIN so we can inspect UPCs."""
    from keepa import KeepaClient
    client = KeepaClient(api_key=key)
    products = client._get_products(asin=asin)
    if not products:
        return {}
    p = products[0]
    return {
        "asin": p.get("asin"),
        "title": p.get("title"),
        "brand": p.get("brand"),
        "manufacturer": p.get("manufacturer"),
        "upcList": p.get("upcList") or [],
        "eanList": p.get("eanList") or [],
        "categoryTree": [c.get("name") for c in (p.get("categoryTree") or [])
                         if c.get("name")],
        "parentAsin": p.get("parentAsin"),
        "variations": p.get("variations") or [],
        "productGroup": p.get("productGroup"),
    }


def _keepa_upc_lookup(upc: str, key: str) -> list:
    """See what ASINs Keepa currently maps to a given UPC."""
    from keepa import KeepaClient
    client = KeepaClient(api_key=key)
    products = client._get_products(code=upc)
    return [{"asin": p.get("asin"), "title": (p.get("title") or "")[:80]}
            for p in (products or [])]


def _keepa_search(term: str, key: str, limit: int = 10) -> list:
    """Search Keepa by title text — the fallback when UPCs don't line up."""
    import requests
    resp = requests.get(
        "https://api.keepa.com/search",
        params={"key": key, "domain": 1, "type": "product",
                "term": term, "page": 0},
        timeout=30,
    )
    if resp.status_code != 200:
        log.error("Keepa /search returned %s: %s", resp.status_code, resp.text[:200])
        return []
    data = resp.json()
    hits = data.get("productList") or data.get("asinList") or []
    if hits and isinstance(hits[0], str):
        return [{"asin": a} for a in hits[:limit]]
    return [{"asin": h.get("asin"), "title": (h.get("title") or "")[:80]}
            for h in hits[:limit]]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--iherb-url", required=True, help="iHerb product page URL")
    ap.add_argument("--amazon-asin", required=True, help="Expected Amazon ASIN")
    args = ap.parse_args()

    key = os.environ.get("KEEPA_API_KEY")
    if not key:
        log.error("KEEPA_API_KEY not set in environment")
        sys.exit(1)

    print()
    print("=" * 70)
    print(" 1. iHerb product page — what identifiers does it publish?")
    print("=" * 70)
    ih = _extract_iherb_ids(args.iherb_url)
    print(f"  Title       : {(ih.get('page_title') or '')[:80]}")
    print(f"  Brand       : {ih.get('brand')}")
    if ih.get("identifiers"):
        print("  Identifiers :")
        for k, v in ih["identifiers"].items():
            marker = "  <-- used for Keepa lookup" if k.startswith("gtin") else ""
            print(f"    {k:10} = {v}{marker}")
    else:
        print("  Identifiers : (NONE — iHerb didn't publish any GTIN/UPC/MPN)")
        print("                This alone would cause the miss — no UPC = no Keepa lookup.")

    # Pick the UPC we would have used
    ih_upc = None
    for k in ("gtin", "gtin13", "gtin12", "gtin14", "gtin8"):
        if ih.get("identifiers", {}).get(k):
            ih_upc = ih["identifiers"][k]
            break

    print()
    print("=" * 70)
    print(f" 2. Keepa: what UPC(s) does ASIN {args.amazon_asin} actually have?")
    print("=" * 70)
    kp = _keepa_lookup_asin(args.amazon_asin, key)
    if not kp:
        print(f"  Keepa has NO record for {args.amazon_asin} (ASIN may be delisted "
              "or non-US)")
        sys.exit(2)
    print(f"  Title       : {(kp.get('title') or '')[:80]}")
    print(f"  Brand       : {kp.get('brand')}")
    print(f"  Product grp : {kp.get('productGroup')}")
    print(f"  Parent ASIN : {kp.get('parentAsin')} "
          f"({len(kp.get('variations') or [])} variations)")
    print(f"  Categories  : {' > '.join(kp.get('categoryTree') or [])}")
    print(f"  UPC list    : {kp.get('upcList')}")
    print(f"  EAN list    : {kp.get('eanList')}")

    print()
    print("=" * 70)
    print(" 3. Verdict: does the iHerb UPC match Amazon?")
    print("=" * 70)
    if not ih_upc:
        print("  ✗ iHerb page has NO UPC → tool has nothing to look up.")
        print("    Fix path: add title/brand-based fallback for UPC-less iHerb pages.")
    elif ih_upc in (kp.get("upcList") or []) or ih_upc in (kp.get("eanList") or []):
        print(f"  ✓ MATCH — iHerb UPC {ih_upc} is in ASIN's UPC/EAN list.")
        print("    If tool still missed it, the bug is in scraper/filter logic, "
              "not identifier matching.")
    else:
        print(f"  ✗ MISMATCH — iHerb publishes UPC {ih_upc}")
        print(f"    but ASIN {args.amazon_asin} has UPC(s): {kp.get('upcList')}")
        print("    Fix path: title-based fallback or map iHerb UPC → parent ASIN's UPCs.")

        print()
        print("=" * 70)
        print(f" 3b. What ASIN(s) does Keepa map to iHerb's UPC {ih_upc}?")
        print("=" * 70)
        other = _keepa_upc_lookup(ih_upc, key)
        if not other:
            print("  (none — this UPC is not on Amazon at all under any ASIN)")
        else:
            for o in other:
                marker = "  <-- our target!" if o["asin"] == args.amazon_asin else ""
                print(f"  {o['asin']}  {o['title']}{marker}")

    print()
    print("=" * 70)
    print(" 4. Fallback: can Keepa /search find it by title?")
    print("=" * 70)
    if ih.get("page_title"):
        # Try brand+first-few-words-of-title
        brand = ih.get("brand") or ""
        title = ih["page_title"]
        term = f"{brand} {' '.join(title.split()[:6])}".strip()
        print(f"  Search term : {term!r}")
        results = _keepa_search(term, key)
        if not results:
            print("  (no results)")
        for r in results:
            marker = "  <-- our target!" if r.get("asin") == args.amazon_asin else ""
            title_txt = r.get("title", "")
            print(f"  {r.get('asin')}  {title_txt}{marker}")
    else:
        print("  (skipped — couldn't extract a page title from iHerb)")

    print()


if __name__ == "__main__":
    main()
