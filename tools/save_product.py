from langchain.tools import tool

from utils.embedding import embed_text
from utils.vector_search import search, add_vector
from utils.db import save_product as db_save_product
from utils.logger import get_logger

logger = get_logger(__name__)


@tool
def save_product(product: dict) -> str:
    """Deduplicate using FAISS and save product to MongoDB."""
    text = f"{product.get('name', '')} {product.get('price', '')}"
    vector = embed_text(text)
    hits = search(vector, top_k=1)
    if hits and hits[0][1] < 0.1:
        logger.info("Duplicate product detected: %s", product.get("name"))
        return "Duplicate"

    add_vector(vector, {"name": product.get("name"), "url": product.get("url")})
    db_save_product(product)
    logger.info("Product saved: %s", product.get("name"))
    return "Saved"
