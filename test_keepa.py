"""
Keepa integration diagnostic.
Run this ONCE after installing keepa.py to verify:
  1. Your API key works
  2. UPC → ASIN lookup returns real products
  3. All the fields we need are populated

Usage:
    # Option A — key on command line:
    python test_keepa.py YOUR_API_KEY

    # Option B — key in environment variable (safer):
    set KEEPA_API_KEY=your_key      (Windows)
    export KEEPA_API_KEY=your_key   (Linux/Mac)
    python test_keepa.py

    # With a specific UPC to test:
    python test_keepa.py --upc 753950002869

    # Read UPCs from your iherb_batch.json:
    python test_keepa.py --from iherb_batch.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("keepa_test")

try:
    from keepa import KeepaClient, KeepaError
except ImportError:
    print("ERROR: keepa.py not found in current folder.")
    sys.exit(1)


def extract_upcs_from_json(path: Path, limit: int = 3) -> list[str]:
    """Pull the first few UPCs out of a scraper batch JSON."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        return []
    upcs = []
    for product in data:
        specs = product.get("specs") or {}
        upc = specs.get("UPC") or specs.get("gtin13") or product.get("upc")
        if upc:
            upcs.append(str(upc).strip())
        if len(upcs) >= limit:
            break
    return upcs


def print_match(m) -> None:
    print(f"\n  ASIN         : {m.asin}")
    print(f"  Title        : {(m.title or '')[:80]}")
    print(f"  Brand        : {m.brand}")
    print(f"  Category     : {m.category}")
    print(f"  UPC          : {m.upc}")
    print(f"  ── Pricing ──")
    print(f"  Buy Box      : ${m.buy_box_price}" if m.buy_box_price else "  Buy Box      : n/a")
    print(f"  New price    : ${m.new_price}" if m.new_price else "  New price    : n/a")
    print(f"  Avg 90d      : ${m.avg_price_90d}" if m.avg_price_90d else "  Avg 90d      : n/a")
    print(f"  ── Sourcing signals ──")
    print(f"  Amazon on listing : {'YES' if m.amazon_on_listing else 'no'}")
    print(f"  FBM sellers       : {m.fbm_seller_count}")
    print(f"  FBA sellers       : {m.fba_seller_count}")
    print(f"  BSR               : {m.bsr}")
    print(f"  BSR avg 90d       : {m.bsr_avg_90d}")
    print(f"  Rating            : {m.rating} ({m.review_count} reviews)")


def check_client_rules(m) -> None:
    """Show whether this product would pass the client's stated rules."""
    print(f"\n  ── Client rule check ──")
    checks = [
        ("Amazon NOT on listing", not m.amazon_on_listing),
        ("≥4 FBM sellers",         m.fbm_seller_count >= 4),
        ("Rating ≥ 3.5",           (m.rating or 0) >= 3.5),
        ("Has BSR",                m.bsr is not None),
        ("Has Buy Box price",      m.buy_box_price is not None),
    ]
    for label, passed in checks:
        icon = "✓" if passed else "✗"
        print(f"  {icon} {label}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("api_key", nargs="?", default=None,
                    help="Keepa API key (or set KEEPA_API_KEY env var)")
    ap.add_argument("--upc", help="Specific UPC to test")
    ap.add_argument("--from", dest="from_json", default="iherb_batch.json",
                    help="Read UPCs from this scraper output (default: iherb_batch.json)")
    ap.add_argument("--limit", type=int, default=3,
                    help="How many UPCs to test (default: 3)")
    args = ap.parse_args()

    # Resolve API key
    key = args.api_key or os.environ.get("KEEPA_API_KEY")
    if not key:
        print("ERROR: No API key. Pass as argument or set KEEPA_API_KEY env var.")
        print("Usage: python test_keepa.py YOUR_KEY")
        sys.exit(1)

    # Resolve UPCs to test
    if args.upc:
        upcs = [args.upc]
    else:
        path = Path(args.from_json)
        if not path.exists():
            print(f"ERROR: {path} not found. Pass --upc UPC or --from FILE.json")
            sys.exit(1)
        upcs = extract_upcs_from_json(path, args.limit)
        if not upcs:
            print(f"ERROR: No UPCs found in {path}. Check that specs.UPC is populated.")
            sys.exit(1)
        log.info("Loaded %d UPC(s) from %s", len(upcs), path)

    # Initialize client
    try:
        client = KeepaClient(api_key=key)
    except KeepaError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    # Run the tests
    print(f"\n{'=' * 60}")
    print(f" Testing {len(upcs)} UPC(s) against Keepa (amazon.com US)")
    print(f"{'=' * 60}")

    hits = 0
    misses = 0
    for i, upc in enumerate(upcs, 1):
        print(f"\n[{i}/{len(upcs)}] UPC {upc}")
        try:
            match = client.lookup_by_upc(upc)
        except KeepaError as e:
            print(f"  API error: {e}")
            sys.exit(1)

        if match is None:
            print("  → No Amazon match (UPC not in Keepa's index or not on amazon.com)")
            misses += 1
        else:
            hits += 1
            print_match(match)
            check_client_rules(match)

    # Summary
    print(f"\n{'=' * 60}")
    print(f" SUMMARY")
    print(f"{'=' * 60}")
    print(f" UPCs tested   : {len(upcs)}")
    print(f" Matches found : {hits}")
    print(f" No matches    : {misses}")
    print(f" Tokens left   : {client.tokens_left}")
    print()

    if hits == 0:
        print("⚠️  No matches at all. Possible causes:")
        print("   • These specific UPCs aren't on amazon.com")
        print("   • Data quality issue in iherb_batch.json (verify UPC format)")
        print("   • Try --upc 753950002869 (a known-good MSM supplement UPC)")
    elif hits < len(upcs):
        print("Some UPCs matched, some didn't — this is normal for iHerb.")
        print("Products that only ship internationally often aren't on amazon.com US.")


if __name__ == "__main__":
    main()
