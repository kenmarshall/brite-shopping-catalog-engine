from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent.db.models import LocationPrice, Product, SizeInfo
from agent.embeddings.faiss_index import FaissIndex
from agent.service import api


class FakeCursor(list):
    def sort(self, *args, **kwargs):
        return self

    def limit(self, count: int) -> FakeCursor:
        return FakeCursor(self[:count])


class FakeCollection:
    def __init__(self) -> None:
        self._docs: list[dict[str, Any]] = []
        self._counter = 0

    def insert_one(self, payload: dict[str, Any]):
        doc = dict(payload)
        if "_id" not in doc:
            doc["_id"] = str(self._counter)
            self._counter += 1
        self._docs.append(doc)
        return type("InsertResult", (), {"inserted_id": doc["_id"]})

    def _match(self, doc: dict[str, Any], query: dict[str, Any]) -> bool:
        if not query:
            return True
        if "$text" in query:
            needle = query["$text"].get("$search", "").lower()
            return needle in doc.get("name", "").lower()
        for key, value in query.items():
            if isinstance(value, dict) and "$regex" in value:
                pattern = value["$regex"].lower()
                return pattern in doc.get(key, "").lower()
            if doc.get(key) != value:
                return False
        return True

    def find(self, query: dict[str, Any] | None = None, *_args, **_kwargs) -> FakeCursor:
        query = query or {}
        matched = [doc for doc in self._docs if self._match(doc, query)]
        return FakeCursor(matched)

    def find_one(self, query: dict[str, Any]) -> dict[str, Any] | None:
        for doc in self._docs:
            match = True
            for key, value in query.items():
                if doc.get(key) != value:
                    match = False
                    break
            if match:
                return doc
        return None

    def update_one(self, query: dict[str, Any], update: dict[str, Any]) -> None:
        target = self.find_one(query)
        if not target:
            return
        if "$set" in update:
            target.update(update["$set"])

    def list_all(self, query: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return list(self.find(query))


class FakeMongoService:
    def __init__(self) -> None:
        self.products = FakeCollection()
        self.jobs = FakeCollection()
        class _Client:
            class _Admin:
                @staticmethod
                def command(_cmd):
                    return {"ok": 1}

            admin = _Admin()

            @staticmethod
            def close():
                return None

        self.client = _Client()

    def list_products(self, query: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return self.products.list_all(query)

    def update_embedding(self, product_id: str, embedding: list[float]) -> None:
        self.products.update_one({"_id": product_id}, {"$set": {"embedding": embedding}})

    def create_job(self, job: Any):  # pragma: no cover - scraping not under test
        return self.jobs.insert_one(job.model_dump(by_alias=True, exclude_none=True)).inserted_id

    def update_job(self, job_id: str, updates: dict[str, Any]) -> None:  # pragma: no cover
        self.jobs.update_one({"_id": job_id}, {"$set": updates})

    def get_job(self, job_id: str) -> dict[str, Any] | None:  # pragma: no cover
        return self.jobs.find_one({"_id": job_id})


@pytest.fixture()
def mongo_service() -> FakeMongoService:
    return FakeMongoService()


@pytest.fixture()
def test_client(tmp_path, mongo_service, monkeypatch):
    index = FaissIndex(
        index_path=tmp_path / "faiss.index", metadata_path=tmp_path / "faiss_meta.json"
    )

    def fake_get_mongo():
        return mongo_service

    def fake_get_index():
        return index

    api.app.dependency_overrides[api.get_mongo] = fake_get_mongo
    api.app.dependency_overrides[api.get_index] = fake_get_index

    def fake_embed_sync(texts: list[str]):
        base_vectors = {
            "Grace Baked Beans": [0.9, 0.1],
            "Lasco Milk Powder": [0.1, 0.9],
        }
        return [base_vectors.get(text.split(" |")[0], [0.5, 0.5]) for text in texts]

    monkeypatch.setattr(api, "embed_sync", fake_embed_sync)
    monkeypatch.setattr(
        "agent.embeddings.ollama_client.embed_sync", lambda texts: fake_embed_sync(list(texts))
    )
    monkeypatch.setattr(
        "agent.service.ranking.text_search", lambda _collection, _query, limit=10: {}
    )
    monkeypatch.setattr(api, "MongoService", FakeMongoService)

    class DummyResponse:
        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {"version": "test"}

    class DummyAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, _path: str):
            return DummyResponse()

    monkeypatch.setattr(api.httpx, "AsyncClient", DummyAsyncClient)

    client = TestClient(api.app)
    yield client

    api.app.dependency_overrides.clear()


def insert_product(
    service: FakeMongoService, name: str, checksum: str, vector: list[float] | None = None
) -> None:
    product = Product(
        store_id="demo",
        store_name="Demo Grocer",
        name=name,
        normalized_name=name.lower(),
        brand=name.split()[0],
        size=SizeInfo(value=400.0, unit="g"),
        category="Canned Goods",
        tags=["canned goods"],
        url=f"https://example.com/{checksum}",
        image_url=None,
        checksum=checksum,
        location_prices=[LocationPrice(location_id="default", amount=345.0).model_dump()],
    )
    payload: dict[str, Any] = product.model_dump(by_alias=True)
    if vector:
        payload["embedding"] = vector
    service.products.insert_one(payload)


def test_reindex_and_search(test_client, mongo_service):
    insert_product(mongo_service, "Grace Baked Beans", "checksum-a", vector=[0.9, 0.1])
    insert_product(mongo_service, "Lasco Milk Powder", "checksum-b", vector=[0.1, 0.9])

    response = test_client.post("/index/rebuild")
    assert response.status_code == 200
    assert response.json()["indexed"] == 2

    response = test_client.post("/search", json={"query": "Grace Baked Beans", "k": 2})
    assert response.status_code == 200
    data = response.json()
    assert data["items"][0]["name"] == "Grace Baked Beans"
    assert data["items"][0]["location_prices"][0]["amount"] == 345.0
