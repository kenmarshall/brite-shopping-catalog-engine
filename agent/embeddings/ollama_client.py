from __future__ import annotations

import asyncio
from collections.abc import Sequence

import httpx

from agent.config.settings import get_settings
from agent.utils.logging import get_logger

LOGGER = get_logger(__name__)


class OllamaClient:
    def __init__(self, base_url: str | None = None, model: str | None = None) -> None:
        settings = get_settings().ollama
        self.base_url = base_url or str(settings.base_url)
        self.model = model or settings.embed_model
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=60.0)

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        payload = {"model": self.model, "input": list(texts)}
        LOGGER.debug("Embedding %d texts", len(texts))
        response = await self._client.post("/api/embeddings", json=payload)
        response.raise_for_status()
        data = response.json()
        embeddings = data.get("data", [])
        if isinstance(embeddings, dict) and "embedding" in embeddings:
            return [embeddings["embedding"]]
        return [item["embedding"] for item in embeddings]

    async def close(self) -> None:
        await self._client.aclose()


async def embed_texts(texts: Sequence[str]) -> list[list[float]]:
    client = OllamaClient()
    try:
        return await client.embed(texts)
    finally:
        await client.close()


def embed_sync(texts: Sequence[str]) -> list[list[float]]:
    return asyncio.get_event_loop().run_until_complete(embed_texts(texts))


__all__ = ["OllamaClient", "embed_texts", "embed_sync"]
