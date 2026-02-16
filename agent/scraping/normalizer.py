from __future__ import annotations

import hashlib
import re

from agent.db.models import SizeInfo

SIZE_PATTERN = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*(?:x\s*\d+(?:\.\d+)?\s*)?(?P<unit>ml|l|litre|liter|g|kg|oz|fl\s*oz|lb|lbs|packs?|pk|ct|count)",
    re.IGNORECASE,
)
CURRENCY_PATTERN = re.compile(r"([\d,.]+)")


FILLER_WORDS = re.compile(r"\b(the|and|with|in|of|for|a|an)\b", re.IGNORECASE)


def normalize_name(name: str) -> str:
    cleaned = re.sub(r"[^\w\s]", "", name.lower())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def normalize_name_for_matching(name: str) -> str:
    """Normalize name with size info and filler words removed, for cross-store matching."""
    cleaned = normalize_name(name)
    # Remove size patterns to avoid mismatches on formatting (e.g., "400g" vs "400 g")
    cleaned = SIZE_PATTERN.sub("", cleaned)
    # Remove filler words
    cleaned = FILLER_WORDS.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def normalize_brand(brand: str | None) -> str | None:
    if not brand:
        return None
    return brand.strip().title()


def normalize_category(category: str | None) -> str | None:
    if not category:
        return None
    # Take the first category if comma-separated (e.g. "Baby & Infant,Medicine" -> "Baby & Infant")
    first = category.split(",")[0].strip()
    return first.title() if first else None


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
    unit = re.sub(r"\s+", "", match.group("unit").lower())
    unit = (
        unit.replace("packs", "pack")
        .replace("pk", "pack")
        .replace("ct", "count")
        .replace("litre", "l")
        .replace("liter", "l")
        .replace("floz", "oz")
        .replace("lbs", "lb")
    )
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


def build_match_key(
    normalized_name: str, brand: str | None, size: SizeInfo
) -> str:
    """Cross-store product identity — same product from different stores shares a match_key.

    Uses normalize_name_for_matching to strip size info and filler words from the name,
    so that "Grace Baked Beans 400g" and "Grace Baked Beans 400 g" produce the same key.
    The actual size comparison comes from the parsed SizeInfo fields.
    """
    matching_name = normalize_name_for_matching(normalized_name)
    payload = "|".join(
        [
            matching_name,
            brand or "",
            f"{size.value or ''}",
            size.unit or "",
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "normalize_name",
    "normalize_name_for_matching",
    "normalize_brand",
    "normalize_category",
    "parse_price",
    "parse_size",
    "build_checksum",
    "build_match_key",
]
