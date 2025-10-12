"""Simple web scraper for fetching HTML content."""

import requests
from bs4 import BeautifulSoup
from typing import Optional, Dict, List, Any
from urllib.parse import urljoin, urlparse

from ..utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
}


class WebScraper:
    """Simple web scraper for fetching HTML content."""
    
    def __init__(self, timeout: int = 30, headers: Optional[Dict[str, str]] = None):
        self.timeout = timeout
        self.headers = headers or DEFAULT_HEADERS
        self.session = requests.Session()
        self.session.headers.update(self.headers)
    
    def scrape_url(self, url: str) -> Optional[str]:
        """Scrape HTML content from a URL."""
        try:
            logger.info("Scraping URL: %s", url)
            response = self.session.get(url, timeout=self.timeout)
            response.raise_for_status()
            
            # Check content type
            content_type = response.headers.get('content-type', '').lower()
            if 'text/html' not in content_type:
                logger.warning("URL does not return HTML content: %s", content_type)
                return None
            
            return response.text
            
        except requests.RequestException as e:
            logger.error("Failed to scrape URL %s: %s", url, e)
            return None
    
    def extract_product_html(self, html_content: str, selectors: Optional[Dict[str, List[str]]] = None) -> str:
        """Extract relevant product sections from HTML."""
        if not selectors:
            # Default selectors for common e-commerce patterns
            selectors = {
                'products': [
                    '.product', '.product-item', '.product-card',
                    '[data-product]', '.item', '.listing-item',
                    '.grid-item', '.shop-item'
                ],
                'product_lists': [
                    '.products', '.product-list', '.product-grid',
                    '.items', '.listings', '.shop-items'
                ]
            }
        
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # Remove scripts, styles, and other non-content elements
        for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'aside']):
            tag.decompose()
        
        # Try to find product containers
        product_sections = []
        
        # First try to find individual products
        for selector in selectors.get('products', []):
            products = soup.select(selector)
            if products:
                logger.info("Found %d products using selector: %s", len(products), selector)
                product_sections.extend(products)
        
        # If no individual products found, try product list containers
        if not product_sections:
            for selector in selectors.get('product_lists', []):
                containers = soup.select(selector)
                if containers:
                    logger.info("Found %d product containers using selector: %s", len(containers), selector)
                    product_sections.extend(containers)
        
        # If still nothing found, return a cleaned version of the main content
        if not product_sections:
            logger.warning("No product sections found, returning cleaned main content")
            main_content = soup.select_one('main') or soup.select_one('.main') or soup.select_one('#main')
            if main_content:
                return str(main_content)
            else:
                # Return body content with common unwanted elements removed
                body = soup.select_one('body')
                if body:
                    return str(body)
                return html_content
        
        # Combine found product sections
        combined_html = ""
        for section in product_sections[:20]:  # Limit to avoid huge content
            combined_html += str(section) + "\n"
        
        return combined_html
    
    def close(self):
        """Close the session."""
        self.session.close()


def scrape_website(url: str, extract_products: bool = True) -> Optional[str]:
    """Convenience function to scrape a website."""
    scraper = WebScraper()
    try:
        html_content = scraper.scrape_url(url)
        if not html_content:
            return None
        
        if extract_products:
            return scraper.extract_product_html(html_content)
        else:
            return html_content
    finally:
        scraper.close() 