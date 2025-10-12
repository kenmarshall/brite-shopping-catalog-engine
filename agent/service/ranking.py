from __future__ import annotations

from pymongo.collection import Collection

from agent.utils.logging import get_logger

LOGGER = get_logger(__name__)


def text_search(collection: Collection, query: str, limit: int = 20) -> dict[str, float]:
    results = (
        collection.find({"$text": {"$search": query}}, {"score": {"$meta": "textScore"}})
        .sort("score", -1)
        .limit(limit)
    )
    return {str(item["_id"]): float(item.get("score", 0.0)) for item in results}


def regex_search(collection: Collection, query: str, limit: int = 20) -> dict[str, float]:
    regex = {"$regex": query, "$options": "i"}
    results = collection.find({"name": regex}).limit(limit)
    return {str(item["_id"]): 0.1 for item in results}


def blend_scores(
    vector_candidates: list[tuple[str, float]],
    text_candidates: dict[str, float],
    tag_candidates: dict[str, float],
) -> list[tuple[str, float]]:
    final: dict[str, float] = {}
    for product_id, score in vector_candidates:
        final[product_id] = final.get(product_id, 0.0) + 0.6 * score
    for product_id, score in text_candidates.items():
        final[product_id] = final.get(product_id, 0.0) + 0.3 * score
    for product_id, score in tag_candidates.items():
        final[product_id] = final.get(product_id, 0.0) + 0.1 * score
    return sorted(final.items(), key=lambda x: x[1], reverse=True)


__all__ = ["text_search", "regex_search", "blend_scores"]
