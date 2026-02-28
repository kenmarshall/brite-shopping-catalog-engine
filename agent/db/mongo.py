from __future__ import annotations

from typing import Any

try:
    import certifi
except ImportError:  # pragma: no cover
    certifi = None  # type: ignore[assignment]
from bson import ObjectId
from pymongo import ASCENDING, DESCENDING, MongoClient

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
        self.curator_actions = self.db["curator_actions"]
        self.curator_snapshots = self.db["curator_snapshots"]
        self.curator_dismissed = self.db["curator_dismissed"]
        self.store_settings = self.db["store_settings"]
        self.barcode_mappings = self.db["barcode_mappings"]
        self._ensure_indexes()

    def _ensure_indexes(self) -> None:
        def _idx(collection, keys, **kwargs):  # type: ignore[no-untyped-def]
            try:
                collection.create_index(keys, **kwargs)
            except Exception as exc:  # pragma: no cover
                LOGGER.debug("Index skipped: %s", exc)

        # Upgrade products text index to cover name + brand + category.
        # MongoDB only allows one text index per collection — drop old ones first.
        try:
            for idx in list(self.products.list_indexes()):
                if "_fts" in dict(idx.get("key", {})):
                    self.products.drop_index(idx["name"])
        except Exception as exc:  # pragma: no cover
            LOGGER.debug("Text index drop skipped: %s", exc)
        _idx(
            self.products,
            [("name", "text"), ("brand", "text"), ("category", "text")],
            name="products_search",
            weights={"name": 10, "brand": 5, "category": 3},
        )

        _idx(self.products, [("updated_at", DESCENDING)])
        _idx(self.products, [("tags", ASCENDING)])
        _idx(self.products, [("store_id", ASCENDING)])
        _idx(self.products, [("match_key", ASCENDING)])
        _idx(self.products, [("location_prices.location_id", ASCENDING)])
        # Curator aggregations group/filter by normalized_name heavily
        _idx(self.products, [("normalized_name", ASCENDING)])
        # Manual merge flag queries filter on this field
        _idx(self.products, [("curator.manual_merge_flag", ASCENDING)])
        # Price anomaly pipeline filters on estimated_price > 0
        _idx(self.products, [("estimated_price", ASCENDING)])
        _idx(self.stores, [("store_id", ASCENDING)], unique=True)
        _idx(self.jobs, [("status", ASCENDING)])
        _idx(self.jobs, [("started_at", DESCENDING)])
        _idx(self.curator_actions, [("status", ASCENDING)])
        _idx(self.curator_actions, [("created_at", DESCENDING)])
        _idx(self.curator_actions, [("normalized_name", ASCENDING)])
        _idx(self.curator_snapshots, [("snapshot_key", ASCENDING)], unique=True)
        _idx(self.curator_snapshots, [("updated_at", ASCENDING)])
        _idx(
            self.curator_dismissed,
            [("normalized_name", ASCENDING), ("section", ASCENDING)],
            unique=True,
        )
        _idx(self.store_settings, [("store_id", ASCENDING)], unique=True)
        _idx(self.barcode_mappings, [("barcode", ASCENDING)], unique=True)
        _idx(self.barcode_mappings, [("product_id", ASCENDING)])
        # Dashboard recent barcodes sorts by created_at DESC
        _idx(self.barcode_mappings, [("created_at", DESCENDING)])

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
                lp_dict: dict[str, Any] = (
                    lp.model_dump()
                    if isinstance(lp, models.LocationPrice)
                    else dict(lp)
                )
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
                update["size"] = (
                    product.size.model_dump()
                    if hasattr(product.size, "model_dump")
                    else dict(product.size)
                )
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

    # ---- Barcode mappings ----

    def upsert_barcode(
        self,
        barcode: str,
        product_id: str,
        source: str,
        product_name: str | None = None,
    ) -> bool:
        """Insert or update a barcode→product mapping. Returns True if new."""
        from datetime import datetime, timezone

        result = self.barcode_mappings.update_one(
            {"barcode": barcode},
            {
                "$set": {
                    "barcode": barcode,
                    "product_id": product_id,
                    "source": source,
                    "product_name": product_name,
                    "created_at": datetime.now(timezone.utc),
                }
            },
            upsert=True,
        )
        return result.upserted_id is not None

    def lookup_barcode(self, barcode: str) -> dict[str, Any] | None:
        return self.barcode_mappings.find_one({"barcode": barcode})


__all__ = ["MongoService"]
