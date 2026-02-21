"""Standard product categories for Brite Shopping.

A canonical list of supermarket categories common to Jamaican grocery stores.
Used by the scraping pipeline (AI assignment) and the admin curator.
"""

STANDARD_CATEGORIES: list[str] = [
    "Alcohol & Spirits",
    "Baby & Infant",
    "Bakery & Bread",
    "Baking Supplies",
    "Beverages",
    "Canned Goods",
    "Condiments & Sauces",
    "Confectionery & Sweets",
    "Cooking Oils & Spices",
    "Dairy & Eggs",
    "Deli & Prepared Foods",
    "Frozen Foods",
    "Fruits & Vegetables",
    "Health & Beauty",
    "Household & Cleaning",
    "Jamaican Specialties",
    "Meat & Seafood",
    "Paper & Disposables",
    "Pet Supplies",
    "Rice, Pasta & Grains",
    "Snacks",
    "Soups & Instant Meals",
    "Breakfast & Cereal",
    "Water & Ice",
    "Other",
]

STANDARD_CATEGORIES_SET: frozenset[str] = frozenset(STANDARD_CATEGORIES)

# Lower-case lookup for fuzzy matching from scraped data
_LOWER_LOOKUP: dict[str, str] = {c.lower(): c for c in STANDARD_CATEGORIES}


def match_standard_category(raw: str | None) -> str | None:
    """Return the standard category if *raw* matches one (case-insensitive).

    Returns ``None`` when no direct match is found — caller should fall back
    to AI assignment or leave the category for curator review.
    """
    if not raw:
        return None
    return _LOWER_LOOKUP.get(raw.strip().lower())
