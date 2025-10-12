from __future__ import annotations

from typing import Any

from bson import ObjectId
from pymongo import ASCENDING, MongoClient

from agent.config.settings import get_settings
from agent.db import models
from agent.utils.logging import get_logger

LOGGER = get_logger(__name__)


class MongoService:
    def __init__(self, client: MongoClient | None = None) -> None:
        settings = get_settings().mongo
        self.client = client or MongoClient(settings.uri)
        self.db = self.client[settings.db]
        self.products = self.db[settings.collection_products]
        self.stores = self.db[settings.collection_stores]
        self.jobs = self.db[settings.collection_jobs]
        self._ensure_indexes()

    def _ensure_indexes(self) -> None:
        try:
            self.products.create_index([("name", "text")], name="product_text")
            self.products.create_index([("tags", ASCENDING)])
            self.products.create_index([("store_id", ASCENDING)])
            self.products.create_index([("location_prices.location_id", ASCENDING)])
            self.stores.create_index([("store_id", ASCENDING)], unique=True)
            self.jobs.create_index([("status", ASCENDING)])
            self.jobs.create_index([("started_at", ASCENDING)])
        except Exception as exc:  # pragma: no cover
            LOGGER.debug("Index creation skipped: %s", exc)

    def upsert_product(self, product: models.Product) -> tuple[ObjectId, bool]:
        query = {
            "checksum": product.checksum,
            "store_id": product.store_id,
        }
        existing = self.products.find_one(query)
        payload = product.model_dump(by_alias=True, exclude_none=True)
        payload["updated_at"] = product.updated_at
        if existing:
            self.products.update_one({"_id": existing["_id"]}, {"$set": payload})
            LOGGER.debug("Updated product %s", existing["_id"])
            return existing["_id"], False
        result = self.products.insert_one(payload)
        LOGGER.debug("Inserted product %s", result.inserted_id)
        return result.inserted_id, True

    def list_products(self, query: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        cursor = self.products.find(query or {})
        return list(cursor)

    def update_embedding(self, product_id: ObjectId, embedding: list[float]) -> None:
        self.products.update_one({"_id": product_id}, {"$set": {"embedding": embedding}})

    def create_job(self, job: models.ScrapeJob) -> ObjectId:
        payload = job.model_dump(by_alias=True, exclude_none=True)
        result = self.jobs.insert_one(payload)
        return result.inserted_id

    def update_job(self, job_id: ObjectId, updates: dict[str, Any]) -> None:
        self.jobs.update_one({"_id": job_id}, {"$set": updates})

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        try:
            oid = ObjectId(job_id)
        except Exception:
            return None
        return self.jobs.find_one({"_id": oid})


__all__ = ["MongoService"]
