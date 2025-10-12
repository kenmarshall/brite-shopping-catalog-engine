"""Product deduplication using FAISS and sentence embeddings."""

from typing import Dict, Any, List, Tuple, Optional

from ..utils.embedding import embed_text
from ..utils.vector_search import search, add_vector
from ..utils.logger import get_logger

logger = get_logger(__name__)


class ProductDeduplicator:
    """Detect and handle duplicate products using FAISS."""
    
    def __init__(self, similarity_threshold: float = 0.15):
        """
        Initialize deduplicator.
        
        Args:
            similarity_threshold: Distance threshold below which products are considered duplicates.
                                Lower values mean more strict matching.
        """
        self.similarity_threshold = similarity_threshold
    
    def create_product_signature(self, product: Dict[str, Any]) -> str:
        """Create a text signature for a product for embedding."""
        parts = []
        
        # Always include name and price if available
        if product.get('name'):
            parts.append(product['name'])
        
        if product.get('price'):
            parts.append(product['price'])
        
        # Include brand and size if available
        if product.get('brand'):
            parts.append(product['brand'])
            
        if product.get('size'):
            parts.append(product['size'])
        
        # Include category if available
        if product.get('category'):
            parts.append(product['category'])
        
        # Include first part of description if available
        if product.get('description'):
            desc = product['description'][:100]  # First 100 chars
            parts.append(desc)
        
        return ' '.join(parts).strip()
    
    def is_duplicate(self, product: Dict[str, Any]) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """
        Check if a product is a duplicate.
        
        Returns:
            Tuple of (is_duplicate, existing_product_metadata)
        """
        signature = self.create_product_signature(product)
        if not signature:
            logger.warning("Cannot create signature for product: %s", product)
            return False, None
        
        # Generate embedding
        try:
            vector = embed_text(signature)
        except Exception as e:
            logger.error("Failed to generate embedding for product %s: %s", product.get('name'), e)
            return False, None
        
        # Search for similar products
        try:
            results = search(vector, top_k=3)
        except Exception as e:
            logger.error("Failed to search for similar products: %s", e)
            return False, None
        
        if not results:
            return False, None
        
        # Check if any result is below threshold
        for metadata, distance in results:
            if distance < self.similarity_threshold:
                logger.info(
                    "Duplicate detected: %s (distance: %.4f, threshold: %.4f)",
                    product.get('name'), distance, self.similarity_threshold
                )
                return True, metadata
        
        return False, None
    
    def add_product(self, product: Dict[str, Any]) -> bool:
        """
        Add a product to the vector store if it's not a duplicate.
        
        Returns:
            True if product was added, False if it was a duplicate.
        """
        is_dup, existing = self.is_duplicate(product)
        
        if is_dup:
            logger.info("Skipping duplicate product: %s", product.get('name'))
            return False
        
        # Add to vector store
        signature = self.create_product_signature(product)
        try:
            vector = embed_text(signature)
            metadata = {
                'name': product.get('name'),
                'price': product.get('price'),
                'brand': product.get('brand'),
                'url': product.get('url'),
                'signature': signature
            }
            add_vector(vector, metadata)
            logger.info("Added product to vector store: %s", product.get('name'))
            return True
        except Exception as e:
            logger.error("Failed to add product to vector store: %s", e)
            return False
    
    def find_similar_products(self, product: Dict[str, Any], top_k: int = 5) -> List[Tuple[Dict[str, Any], float]]:
        """Find similar products in the database."""
        signature = self.create_product_signature(product)
        if not signature:
            return []
        
        try:
            vector = embed_text(signature)
            results = search(vector, top_k=top_k)
            return results
        except Exception as e:
            logger.error("Failed to find similar products: %s", e)
            return []


# Global deduplicator instance
_deduplicator = None

def get_deduplicator(similarity_threshold: float = 0.15) -> ProductDeduplicator:
    """Get or create the global deduplicator."""
    global _deduplicator
    if _deduplicator is None:
        _deduplicator = ProductDeduplicator(similarity_threshold)
    return _deduplicator


def is_duplicate_product(product: Dict[str, Any]) -> bool:
    """Convenience function to check if a product is duplicate."""
    deduplicator = get_deduplicator()
    is_dup, _ = deduplicator.is_duplicate(product)
    return is_dup


def add_product_if_unique(product: Dict[str, Any]) -> bool:
    """Convenience function to add product if it's unique."""
    deduplicator = get_deduplicator()
    return deduplicator.add_product(product) 