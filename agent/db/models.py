from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class LocationPrice(BaseModel):
    location_id: str
    store_name: str | None = None
    amount: float
    currency: str = "JMD"
    last_seen_at: datetime = Field(default_factory=datetime.utcnow)


class SizeInfo(BaseModel):
    value: float | None = None
    unit: str | None = None


class Product(BaseModel):
    id: str | None = Field(default=None, alias="_id")
    store_id: str
    store_name: str
    location_prices: list[LocationPrice] = Field(default_factory=list)
    estimated_price: float | None = None
    name: str
    normalized_name: str
    brand: str | None = None
    size: SizeInfo = Field(default_factory=SizeInfo)
    category: str | None = None
    tags: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    url: str
    image_url: str | None = None
    embedding: list[float] | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    checksum: str
    match_key: str | None = None

    class Config:
        populate_by_name = True
        arbitrary_types_allowed = True


class StoreLocation(BaseModel):
    location_id: str
    name: str
    address: str | None = None
    parish: str | None = None


class Store(BaseModel):
    id: str | None = Field(default=None, alias="_id")
    store_id: str
    store_name: str
    base_url: str
    locations: list[StoreLocation] = Field(default_factory=list)

    class Config:
        populate_by_name = True


class ScrapeJobStats(BaseModel):
    seen: int = 0
    saved: int = 0
    updated: int = 0


class ScrapeJobError(BaseModel):
    url: str
    reason: str


class ScrapeJob(BaseModel):
    id: str | None = Field(default=None, alias="_id")
    store_id: str | None = None
    seed_url: str | None = None
    status: Literal["queued", "running", "done", "error"] = "queued"
    stats: ScrapeJobStats = Field(default_factory=ScrapeJobStats)
    errors: list[ScrapeJobError] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=datetime.utcnow)
    finished_at: datetime | None = None

    class Config:
        populate_by_name = True


__all__ = [
    "LocationPrice",
    "SizeInfo",
    "Product",
    "Store",
    "StoreLocation",
    "ScrapeJob",
    "ScrapeJobStats",
    "ScrapeJobError",
]
