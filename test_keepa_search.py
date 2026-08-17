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
    if TARGET in asins:
        rank = asins.index(TARGET) + 1
        print(f"  ✓ FOUND {TARGET} at rank {rank}")
    else:
        print(f"  ✗ {TARGET} NOT in results")
    # Show top 5 asins for eyeballing
    for i, a in enumerate(asins[:5], 1):
        marker = "  <-- target!" if a == TARGET else ""
        # Try to find title from products list
        title = ""
        for p in products:
            if p.get("asin") == a:
                title = (p.get("title") or "")[:70]
                break
        print(f"    {i}. {a}  {title}{marker}")

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
