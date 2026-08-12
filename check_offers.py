"""
Diagnose the raw offers array structure — one-time check to verify FBM extraction.
Run: python check_offers.py
"""
import json
import os
import sys

import requests

UPC = "753950000971"  # Doctor's Best MSM — 5874 reviews so surely has multiple sellers

key = os.environ.get("KEEPA_API_KEY")
if not key:
    print("ERROR: set KEEPA_API_KEY env var first")
    sys.exit(1)

r = requests.get(
    "https://api.keepa.com/product",
    params={"key": key, "domain": 1, "code": UPC, "stats": 90, "offers": 20},
    timeout=30,
)
data = r.json()
p = data["products"][0]

print(f"ASIN: {p['asin']}")
print(f"Title: {p['title'][:70]}")
print()

offers = p.get("offers") or []
print(f"offers array length: {len(offers)}")
print(f"liveOffersOrder: {p.get('liveOffersOrder')}")
print()

# Also check stats for seller counts
stats = p.get("stats") or {}
current = stats.get("current") or []
print(f"stats.current length: {len(current)}")
print(f"stats.current[11] (New FBM price): {current[11] if len(current) > 11 else 'N/A'}")
print(f"stats.current[12] (FBM count?):    {current[12] if len(current) > 12 else 'N/A'}")
print(f"stats.offerCountFBA: {stats.get('offerCountFBA')}")
print(f"stats.offerCountFBM: {stats.get('offerCountFBM')}")
print(f"stats.offerCountNew: {stats.get('offerCountNew')}")
print(f"stats.totalOfferCount: {stats.get('totalOfferCount')}")
print()

# Show every offer's fingerprint
print("=== EVERY OFFER (first 15) ===")
for i, o in enumerate(offers[:15]):
    keys_of_interest = ["sellerId", "isAmazon", "isFBA", "isPrime", "isMAP",
                        "condition", "isShippable", "lastSeen", "price"]
    fingerprint = {k: o.get(k) for k in keys_of_interest if k in o}
    print(f"  [{i}] {fingerprint}")

print()
print("=== ALL KEYS IN FIRST OFFER ===")
if offers:
    print(list(offers[0].keys()))
