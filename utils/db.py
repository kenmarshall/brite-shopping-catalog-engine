import os

from pymongo import MongoClient

from .logger import get_logger

logger = get_logger(__name__)

MONGO_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
DB_NAME = os.getenv("MONGODB_DB", "brite")
COLLECTION_NAME = os.getenv("MONGODB_COLLECTION", "products")

_client = MongoClient(MONGO_URI)
_db = _client[DB_NAME]
_collection = _db[COLLECTION_NAME]


def save_product(product: dict) -> None:
    logger.info("Saving product to MongoDB: %s", product.get("name"))
    _collection.insert_one(product)


def find_by_url(url: str):
    return _collection.find_one({"url": url})
