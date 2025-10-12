from __future__ import annotations

import asyncio
from collections.abc import Sequence

import httpx

from agent.config.settings import get_settings
from agent.utils.logging import get_logger

LOGGER = get_logger(__name__)


class EmbeddingError(RuntimeError):
    """Base class for embedding related failures."""


class EmbeddingModelUnavailable(EmbeddingError):
    """Raised when the configured embedding model is not available on the Ollama server."""


class EmbeddingResponseError(EmbeddingError):
    """Raised when the Ollama server returns an invalid embedding payload."""


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
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                detail = "embedding model not found"
                try:
                    detail_json = exc.response.json()
                    detail = detail_json.get("error", detail)
                except Exception:  # pragma: no cover - non-json response
                    detail = exc.response.text or detail
                raise EmbeddingModelUnavailable(detail) from exc
            raise
        data = response.json()
        embeddings = data.get("data", [])
        if isinstance(embeddings, dict) and "embedding" in embeddings:
            embeddings = [embeddings["embedding"]]
        else:
            embeddings = [item["embedding"] for item in embeddings]

        if not embeddings or not embeddings[0]:
            raise EmbeddingResponseError("empty embedding payload from Ollama")
        return embeddings

    async def close(self) -> None:
        await self._client.aclose()


async def embed_texts(texts: Sequence[str]) -> list[list[float]]:
    client = OllamaClient()
    try:
        return await client.embed(texts)
    finally:
        await client.close()


def embed_sync(texts: Sequence[str]) -> list[list[float]]:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            return loop.run_until_complete(embed_texts(texts))
        finally:
            asyncio.set_event_loop(None)
            loop.close()
    if loop.is_running():
        future = asyncio.run_coroutine_threadsafe(embed_texts(texts), loop)
        return future.result()
    return loop.run_until_complete(embed_texts(texts))


__all__ = [
    "EmbeddingError",
    "EmbeddingModelUnavailable",
    "EmbeddingResponseError",
    "OllamaClient",
    "embed_texts",
    "embed_sync",
]
