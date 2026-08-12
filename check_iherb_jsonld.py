"""
Diagnose iHerb JSON-LD content.
Fetches 5 random iHerb US products and shows what fields are published.
Answers: does iHerb publish UPC/gtin12 for these products, or not?

Usage:
    python check_iherb_jsonld.py
"""
import json
import re
import sys
from pathlib import Path

from curl_client import CurlClient
from bs4 import BeautifulSoup

# Force US pricing via the same cookies that worked in your curl test
client = CurlClient(delay=2.0, respect_robots=False)
client.set_cookie(
    "ih-preference",
    "country=US&currency=USD&language=en-US&store=0",
    domain=".iherb.com",
)
client.set_cookie(
    "iher-pref1",
    "lan=en-US&sccode=US&scurcode=USD&storeid=0",
    domain=".iherb.com",
)
client.set_header("Accept-Language", "en-US,en;q=0.9")

# Discover 5 random URLs from iHerb sitemap
from sitemap import SitemapCrawler
crawler = SitemapCrawler(client)
urls = crawler.collect_urls(
    base_url="https://www.iherb.com",
    limit=5,
    sample_random=True,
)
print(f"\nFound {len(urls)} URLs to inspect:\n")

for i, url in enumerate(urls, 1):
    print(f"{'=' * 70}")
    print(f"[{i}/{len(urls)}] {url}")
    print(f"{'=' * 70}")

    html = client.get(url)
    if html is None:
        print("  FETCH FAILED\n")
        continue

    soup = BeautifulSoup(html, "html.parser")

    # Find all JSON-LD scripts
    scripts = soup.find_all("script", type="application/ld+json")
    print(f"  Found {len(scripts)} JSON-LD script(s)")

    for si, tag in enumerate(scripts, 1):
        raw = tag.string or tag.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            print(f"  Script {si}: PARSE ERROR — {e}")
            continue

        # Unwrap @graph if present
        blocks = []
        if isinstance(data, list):
            blocks = data
        elif isinstance(data, dict):
            if "@graph" in data:
                blocks = data["@graph"]
                print(f"  Script {si}: @graph wrapper with {len(blocks)} blocks")
            else:
                blocks = [data]

        for b in blocks:
            if not isinstance(b, dict):
                continue
            t = b.get("@type", "")
            if "Product" not in str(t):
                continue

            print(f"\n  --> Product block found (@type={t!r})")
            # Show every top-level field so we know what's actually there
            for key in sorted(b.keys()):
                val = b[key]
                if isinstance(val, (dict, list)):
                    val_str = json.dumps(val, ensure_ascii=False)[:80]
                else:
                    val_str = str(val)[:80]
                print(f"       {key}: {val_str}")

            # Explicit UPC/GTIN check
            print()
            gtin_check = {k: b.get(k) for k in
                          ["gtin", "gtin8", "gtin12", "gtin13", "gtin14",
                           "mpn", "sku", "productID", "identifier"]
                          if b.get(k)}
            if gtin_check:
                print(f"  Identifier fields present: {gtin_check}")
            else:
                print("  ⚠️  NO identifier fields (gtin/mpn/sku) found in this product block")

    print()

client.close()
