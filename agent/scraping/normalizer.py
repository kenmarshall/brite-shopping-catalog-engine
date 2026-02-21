from __future__ import annotations

import hashlib
import re

from agent.db.models import SizeInfo

MEASURE_UNITS = r"ml|l|litre|liter|g|kg|oz|fl\s*oz|lb|lbs|gal|gallon|gallons|pt|pint|pints|qt|quart|quarts|cl|mg"
COUNT_UNITS = r"packs?|pk|ct|count"

MULTIPACK_PATTERN = re.compile(
    rf"\b(?P<count>\d+)\s*[xX]\s*(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>{MEASURE_UNITS})\b",
    re.IGNORECASE,
)
PACK_COUNT_PATTERN = re.compile(
    rf"\b(?:pack\s*of\s*(?P<count_a>\d+)|(?P<count_b>\d+)\s*(?:{COUNT_UNITS}))\b",
    re.IGNORECASE,
)
MEASURE_SIZE_PATTERN = re.compile(
    rf"\b(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>{MEASURE_UNITS})\b",
    re.IGNORECASE,
)
COUNT_SIZE_PATTERN = re.compile(
    rf"\b(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>{COUNT_UNITS})\b",
    re.IGNORECASE,
)
SIZE_TOKEN_PATTERN = re.compile(
    rf"\b\d+\s*[xX]\s*\d+(?:\.\d+)?\s*(?:{MEASURE_UNITS})\b"
    rf"|\bpack\s*of\s*\d+\b"
    rf"|\b\d+\s*(?:{COUNT_UNITS})\b"
    rf"|\b\d+(?:\.\d+)?\s*(?:{MEASURE_UNITS})\b",
    re.IGNORECASE,
)
CURRENCY_PATTERN = re.compile(r"([\d,.]+)")


FILLER_WORDS = re.compile(r"\b(the|and|with|in|of|for|a|an)\b", re.IGNORECASE)
DISPLAY_UPPER_TOKENS = {"bbq", "usa", "uk", "usd", "jmd", "xl", "xxl"}


def normalize_name(name: str) -> str:
    cleaned = re.sub(r"[^\w\s]", "", name.lower())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def strip_size_from_name(name: str) -> str:
    if not name:
        return ""
    cleaned = SIZE_TOKEN_PATTERN.sub(" ", name)
    cleaned = re.sub(r"[\(\)\[\]]", " ", cleaned)
    cleaned = re.sub(r"[-_/]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def normalize_display_name(name: str) -> str:
    cleaned = re.sub(r"\s+", " ", name).strip()
    if not cleaned:
        return ""
    normalized = cleaned.title()
    for token in DISPLAY_UPPER_TOKENS:
        normalized = re.sub(
            rf"\b{re.escape(token.title())}\b",
            token.upper(),
            normalized,
        )
    return normalized


def normalize_product_name(name: str) -> str:
    stripped = strip_size_from_name(name).strip()
    return normalize_display_name(stripped or name)


def normalize_name_for_matching(name: str) -> str:
    """Normalize name with size info and filler words removed, for cross-store matching."""
    cleaned = normalize_name(strip_size_from_name(name))
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


def _normalize_unit(unit: str) -> str:
    normalized = re.sub(r"\s+", "", unit.lower())
    mapping = {
        "packs": "pack",
        "pk": "pack",
        "ct": "count",
        "litre": "l",
        "liter": "l",
        "floz": "oz",
        "lbs": "lb",
        "gallon": "gal",
        "gallons": "gal",
        "pint": "pt",
        "pints": "pt",
        "quart": "qt",
        "quarts": "qt",
    }
    return mapping.get(normalized, normalized)


def parse_size(text: str) -> SizeInfo:
    if not text:
        return SizeInfo()
    multipack_match = MULTIPACK_PATTERN.search(text)
    if multipack_match:
        count = int(multipack_match.group("count"))
        value = float(multipack_match.group("value"))
        unit = _normalize_unit(multipack_match.group("unit"))
        return SizeInfo(value=value, unit=unit, pack_count=count)

    pack_count: int | None = None
    pack_match = PACK_COUNT_PATTERN.search(text)
    if pack_match:
        count_raw = pack_match.group("count_a") or pack_match.group("count_b")
        if count_raw:
            pack_count = int(count_raw)

    measure_match = MEASURE_SIZE_PATTERN.search(text)
    if measure_match:
        value = float(measure_match.group("value"))
        unit = _normalize_unit(measure_match.group("unit"))
        return SizeInfo(value=value, unit=unit, pack_count=pack_count)

    count_match = COUNT_SIZE_PATTERN.search(text)
    if count_match:
        value = float(count_match.group("value"))
        if pack_count is None:
            pack_count = int(value)
        return SizeInfo(value=value, unit="count", pack_count=pack_count)

    if pack_count is not None:
        return SizeInfo(value=float(pack_count), unit="count", pack_count=pack_count)

    return SizeInfo()


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
            f"{size.pack_count or ''}",
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
            f"{size.pack_count or ''}",
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "normalize_name",
    "strip_size_from_name",
    "normalize_display_name",
    "normalize_product_name",
    "normalize_name_for_matching",
    "normalize_brand",
    "normalize_category",
    "parse_price",
    "parse_size",
    "build_checksum",
    "build_match_key",
]
