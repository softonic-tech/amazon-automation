import json
import re
from pathlib import Path

from bs4 import BeautifulSoup


html_path = Path("product.html")

if not html_path.exists():
    raise FileNotFoundError("product.html was not found.")

html = html_path.read_text(encoding="utf-8", errors="ignore")
soup = BeautifulSoup(html, "lxml")

result = {
    "title": None,
    "price": None,
    "currency": None,
    "availability": None,
    "sku": None,
}

# Extract JSON-LD product data
for script in soup.find_all("script", type="application/ld+json"):
    try:
        data = json.loads(script.get_text(strip=True))
    except (json.JSONDecodeError, TypeError):
        continue

    items = data if isinstance(data, list) else [data]

    for item in items:
        if not isinstance(item, dict):
            continue

        if item.get("@type") == "Product":
            result["title"] = item.get("name")
            result["sku"] = item.get("sku")

            offers = item.get("offers", {})

            if isinstance(offers, list) and offers:
                offers = offers[0]

            if isinstance(offers, dict):
                result["price"] = offers.get("price")
                result["currency"] = offers.get("priceCurrency")
                result["availability"] = offers.get("availability")

# Fallback title
if not result["title"] and soup.title:
    result["title"] = soup.title.get_text(strip=True)

# Fallback search for price/currency
if not result["price"]:
    match = re.search(
        r'"price"\s*:\s*"?([0-9]+(?:\.[0-9]+)?)"?',
        html,
        re.IGNORECASE,
    )
    if match:
        result["price"] = match.group(1)

if not result["currency"]:
    match = re.search(
        r'"priceCurrency"\s*:\s*"([A-Z]{3})"',
        html,
        re.IGNORECASE,
    )
    if match:
        result["currency"] = match.group(1).upper()

print(json.dumps(result, indent=2, ensure_ascii=False))