from functools import lru_cache
from typing import List

from sentence_transformers import SentenceTransformer

from .logger import get_logger

logger = get_logger(__name__)

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"

@lru_cache(maxsize=1)
def _load_model() -> SentenceTransformer:
    logger.info("Loading embedding model %s", MODEL_NAME)
    return SentenceTransformer(MODEL_NAME)


def embed_text(text: str) -> List[float]:
    """Return embedding vector for given text."""
    model = _load_model()
    return model.encode(text, show_progress_bar=False).tolist()
