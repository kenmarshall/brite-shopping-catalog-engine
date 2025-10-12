"""Store configuration for different e-commerce sites and regions."""

from dataclasses import dataclass
from typing import Dict, List, Optional
from enum import Enum


class StoreRegion(Enum):
    """Supported store regions."""
    JAMAICA = "jamaica"
    CARIBBEAN = "caribbean"
    US = "united_states"
    CANADA = "canada"


@dataclass
class StoreConfig:
    """Configuration for a specific store."""
    
    # Store identification
    store_id: str
    name: str
    region: StoreRegion
    base_url: str
    
    # Scraping configuration
    product_selectors: Dict[str, List[str]]
    pagination_config: Optional[Dict[str, str]] = None
    rate_limit_delay: float = 1.0  # seconds between requests
    
    # Mobile app integration
    supports_online_ordering: bool = False
    has_store_locator: bool = False
    typical_categories: Optional[List[str]] = None
    
    # Data processing hints
    currency: str = "USD"
    common_brands: Optional[List[str]] = None
    category_mapping: Optional[Dict[str, str]] = None
    
    def __post_init__(self):
        if self.typical_categories is None:
            self.typical_categories = []
        if self.common_brands is None:
            self.common_brands = []
        if self.category_mapping is None:
            self.category_mapping = {}


# Store configurations for Jamaican and Caribbean markets
STORE_CONFIGS = {
    "hilo_shopping": StoreConfig(
        store_id="hilo_shopping",
        name="Hilo Shopping Jamaica",
        region=StoreRegion.JAMAICA,
        base_url="https://hiloshoppingja.com",
        product_selectors={
            'products': ['.product-item', '.product-card', '.item'],
            'product_lists': ['.products', '.product-grid', '.items'],
            'name': ['.product-title', '.product-name', 'h2', 'h3'],
            'price': ['.price', '.product-price', '.cost'],
            'image': ['img', '.product-image img'],
            'brand': ['.brand', '.manufacturer', '.product-brand'],
            'description': ['.description', '.product-description', 'p']
        },
        currency="JMD",  # Jamaican Dollar
        typical_categories=[
            "Fresh Produce", "Meat & Seafood", "Dairy", "Beverages",
            "Pantry Staples", "Snacks", "Personal Care", "Household"
        ],
        common_brands=[
            "Grace", "Walkerswood", "Blue Mountain", "Tastee", "Supper Fresh"
        ],
        supports_online_ordering=True,
        has_store_locator=True
    ),
    
    "progressive_grocers": StoreConfig(
        store_id="progressive_grocers",
        name="Progressive Grocers Jamaica",
        region=StoreRegion.JAMAICA,
        base_url="https://progressiveja.com",
        product_selectors={
            'products': ['.product', '.item', '[data-product]'],
            'product_lists': ['.product-list', '.grid'],
            'name': ['.title', '.name'],
            'price': ['.price', '.amount'],
            'image': ['.image img', 'img'],
        },
        currency="JMD",
        typical_categories=[
            "Groceries", "Fresh Foods", "Beverages", "Personal Care"
        ]
    ),
    
    "mega_mart": StoreConfig(
        store_id="mega_mart",
        name="MegaMart Caribbean",
        region=StoreRegion.CARIBBEAN,
        base_url="https://megamarttt.com",
        product_selectors={
            'products': ['.product-item', '.product'],
            'name': ['.product-name', 'h3'],
            'price': ['.price', '.product-price'],
        },
        currency="TTD",  # Trinidad & Tobago Dollar
        typical_categories=[
            "Groceries", "Electronics", "Home & Garden", "Beauty"
        ]
    )
}


def get_store_config(store_id: str) -> Optional[StoreConfig]:
    """Get configuration for a specific store."""
    return STORE_CONFIGS.get(store_id)


def get_stores_by_region(region: StoreRegion) -> List[StoreConfig]:
    """Get all stores for a specific region."""
    return [config for config in STORE_CONFIGS.values() if config.region == region]


def get_all_store_ids() -> List[str]:
    """Get all available store IDs."""
    return list(STORE_CONFIGS.keys())


def get_jamaican_stores() -> List[StoreConfig]:
    """Get stores specifically for Jamaica market."""
    return get_stores_by_region(StoreRegion.JAMAICA)


# Default store for testing
DEFAULT_STORE = "hilo_shopping" 