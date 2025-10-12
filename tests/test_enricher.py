from __future__ import annotations

import pytest

from agent.db.models import Product, SizeInfo
from agent.embeddings import enricher


def _build_product(**overrides) -> Product:
    payload = {
        "store_id": "demo",
        "store_name": "Demo Store",
        "name": "Grace Baked Beans",
        "normalized_name": "grace baked beans",
        "brand": "Grace",
        "size": SizeInfo(value=400.0, unit="g"),
        "category": "Canned Goods",
        "tags": ["canned goods"],
        "url": "https://example.com/grace-baked-beans",
        "checksum": "abc123",
    }
    payload.update(overrides)
    return Product(**payload)


@pytest.mark.asyncio()
async def test_enrich_product_merges_ai_tags(monkeypatch):
    monkeypatch.setattr(enricher, "_EMBEDDINGS_DISABLED", False)
    product = _build_product()

    async def fake_generate_tags(_prompt: str) -> list[str]:
        return ["Jamaican", "Spicy Foods", "Canned Goods"]

    async def fake_embed_texts(_texts):
        return [[0.1, 0.9]]

    monkeypatch.setattr(enricher, "_generate_tags", fake_generate_tags)
    monkeypatch.setattr(enricher, "embed_texts", fake_embed_texts)

    enriched = await enricher.enrich_product(product)

    assert enriched.embedding == [0.1, 0.9]
    assert enriched.tags == [
        "jamaican",
        "spicy foods",
        "canned goods",
        "grace",
        "g",
    ]


@pytest.mark.asyncio()
async def test_enrich_product_fallback_tags(monkeypatch):
    monkeypatch.setattr(enricher, "_EMBEDDINGS_DISABLED", False)
    product = _build_product(brand=None, category=None, tags=[])

    async def fake_generate_tags(_prompt: str) -> list[str]:
        return []

    async def fake_embed_texts(_texts):
        return [[1.0, 0.0]]

    monkeypatch.setattr(enricher, "_generate_tags", fake_generate_tags)
    monkeypatch.setattr(enricher, "embed_texts", fake_embed_texts)

    enriched = await enricher.enrich_product(product)

    # AI supplied no tags so the list remains empty.
    assert enriched.tags == []
    assert enriched.embedding == [1.0, 0.0]


@pytest.mark.asyncio()
async def test_embedding_disabled_after_model_missing(monkeypatch):
    from agent.embeddings.ollama_client import EmbeddingModelUnavailable

    monkeypatch.setattr(enricher, "_EMBEDDINGS_DISABLED", False)
    product = _build_product()

    async def fake_generate_tags(_prompt: str) -> list[str]:
        return ["one", "two", "three", "four", "five"]

    async def fake_embed_texts(_texts):
        raise EmbeddingModelUnavailable("model not found")

    monkeypatch.setattr(enricher, "_generate_tags", fake_generate_tags)
    monkeypatch.setattr(enricher, "embed_texts", fake_embed_texts)

    enriched = await enricher.enrich_product(product)

    assert enriched.embedding is None
    assert enricher._EMBEDDINGS_DISABLED is True
    assert enriched.tags == ["one", "two", "three", "four", "five"]

    async def should_not_be_called(_texts):  # pragma: no cover
        raise AssertionError("embed_texts should not be called when disabled")

    monkeypatch.setattr(enricher, "embed_texts", should_not_be_called)
    enriched = await enricher.enrich_product(_build_product())
    assert enriched.embedding is None


@pytest.mark.asyncio()
async def test_embedding_disabled_after_empty_payload(monkeypatch):
    monkeypatch.setattr(enricher, "_EMBEDDINGS_DISABLED", False)
    product = _build_product()

    async def fake_generate_tags(_prompt: str) -> list[str]:
        return ["alpha", "beta", "gamma", "delta", "epsilon"]

    async def fake_embed_texts(_texts):
        return []

    monkeypatch.setattr(enricher, "_generate_tags", fake_generate_tags)
    monkeypatch.setattr(enricher, "embed_texts", fake_embed_texts)

    enriched = await enricher.enrich_product(product)

    assert enriched.embedding is None
    assert enricher._EMBEDDINGS_DISABLED is True
    assert enriched.tags == ["alpha", "beta", "gamma", "delta", "epsilon"]


@pytest.mark.asyncio()
async def test_low_quality_tags_are_filtered(monkeypatch):
    monkeypatch.setattr(enricher, "_EMBEDDINGS_DISABLED", True)
    product = _build_product(
        name="Grace Ackees",
        normalized_name="grace ackees",
        category="Canned Fruit",
    )

    async def fake_generate_tags(_prompt: str) -> list[str]:
        return [
            "i cant provide information on a specific product",
            "assuming grace ackees is a type of food",
            "here are five possible tags",
            "Ackee Fruit",
            "Grace Brand",
        ]

    monkeypatch.setattr(enricher, "_generate_tags", fake_generate_tags)

    enriched = await enricher.enrich_product(product)

    assert enriched.tags == ["ackee fruit", "grace brand", "grace", "canned fruit", "ackees"]
