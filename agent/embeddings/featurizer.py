from __future__ import annotations

from agent.db.models import Product


def build_embedding_text(product: Product) -> str:
    parts: list[str] = [product.name]
    if product.brand:
        parts.append(product.brand)
    if product.category:
        parts.append(product.category)
    if product.tags:
        parts.extend(product.tags)
    return " | ".join(parts)


__all__ = ["build_embedding_text"]
