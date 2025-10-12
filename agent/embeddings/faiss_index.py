from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

import faiss
import numpy as np

from agent.config.settings import get_settings
from agent.utils.logging import get_logger

LOGGER = get_logger(__name__)


class FaissIndex:
    def __init__(self, index_path: Path | None = None, metadata_path: Path | None = None) -> None:
        settings = get_settings()
        self.index_path = index_path or settings.faiss_index_path
        self.metadata_path = metadata_path or settings.faiss_meta_path
        self.index: faiss.Index | None = None
        self.metadata: dict[int, str] = {}
        self._load()

    def _load(self) -> None:
        if self.index_path.exists():
            LOGGER.info("Loading FAISS index from %s", self.index_path)
            self.index = faiss.read_index(str(self.index_path))
        if self.metadata_path.exists():
            self.metadata = json.loads(self.metadata_path.read_text())

    def _ensure_index(self, dim: int) -> None:
        if self.index is None:
            LOGGER.info("Creating new FAISS index with dim=%s", dim)
            self.index = faiss.IndexFlatIP(dim)
            self.metadata = {}

    def reset(self, dim: int) -> None:
        self.index = faiss.IndexFlatIP(dim)
        self.metadata = {}

    def add_vectors(self, vectors: Iterable[list[float]], ids: Iterable[str]) -> None:
        vectors_list = list(vectors)
        if not vectors_list:
            return
        dim = len(vectors_list[0])
        self._ensure_index(dim)
        arr = np.array(vectors_list, dtype=np.float32)
        faiss.normalize_L2(arr)
        start_id = len(self.metadata)
        self.index.add(arr)
        for offset, product_id in enumerate(ids):
            self.metadata[start_id + offset] = product_id
        self._persist()

    def _persist(self) -> None:
        if self.index is None:
            return
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        faiss.write_index(self.index, str(self.index_path))
        self.metadata_path.write_text(json.dumps(self.metadata))

    def search(self, vector: list[float], k: int = 10) -> list[tuple[str, float]]:
        if self.index is None:
            return []
        arr = np.array([vector], dtype=np.float32)
        faiss.normalize_L2(arr)
        scores, indices = self.index.search(arr, k)
        results: list[tuple[str, float]] = []
        for idx, score in zip(indices[0], scores[0], strict=False):
            if idx == -1:
                continue
            product_id = self.metadata.get(int(idx))
            if product_id:
                results.append((product_id, float(score)))
        return results


__all__ = ["FaissIndex"]
