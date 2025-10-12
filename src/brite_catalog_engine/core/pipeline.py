"""Main processing pipeline for product data."""

from typing import List, Dict, Any, Tuple, Optional

from .web_scraper import WebScraper
from .product_extractor import ProductExtractor
from .deduplicator import ProductDeduplicator
from ..utils.db import save_product
from ..utils.logger import get_logger

logger = get_logger(__name__)


class ProductPipeline:
    """Main pipeline for processing product data."""
    
    def __init__(self, similarity_threshold: float = 0.15):
        """Initialize the pipeline with all components."""
        self.scraper = WebScraper()
        self.extractor = ProductExtractor()
        self.deduplicator = ProductDeduplicator(similarity_threshold)
        
        # Stats tracking
        self.stats = {
            'urls_processed': 0,
            'products_extracted': 0,
            'products_saved': 0,
            'duplicates_found': 0,
            'errors': 0
        }
    
    def process_url(self, url: str) -> Dict[str, Any]:
        """
        Process a single URL through the complete pipeline.
        
        Returns:
            Dictionary with processing results and statistics.
        """
        logger.info("Starting pipeline processing for URL: %s", url)
        self.stats['urls_processed'] += 1
        
        try:
            # Step 1: Scrape HTML
            html_content = self.scraper.scrape_url(url)
            if not html_content:
                logger.error("Failed to scrape URL: %s", url)
                self.stats['errors'] += 1
                return self._create_result(url, [], "Failed to scrape URL")
            
            # Extract product-relevant HTML
            product_html = self.scraper.extract_product_html(html_content)
            
            # Step 2: Extract structured product information
            products = self.extractor.extract_products_from_html(product_html)
            if not products:
                logger.warning("No products extracted from URL: %s", url)
                return self._create_result(url, [], "No products found")
            
            logger.info("Extracted %d products from URL", len(products))
            self.stats['products_extracted'] += len(products)
            
            # Step 3: Process each product
            processed_products = []
            for product in products:
                try:
                    processed_product = self._process_single_product(product, url)
                    if processed_product:
                        processed_products.append(processed_product)
                except Exception as e:
                    logger.error("Failed to process product %s: %s", product.get('name'), e)
                    self.stats['errors'] += 1
            
            return self._create_result(url, processed_products, "Success")
            
        except Exception as e:
            logger.error("Pipeline processing failed for URL %s: %s", url, e)
            self.stats['errors'] += 1
            return self._create_result(url, [], f"Pipeline error: {str(e)}")
    
    def _process_single_product(self, product: Dict[str, Any], source_url: str) -> Optional[Dict[str, Any]]:
        """Process a single product through the pipeline."""
        
        # Add source URL if not present
        if 'url' not in product:
            product['url'] = source_url
        
        # Step 3: Generate tags
        try:
            tags = self.extractor.generate_tags(product)
            product['tags'] = tags
        except Exception as e:
            logger.error("Failed to generate tags for product %s: %s", product.get('name'), e)
            product['tags'] = []
        
        # Step 4: Check for duplicates and add to vector store
        is_unique = self.deduplicator.add_product(product)
        
        if not is_unique:
            logger.info("Skipping duplicate product: %s", product.get('name'))
            self.stats['duplicates_found'] += 1
            return None
        
        # Step 5: Save to MongoDB
        try:
            save_product(product)
            self.stats['products_saved'] += 1
            logger.info("Successfully saved product: %s", product.get('name'))
            return product
        except Exception as e:
            logger.error("Failed to save product %s to database: %s", product.get('name'), e)
            self.stats['errors'] += 1
            return None
    
    def process_multiple_urls(self, urls: List[str]) -> List[Dict[str, Any]]:
        """Process multiple URLs."""
        results = []
        for url in urls:
            try:
                result = self.process_url(url)
                results.append(result)
            except Exception as e:
                logger.error("Failed to process URL %s: %s", url, e)
                results.append(self._create_result(url, [], f"Error: {str(e)}"))
        
        return results
    
    def _create_result(self, url: str, products: List[Dict[str, Any]], status: str) -> Dict[str, Any]:
        """Create a standardized result object."""
        return {
            'url': url,
            'status': status,
            'products_count': len(products),
            'products': products,
            'timestamp': self._get_timestamp()
        }
    
    def _get_timestamp(self) -> str:
        """Get current timestamp as string."""
        from datetime import datetime
        return datetime.now().isoformat()
    
    def get_stats(self) -> Dict[str, Any]:
        """Get pipeline processing statistics."""
        return dict(self.stats)
    
    def reset_stats(self):
        """Reset statistics counters."""
        for key in self.stats:
            self.stats[key] = 0
    
    def close(self):
        """Close pipeline resources."""
        self.scraper.close()


def process_url(url: str, similarity_threshold: float = 0.15) -> Dict[str, Any]:
    """Convenience function to process a single URL."""
    pipeline = ProductPipeline(similarity_threshold)
    try:
        return pipeline.process_url(url)
    finally:
        pipeline.close()


def process_urls(urls: List[str], similarity_threshold: float = 0.15) -> List[Dict[str, Any]]:
    """Convenience function to process multiple URLs."""
    pipeline = ProductPipeline(similarity_threshold)
    try:
        return pipeline.process_multiple_urls(urls)
    finally:
        pipeline.close() 