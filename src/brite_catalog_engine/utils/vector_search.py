import pickle
from pathlib import Path
from typing import Any, List, Tuple

import faiss
import numpy as np

from .logger import get_logger

logger = get_logger(__name__)

INDEX_DIR = Path("data/faiss_index")
INDEX_PATH = INDEX_DIR / "index.faiss"
META_PATH = INDEX_DIR / "metadata.pkl"
DIMENSION = 384

INDEX_DIR.mkdir(parents=True, exist_ok=True)


def _load_index() -> faiss.Index:
    if INDEX_PATH.exists():
        logger.info("Loading FAISS index")
        return faiss.read_index(str(INDEX_PATH))
    logger.info("Creating new FAISS index")
    return faiss.IndexFlatL2(DIMENSION)


def _load_metadata() -> List[Any]:
    if META_PATH.exists():
        with META_PATH.open("rb") as f:
            return pickle.load(f)
    return []


_index = _load_index()
_metadata = _load_metadata()


def _save_state() -> None:
    faiss.write_index(_index, str(INDEX_PATH))
    with META_PATH.open("wb") as f:
        pickle.dump(_metadata, f)


def search(vector: List[float], top_k: int = 1) -> List[Tuple[Any, float]]:
    if _index.ntotal == 0:
        return []
    vec = np.array([vector], dtype="float32")
    distances, indices = _index.search(vec, top_k)
    results = []
    for dist, idx in zip(distances[0], indices[0]):
        if idx == -1:
            continue
        results.append((_metadata[idx], float(dist)))
    return results


def add_vector(vector: List[float], meta: Any) -> None:
    vec = np.array([vector], dtype="float32")
    _index.add(vec)
    _metadata.append(meta)
    _save_state()
