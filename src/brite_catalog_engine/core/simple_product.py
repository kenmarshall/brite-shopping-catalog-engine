"""Simple product data structure for extraction and storage."""

from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
from datetime import datetime


@dataclass
class ProductData:
    """Simple product data for extraction and MongoDB storage."""
    
    # Basic extracted data
    name: str
    brand: Optional[str] = None
    size: Optional[str] = None
    price: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    tags: Optional[List[str]] = None
    
    # Source info
    source_url: str = ""
    image_url: Optional[str] = None
    
    # Metadata
    scraped_at: Optional[datetime] = None
    confidence_score: float = 1.0
    
    def __post_init__(self):
        if self.tags is None:
            self.tags = []
        if self.scraped_at is None:
            self.scraped_at = datetime.now()
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for MongoDB storage."""
        return {
            'name': self.name,
            'brand': self.brand,
            'size': self.size,
            'price': self.price,
            'description': self.description,
            'category': self.category,
            'tags': self.tags,
            'source_url': self.source_url,
            'image_url': self.image_url,
            'scraped_at': self.scraped_at,
            'confidence_score': self.confidence_score
        }
    
    @classmethod
    def from_scraped_data(cls, data: Dict[str, Any]) -> 'ProductData':
        """Create ProductData from scraped data dictionary."""
        return cls(
            name=data.get('name', ''),
            brand=data.get('brand'),
            size=data.get('size'),
            price=data.get('price'),
            description=data.get('description'),
            category=data.get('category'),
            tags=data.get('tags', []),
            source_url=data.get('url', ''),
            image_url=data.get('image_url'),
            confidence_score=data.get('confidence_score', 1.0)
        ) 