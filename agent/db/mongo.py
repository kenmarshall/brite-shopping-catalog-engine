from __future__ import annotations

from typing import Any

try:
    import certifi
except ImportError:  # pragma: no cover
    certifi = None  # type: ignore[assignment]
from bson import ObjectId
from pymongo import ASCENDING, MongoClient

from agent.config.settings import get_settings
from agent.db import models
from agent.utils.logging import get_logger

LOGGER = get_logger(__name__)


class MongoService:
    def __init__(self, client: MongoClient | None = None) -> None:
        settings = get_settings().mongo
        client_kwargs: dict[str, Any] = {"serverSelectionTimeoutMS": 5000}
        if certifi is not None:
            try:
                client_kwargs["tlsCAFile"] = certifi.where()
            except Exception as exc:  # pragma: no cover
                LOGGER.debug("Unable to locate CA bundle via certifi: %s", exc)
        else:  # pragma: no cover - certifi should be installed with httpx
            LOGGER.debug("certifi not available; using default TLS configuration")
        self.client = client or MongoClient(settings.uri, **client_kwargs)
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
            self.products.create_index([("match_key", ASCENDING)])
            self.products.create_index([("location_prices.location_id", ASCENDING)])
            self.stores.create_index([("store_id", ASCENDING)], unique=True)
            self.jobs.create_index([("status", ASCENDING)])
            self.jobs.create_index([("started_at", ASCENDING)])
        except Exception as exc:  # pragma: no cover
            LOGGER.debug("Index creation skipped: %s", exc)

    def upsert_product(self, product: models.Product) -> tuple[ObjectId, bool]:
        new_location_prices = product.location_prices

        # Check for a cross-store match first (same product from a different store)
        if product.match_key:
            existing = self.products.find_one({"match_key": product.match_key})
        else:
            existing = None

        if existing:
            # Merge: add new location prices that aren't already present
            current_prices: list[dict[str, Any]] = existing.get("location_prices", [])
            existing_location_ids = {lp.get("location_id") for lp in current_prices}

            merged = False
            for lp in new_location_prices:
                lp_dict: dict[str, Any] = lp.model_dump() if isinstance(lp, models.LocationPrice) else dict(lp)
                if lp_dict.get("location_id") not in existing_location_ids:
                    current_prices.append(lp_dict)
                    merged = True
                else:
                    # Update existing location price
                    for i, existing_lp in enumerate(current_prices):
                        if existing_lp.get("location_id") == lp_dict.get("location_id"):
                            current_prices[i] = lp_dict
                            break

            # Recompute estimated price as average of all location prices
            amounts: list[float] = [
                float(lp["amount"]) for lp in current_prices
                if lp.get("amount") is not None
            ]
            estimated_price = round(sum(amounts) / len(amounts), 2) if amounts else None

            update: dict[str, Any] = {
                "location_prices": current_prices,
                "estimated_price": estimated_price,
                "updated_at": product.updated_at,
            }

            # Fill in missing fields from the new source
            if product.embedding and not existing.get("embedding"):
                update["embedding"] = product.embedding
            if product.tags and not existing.get("tags"):
                update["tags"] = product.tags
            if product.brand and not existing.get("brand"):
                update["brand"] = product.brand
            if product.category and not existing.get("category"):
                update["category"] = product.category
            if product.size and product.size.value and not existing.get("size", {}).get("value"):
                update["size"] = product.size.model_dump() if hasattr(product.size, "model_dump") else dict(product.size)
            # Use image from whichever source provides a real one
            existing_img = existing.get("image_url") or ""
            new_img = product.image_url or ""
            if new_img and (not existing_img or existing_img.startswith("data:")):
                update["image_url"] = product.image_url

            self.products.update_one({"_id": existing["_id"]}, {"$set": update})
            LOGGER.debug("Merged product %s (location: %s)", existing["_id"], product.store_id)
            return existing["_id"], not merged  # False if we added a new location

        # No cross-store match — insert as new product
        payload = product.model_dump(by_alias=True, exclude_none=True)
        payload["updated_at"] = product.updated_at
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
