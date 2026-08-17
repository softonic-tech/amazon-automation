"""
Sanity-check Keepa /search: can it find B0BLHRM711 with a clean query?

If YES → title-fallback fix is worth building.
If NO  → the ASIN is genuinely unindexed and we tell the client so.
"""
from __future__ import annotations

import json
import os
import sys

import requests

KEY = os.environ.get("KEEPA_API_KEY")
if not KEY:
    print("KEEPA_API_KEY not set")
    sys.exit(1)

TARGET = "B0BLHRM711"

QUERIES = [
    "Alter Eco Organic Dark Chocolate Granola 8 Ounce Pack of 6",
    "Alter Eco Dark Chocolate Granola 8 oz",
    "Alter Eco Organic Dark Chocolate Granola",
    "Alter Eco Dark Chocolate Granola Pack of 6",
]

for q in QUERIES:
    print(f"\n=== Query: {q!r} ===")
    resp = requests.get(
        "https://api.keepa.com/search",
        params={"key": KEY, "domain": 1, "type": "product",
                "term": q, "page": 0, "stats": 1},
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"  HTTP {resp.status_code}: {resp.text[:200]}")
        continue

    data = resp.json()
    tokens_left = data.get("tokensLeft")
    asins = data.get("asinList") or []
    products = data.get("products") or []
    print(f"  Tokens left: {tokens_left}")
    print(f"  asinList count: {len(asins)}   products count: {len(products)}")

    # Union of ASINs from both fields (Keepa returns them in `products`
    # when stats=1 is set, and in `asinList` when it's not).
    all_asins = [p.get("asin") for p in products if p.get("asin")]
    for a in asins:
        if a not in all_asins:
            all_asins.append(a)

    if TARGET in all_asins:
        rank = all_asins.index(TARGET) + 1
        print(f"  ✓✓✓ FOUND {TARGET} at rank {rank}")
    else:
        print(f"  ✗ {TARGET} NOT in results")

    # Top 10 for eyeballing
    print("  Top 10 results:")
    for i, a in enumerate(all_asins[:10], 1):
        title = ""
        brand = ""
        for p in products:
            if p.get("asin") == a:
                title = (p.get("title") or "")[:60]
                brand = p.get("brand") or ""
                break
        marker = "  <-- TARGET!" if a == TARGET else ""
        print(f"    {i:2d}. {a}  [{brand}]  {title}{marker}")

print()
print("=== Also: does Keepa know B0BLHRM711 at all in text index? ===")
resp = requests.get(
    "https://api.keepa.com/search",
    params={"key": KEY, "domain": 1, "type": "product",
            "term": "B0BLHRM711", "page": 0},
    timeout=30,
)
data = resp.json()
asins = data.get("asinList") or []
print(f"  Searching for the raw ASIN string returned {len(asins)} result(s)")
if TARGET in asins:
    print(f"  ✓ Keepa's text index knows about {TARGET} (found itself)")
else:
    print(f"  ✗ Keepa's text index does NOT even find {TARGET} by its own ASIN "
          "— it's genuinely unindexed and unfindable via /search")
