"""API export functionality for backend integration."""

import json
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
from pathlib import Path

from .product_models import Product
from ..utils.store_config import StoreRegion
from ..utils.db import _collection
from ..utils.logger import get_logger

logger = get_logger(__name__)


class CatalogExporter:
    """Export catalog data for API consumption."""
    
    def __init__(self):
        self.collection = _collection
    
    def export_products_for_api(
        self, 
        limit: Optional[int] = None,
        region: Optional[StoreRegion] = None,
        categories: Optional[List[str]] = None,
        min_confidence: float = 0.5
    ) -> List[Dict[str, Any]]:
        """Export products in API-ready format."""
        
        # Build query
        query = {}
        
        # Add confidence filter
        if min_confidence > 0:
            query["confidence_score"] = {"$gte": min_confidence}
        
        # Add region filter (if region data exists in products)
        if region:
            query["region"] = region.value
        
        # Add category filter
        if categories:
            query["category"] = {"$in": categories}
        
        # Fetch products
        cursor = self.collection.find(query)
        if limit:
            cursor = cursor.limit(limit)
        
        # Convert to API format
        api_products = []
        for doc in cursor:
            try:
                # Convert MongoDB doc to Product object
                product = Product.from_scraped_data(doc)
                api_products.append(product.to_api_dict())
            except Exception as e:
                logger.error("Failed to convert product to API format: %s", e)
                continue
        
        logger.info("Exported %d products for API", len(api_products))
        return api_products
    
    def export_mobile_search_index(self) -> Dict[str, Any]:
        """Export optimized search index for mobile app."""
        
        # Get all products with good confidence
        query = {"confidence_score": {"$gte": 0.6}}
        products = list(self.collection.find(query))
        
        # Build search index
        search_index = {
            "products": [],
            "categories": set(),
            "brands": set(),
            "keywords": set(),
            "metadata": {
                "generated_at": datetime.now().isoformat(),
                "total_products": len(products),
                "regions": []
            }
        }
        
        regions = set()
        
        for doc in products:
            try:
                product = Product.from_scraped_data(doc)
                mobile_data = product.to_mobile_dict()
                
                search_index["products"].append(mobile_data)
                
                # Collect metadata
                if product.category:
                    search_index["categories"].add(product.category)
                if product.brand:
                    search_index["brands"].add(product.brand)
                
                search_index["keywords"].update(product.search_keywords)
                    
            except Exception as e:
                logger.error("Failed to process product for search index: %s", e)
                continue
        
        # Convert sets to sorted lists for JSON serialization
        search_index["categories"] = sorted(list(search_index["categories"]))
        search_index["brands"] = sorted(list(search_index["brands"]))
        search_index["keywords"] = sorted(list(search_index["keywords"]))
        search_index["metadata"]["regions"] = sorted(list(regions))
        
        return search_index
    
    def export_category_breakdown(self) -> Dict[str, Any]:
        """Export category breakdown for mobile app navigation."""
        
        pipeline = [
            {"$match": {"confidence_score": {"$gte": 0.5}}},
            {"$group": {
                "_id": "$category",
                "count": {"$sum": 1},
                "brands": {"$addToSet": "$brand"},
                "avg_confidence": {"$avg": "$confidence_score"}
            }},
            {"$sort": {"count": -1}}
        ]
        
        result = list(self.collection.aggregate(pipeline))
        
        categories = {}
        for item in result:
            if item["_id"]:  # Skip null categories
                categories[item["_id"]] = {
                    "name": item["_id"],
                    "product_count": item["count"],
                    "brands": [b for b in item["brands"] if b],  # Filter null brands
                    "avg_confidence": round(item["avg_confidence"], 2)
                }
        
        return {
            "categories": categories,
            "total_categories": len(categories),
            "generated_at": datetime.now().isoformat()
        }
    
    def export_to_json_file(
        self, 
        output_dir: str = "exports",
        include_search_index: bool = True,
        include_category_breakdown: bool = True
    ) -> Dict[str, str]:
        """Export data to JSON files for API server to load."""
        
        output_path = Path(output_dir)
        output_path.mkdir(exist_ok=True)
        
        files_created = {}
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        # Export full catalog
        try:
            catalog_data = self.export_products_for_api()
            catalog_file = output_path / f"catalog_{timestamp}.json"
            
            with open(catalog_file, 'w', encoding='utf-8') as f:
                json.dump(catalog_data, f, indent=2, ensure_ascii=False)
            
            files_created["catalog"] = str(catalog_file)
            logger.info("Exported catalog to %s", catalog_file)
            
        except Exception as e:
            logger.error("Failed to export catalog: %s", e)
        
        # Export mobile search index
        if include_search_index:
            try:
                search_data = self.export_mobile_search_index()
                search_file = output_path / f"search_index_{timestamp}.json"
                
                with open(search_file, 'w', encoding='utf-8') as f:
                    json.dump(search_data, f, indent=2, ensure_ascii=False)
                
                files_created["search_index"] = str(search_file)
                logger.info("Exported search index to %s", search_file)
                
            except Exception as e:
                logger.error("Failed to export search index: %s", e)
        
        # Export category breakdown
        if include_category_breakdown:
            try:
                category_data = self.export_category_breakdown()
                category_file = output_path / f"categories_{timestamp}.json"
                
                with open(category_file, 'w', encoding='utf-8') as f:
                    json.dump(category_data, f, indent=2, ensure_ascii=False)
                
                files_created["categories"] = str(category_file)
                logger.info("Exported categories to %s", category_file)
                
            except Exception as e:
                logger.error("Failed to export categories: %s", e)
        
        return files_created
    
    def get_recent_updates(self, hours: int = 24) -> List[Dict[str, Any]]:
        """Get products updated within the last N hours."""
        
        cutoff = datetime.now() - timedelta(hours=hours)
        query = {
            "last_updated": {"$gte": cutoff},
            "confidence_score": {"$gte": 0.5}
        }
        
        products = []
        for doc in self.collection.find(query):
            try:
                product = Product.from_scraped_data(doc)
                products.append(product.to_api_dict())
            except Exception as e:
                logger.error("Failed to process recent update: %s", e)
                continue
        
        logger.info("Found %d recent updates in last %d hours", len(products), hours)
        return products


# Convenience functions
def export_for_api(limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Quick export for API consumption."""
    exporter = CatalogExporter()
    return exporter.export_products_for_api(limit=limit)


def create_mobile_search_index() -> Dict[str, Any]:
    """Create mobile-optimized search index."""
    exporter = CatalogExporter()
    return exporter.export_mobile_search_index()


def export_all_to_files(output_dir: str = "exports") -> Dict[str, str]:
    """Export all data to files for API integration."""
    exporter = CatalogExporter()
    return exporter.export_to_json_file(output_dir) 