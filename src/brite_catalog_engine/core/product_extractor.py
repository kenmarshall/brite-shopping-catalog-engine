"""Extract structured product information from HTML using Ollama."""

from typing import List, Dict, Any, Optional
from bs4 import BeautifulSoup

from utils.ollama_client import get_ollama_client
from utils.logger import get_logger

logger = get_logger(__name__)

PRODUCT_EXTRACTION_PROMPT = """
You are an AI assistant that extracts product information from HTML content.

Extract product information from the following HTML and return a JSON array of products.
Each product should have these fields:
- name: Product name (string)
- brand: Brand name if identifiable (string, or null)
- size: Size/quantity if mentioned (string, or null)
- price: Price with currency (string)
- description: Brief description if available (string, or null)
- category: Product category if obvious (string, or null)

Return only valid JSON. If no products are found, return an empty array [].

HTML content:
{html_content}
"""

TAG_GENERATION_PROMPT = """
You are an AI assistant that generates relevant search tags and keywords for products.

Given the following product information, generate 5-8 relevant search tags/keywords.
The tags should be:
- Lowercase
- Single words or short phrases
- Relevant for search and categorization
- Include product type, brand, category, and descriptive terms

Product information:
Name: {name}
Brand: {brand}
Size: {size}
Description: {description}
Category: {category}

Return the tags as a JSON array of strings.
"""


class ProductExtractor:
    """Extract product information from HTML using Ollama."""
    
    def __init__(self):
        self.ollama = get_ollama_client()
    
    def extract_products_from_html(self, html_content: str, max_length: int = 8000) -> List[Dict[str, Any]]:
        """Extract product information from HTML content."""
        if not self.ollama.is_available():
            logger.error("Ollama is not available for product extraction")
            return []
        
        # Truncate HTML if too long
        if len(html_content) > max_length:
            html_content = html_content[:max_length] + "..."
            logger.warning("HTML content truncated to %d characters", max_length)
        
        prompt = PRODUCT_EXTRACTION_PROMPT.format(html_content=html_content)
        
        result = self.ollama.generate_json(prompt)
        if not result:
            logger.error("Failed to extract products from HTML")
            return []
        
        # Handle both direct array and object with array
        if isinstance(result, list):
            products = result
        elif isinstance(result, dict) and 'products' in result:
            products = result['products']
        else:
            logger.error("Unexpected JSON structure from product extraction")
            return []
        
        # Validate and clean product data
        validated_products = []
        for product in products:
            if not isinstance(product, dict):
                continue
            
            # Ensure required fields
            if 'name' not in product or not product['name']:
                continue
            
            # Clean and validate product
            cleaned_product = {
                'name': str(product.get('name', '')).strip(),
                'brand': str(product.get('brand', '')).strip() if product.get('brand') else None,
                'size': str(product.get('size', '')).strip() if product.get('size') else None,
                'price': str(product.get('price', '')).strip(),
                'description': str(product.get('description', '')).strip() if product.get('description') else None,
                'category': str(product.get('category', '')).strip() if product.get('category') else None,
            }
            
            # Remove empty strings, keep None for missing values
            for key, value in cleaned_product.items():
                if value == '':
                    cleaned_product[key] = None
            
            validated_products.append(cleaned_product)
        
        logger.info("Extracted %d products from HTML", len(validated_products))
        return validated_products
    
    def generate_tags(self, product: Dict[str, Any]) -> List[str]:
        """Generate search tags for a product."""
        if not self.ollama.is_available():
            logger.error("Ollama is not available for tag generation")
            return []
        
        prompt = TAG_GENERATION_PROMPT.format(
            name=product.get('name', ''),
            brand=product.get('brand', 'N/A'),
            size=product.get('size', 'N/A'),
            description=product.get('description', 'N/A'),
            category=product.get('category', 'N/A')
        )
        
        result = self.ollama.generate_json(prompt)
        if not result:
            logger.error("Failed to generate tags for product: %s", product.get('name'))
            return []
        
        # Handle both direct array and object with array
        if isinstance(result, list):
            tags = result
        elif isinstance(result, dict) and 'tags' in result:
            tags = result['tags']
        else:
            logger.error("Unexpected JSON structure from tag generation")
            return []
        
        # Clean and validate tags
        validated_tags = []
        for tag in tags:
            if isinstance(tag, str) and tag.strip():
                validated_tags.append(tag.strip().lower())
        
        logger.info("Generated %d tags for product: %s", len(validated_tags), product.get('name'))
        return validated_tags


def extract_products_from_html(html_content: str) -> List[Dict[str, Any]]:
    """Convenience function to extract products from HTML."""
    extractor = ProductExtractor()
    return extractor.extract_products_from_html(html_content)


def generate_product_tags(product: Dict[str, Any]) -> List[str]:
    """Convenience function to generate tags for a product."""
    extractor = ProductExtractor()
    return extractor.generate_tags(product) 