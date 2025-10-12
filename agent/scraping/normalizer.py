from __future__ import annotations

import hashlib
import re

from agent.db.models import SizeInfo

SIZE_PATTERN = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>ml|l|g|kg|oz|lb|packs?|pk|ct)", re.IGNORECASE
)
CURRENCY_PATTERN = re.compile(r"([\d,.]+)")


def normalize_name(name: str) -> str:
    cleaned = re.sub(r"[^\w\s]", "", name.lower())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def normalize_brand(brand: str | None) -> str | None:
    if not brand:
        return None
    return brand.strip().title()


def normalize_category(category: str | None) -> str | None:
    if not category:
        return None
    return category.strip().title()


def parse_price(price_text: str) -> float | None:
    if not price_text:
        return None
    match = CURRENCY_PATTERN.search(price_text)
    if not match:
        return None
    value = match.group(1).replace(",", "")
    try:
        return float(value)
    except ValueError:
        return None


def parse_size(text: str) -> SizeInfo:
    if not text:
        return SizeInfo()
    match = SIZE_PATTERN.search(text)
    if not match:
        return SizeInfo()
    value = float(match.group("value"))
    unit = match.group("unit").lower()
    unit = unit.replace("packs", "pack").replace("pk", "pack").replace("ct", "count")
    return SizeInfo(value=value, unit=unit)


def build_checksum(
    store_id: str, normalized_name: str, brand: str | None, size: SizeInfo
) -> str:
    payload = "|".join(
        [
            store_id,
            normalized_name,
            brand or "",
            f"{size.value or ''}",
            size.unit or "",
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "normalize_name",
    "normalize_brand",
    "normalize_category",
    "parse_price",
    "parse_size",
    "build_checksum",
]
