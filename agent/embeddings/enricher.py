from __future__ import annotations

import asyncio
import re
from collections.abc import Iterable

import httpx

from agent.config.settings import get_settings
from agent.db.models import Product
from agent.embeddings.featurizer import build_embedding_text
from agent.embeddings.ollama_client import (
    EmbeddingError,
    EmbeddingModelUnavailable,
    EmbeddingResponseError,
    embed_texts,
)
from agent.utils.logging import get_logger

LOGGER = get_logger(__name__)

TAG_SPLIT_PATTERN = re.compile(r"[,\n]")
BAD_TAG_PHRASES = {
    "i cant",
    "i can",
    "here are",
    "assuming",
    "guideline",
    "unknown",
    "provide information",
    "general",
}
_EMBEDDINGS_DISABLED = False


def _clean_tag(tag: str) -> str | None:
    cleaned = tag.strip().strip("-• ").lower()
    if not cleaned:
        return None
    cleaned = re.sub(r"[^a-z0-9\s\-&/]", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return None
    return cleaned


def _is_quality_tag(tag: str) -> bool:
    if not tag:
        return False
    if len(tag) > 32:
        return False
    if tag.count(" ") >= 3:
        return False
    if not re.search(r"[a-z]", tag):
        return False
    if any(phrase in tag for phrase in BAD_TAG_PHRASES):
        return False
    return True


def _normalize_tags(tags: Iterable[str]) -> list[str]:
    seen: dict[str, str] = {}
    for tag in tags:
        cleaned = _clean_tag(tag)
        if cleaned and cleaned not in seen and _is_quality_tag(cleaned):
            seen[cleaned] = cleaned
    return list(seen.values())


async def _generate_tags(prompt: str) -> list[str]:
    settings = get_settings().ollama
    model = settings.tag_model
    if not model:
        return []
    try:
        async with httpx.AsyncClient(
            base_url=str(settings.base_url),
            timeout=60.0,
        ) as client:
            response = await client.post(
                "/api/generate",
                json={
                    "model": model,
                    "prompt": prompt,
                    "stream": False,
                },
            )
            response.raise_for_status()
            data = response.json()
    except Exception as exc:  # pragma: no cover - best effort
        LOGGER.warning("Tag generation failed: %s", exc)
        return []

    text = ""
    if isinstance(data, dict):
        text = (
            data.get("response")
            or data.get("text")
            or data.get("data")
            or data.get("message", {}).get("content", "")
        )
    elif isinstance(data, str):
        text = data
    if not text:
        return []

    return _normalize_tags(TAG_SPLIT_PATTERN.split(text))


def _fallback_tags(product: Product) -> list[str]:
    tags: list[str] = []
    if product.brand:
        tags.append(product.brand.lower())
    if product.category:
        tags.append(product.category.lower())
    if product.size.unit:
        tags.append(product.size.unit.lower())
    return _normalize_tags(tags)


def _name_tokens(product: Product) -> list[str]:
    tokens = re.split(r"\s+", product.normalized_name)
    return _normalize_tags(tokens)


async def enrich_product(product: Product) -> Product:
    prompt = (
        "You are a grocery taxonomy assistant. "
        "Given a product name, brand, and category, return exactly 5 concise tags "
        "that help shoppers find the item. "
        "Respond with a comma-separated list of tags only.\n\n"
        f"Product name: {product.name}\n"
        f"Brand: {product.brand or 'Unknown'}\n"
        f"Category: {product.category or 'Uncategorized'}\n"
        f"Details: size={product.size.value or ''} {product.size.unit or ''}\n"
    )

    ai_tags = await _generate_tags(prompt)
    if not ai_tags:
        LOGGER.debug("AI returned no tags for %s; leaving tag list empty", product.name)
        product.tags = []
    else:
        tags = list(ai_tags)
        if len(tags) < 5:
            for fallback_tag in _fallback_tags(product):
                if fallback_tag not in tags:
                    tags.append(fallback_tag)
                if len(tags) >= 5:
                    break
        if len(tags) < 5:
            for token in _name_tokens(product):
                if token not in tags:
                    tags.append(token)
                if len(tags) >= 5:
                    break
        product.tags = tags[:5]

    global _EMBEDDINGS_DISABLED
    if not _EMBEDDINGS_DISABLED:
        try:
            vectors = await embed_texts([build_embedding_text(product)])
            if not vectors:
                raise EmbeddingResponseError("no embeddings returned")
            product.embedding = vectors[0]
        except EmbeddingModelUnavailable as exc:
            _EMBEDDINGS_DISABLED = True
            LOGGER.error(
                "Embedding model unavailable (%s). "
                "Set OLLAMA_EMBED_MODEL to an installed model and run `ollama pull <model>`.",
                exc,
            )
        except EmbeddingResponseError as exc:
            _EMBEDDINGS_DISABLED = True
            LOGGER.error("Embedding response invalid: %s", exc)
        except EmbeddingError as exc:
            _EMBEDDINGS_DISABLED = True
            LOGGER.error("Embedding client error: %s", exc)
        except Exception as exc:  # pragma: no cover - best effort
            LOGGER.warning("Embedding generation failed for %s: %s", product.name, exc)

    return product


def enrich_product_sync(product: Product) -> Product:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            return loop.run_until_complete(enrich_product(product))
        finally:
            asyncio.set_event_loop(None)
            loop.close()
    if loop.is_running():
        raise RuntimeError("enrich_product_sync cannot run inside an active event loop")
    return loop.run_until_complete(enrich_product(product))


__all__ = ["enrich_product", "enrich_product_sync"]
