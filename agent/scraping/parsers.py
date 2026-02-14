from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from agent.db.models import Product
from agent.scraping.normalizer import (
    build_checksum,
    build_match_key,
    normalize_brand,
    normalize_category,
    normalize_name,
    parse_price,
    parse_size,
)


@dataclass
class RawProduct:
    name: str
    price: float | None
    currency: str
    image_url: str | None
    size_hint: str | None
    brand_hint: str | None
    category_hint: str | None
    url: str


def parse_products(html: str, selectors: dict, base_url: str, currency: str = "JMD") -> list[RawProduct]:
    soup = BeautifulSoup(html, "html.parser")
    product_selector = selectors.get("product")
    elements = (
        soup.select(product_selector) if product_selector else soup.find_all(class_="product")
    )

    results: list[RawProduct] = []
    for el in elements:
        name_selector = selectors.get("name")
        name = (
            el.select_one(name_selector).get_text(strip=True)
            if name_selector
            else el.get_text(strip=True)
        )
        price_selector = selectors.get("price")
        price_text = el.select_one(price_selector).get_text(strip=True) if price_selector else None
        image_selector = selectors.get("image")
        image_el = (
            el.select_one(image_selector.split("::attr(")[0])
            if image_selector and "::attr" in image_selector
            else el.select_one(image_selector) if image_selector else None
        )
        image_url = None
        if image_selector and "::attr" in image_selector and image_el is not None:
            attr = re.findall(r"::attr\((.*?)\)", image_selector)
            if attr:
                image_url = image_el.get(attr[0])
        elif image_el is not None:
            image_url = image_el.get("src")
        size_selector = selectors.get("size_hint")
        size_hint = el.select_one(size_selector).get_text(strip=True) if size_selector else name
        brand_selector = selectors.get("brand_hint")
        brand_hint = el.select_one(brand_selector).get_text(strip=True) if brand_selector else None
        category_selector = selectors.get("category_hint")
        category_hint = (
            el.select_one(category_selector).get_text(strip=True) if category_selector else None
        )
        link = el.find("a", href=True)
        href = link["href"] if link else None
        url = urljoin(base_url, href) if href else base_url

        price = parse_price(price_text) if price_text else None
        if image_url and not image_url.startswith("http"):
            image_url = urljoin(base_url, image_url)

        results.append(
            RawProduct(
                name=name,
                price=price,
                currency=currency,
                image_url=image_url,
                size_hint=size_hint,
                brand_hint=brand_hint,
                category_hint=category_hint,
                url=url,
            )
        )
    return results


def raw_to_product(
    raw: RawProduct,
    store_id: str,
    store_name: str,
    default_brand: str | None = None,
) -> Product:
    normalized_name = normalize_name(raw.name)
    brand = normalize_brand(raw.brand_hint)
    if not brand and default_brand:
        brand = default_brand
    category = normalize_category(raw.category_hint)
    size = parse_size(raw.size_hint or raw.name)
    checksum = build_checksum(
        store_id=store_id, normalized_name=normalized_name, brand=brand, size=size
    )
    match_key = build_match_key(
        normalized_name=normalized_name, brand=brand, size=size
    )
    tags: list[str] = []
    if category:
        tags.append(category.lower())
    product = Product(
        store_id=store_id,
        store_name=store_name,
        name=raw.name,
        normalized_name=normalized_name,
        brand=brand,
        size=size,
        category=category,
        tags=tags,
        url=raw.url,
        image_url=raw.image_url,
        checksum=checksum,
        match_key=match_key,
        location_prices=[],
    )
    if raw.price is not None:
        product.location_prices.append(
            {
                "location_id": store_id,
                "store_name": store_name,
                "amount": raw.price,
                "currency": raw.currency,
            }
        )
        product.estimated_price = raw.price
    return product


__all__ = ["RawProduct", "parse_products", "raw_to_product"]
