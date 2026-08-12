import json
import re
import sys
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup


URL = (
    "https://pk.iherb.com/pr/"
    "doctor-s-best-msm-with-optimsm-1-500-mg-120-tablets/3"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/151.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://pk.iherb.com/",
    "Cache-Control": "no-cache",
}


def clean_text(value: str | None) -> str | None:
    if not value:
        return None

    value = BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
    value = re.sub(r"\s+", " ", value).strip()
    return value or None


def find_product_json(data: Any) -> dict[str, Any] | None:
    """
    Recursively search JSON-LD data for an object whose @type is Product.
    """
    if isinstance(data, dict):
        object_type = data.get("@type")

        if object_type == "Product":
            return data

        if isinstance(object_type, list) and "Product" in object_type:
            return data

        for value in data.values():
            result = find_product_json(value)
            if result:
                return result

    elif isinstance(data, list):
        for item in data:
            result = find_product_json(item)
            if result:
                return result

    return None


def parse_json_ld(soup: BeautifulSoup) -> dict[str, Any] | None:
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text()

        if not raw.strip():
            continue

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue

        product = find_product_json(parsed)
        if product:
            return product

    return None


def extract_offer(product_json: dict[str, Any]) -> dict[str, Any]:
    offers = product_json.get("offers", {})

    if isinstance(offers, list):
        offers = offers[0] if offers else {}

    if not isinstance(offers, dict):
        offers = {}

    return {
        "price": offers.get("price"),
        "currency": offers.get("priceCurrency"),
        "availability": offers.get("availability"),
        "url": offers.get("url"),
    }


def extract_rating(product_json: dict[str, Any]) -> dict[str, Any]:
    rating = product_json.get("aggregateRating", {})

    if not isinstance(rating, dict):
        rating = {}

    return {
        "rating_value": rating.get("ratingValue"),
        "rating_count": rating.get("ratingCount")
        or rating.get("reviewCount"),
    }


def get_meta_content(
    soup: BeautifulSoup,
    *,
    name: str | None = None,
    property_name: str | None = None,
) -> str | None:
    if name:
        tag = soup.find("meta", attrs={"name": name})
    elif property_name:
        tag = soup.find("meta", attrs={"property": property_name})
    else:
        return None

    return tag.get("content") if tag else None


def scrape_product(url: str) -> dict[str, Any]:
    session = requests.Session()
    session.headers.update(HEADERS)

    response = session.get(url, timeout=30, allow_redirects=True)
    response.raise_for_status()

    html = response.text
    soup = BeautifulSoup(html, "lxml")

    product_json = parse_json_ld(soup) or {}

    offer = extract_offer(product_json)
    rating = extract_rating(product_json)

    brand = product_json.get("brand")
    if isinstance(brand, dict):
        brand = brand.get("name")

    image = product_json.get("image")
    if isinstance(image, list):
        images = image
    elif image:
        images = [image]
    else:
        og_image = get_meta_content(soup, property_name="og:image")
        images = [og_image] if og_image else []

    h1 = soup.find("h1")

    result = {
        "requested_url": url,
        "final_url": response.url,
        "status_code": response.status_code,
        "name": product_json.get("name")
        or clean_text(h1.get_text(" ", strip=True) if h1 else None),
        "brand": brand,
        "sku": product_json.get("sku"),
        "description": clean_text(
            product_json.get("description")
            or get_meta_content(soup, name="description")
        ),
        "price": offer["price"],
        "currency": offer["currency"],
        "availability": offer["availability"],
        "rating": rating["rating_value"],
        "rating_count": rating["rating_count"],
        "images": images,
        "canonical_url": (
            soup.find("link", rel="canonical").get("href")
            if soup.find("link", rel="canonical")
            else response.url
        ),
    }

    return result


def main() -> None:
    try:
        product = scrape_product(URL)

        output_path = Path("product.json")
        output_path.write_text(
            json.dumps(product, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        print(json.dumps(product, indent=2, ensure_ascii=False))
        print(f"\nSaved to: {output_path.resolve()}")

    except requests.RequestException as exc:
        print(f"Request failed: {exc}", file=sys.stderr)
        sys.exit(1)

    except Exception as exc:
        print(f"Scraping failed: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()