from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

from agent.config.settings import get_settings
from agent.db import models
from agent.db.mongo import MongoService
from agent.scraping import parsers
from agent.scraping.playwright_client import fetch_html
from agent.utils.logging import get_logger

LOGGER = get_logger(__name__)


def load_store_configs(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or []


def get_store_config(store_id: str) -> dict[str, Any]:
    settings = get_settings()
    configs = load_store_configs(settings.stores_config_path)
    for config in configs:
        if config["store_id"] == store_id:
            return config
    raise ValueError(f"Store config not found for {store_id}")


async def fetch_category(url: str, use_playwright: bool = True) -> str:
    if use_playwright:
        return await fetch_html(url)
    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.text


async def scrape_store(store_id: str, *, use_playwright: bool = True) -> dict[str, Any] | None:
    config = get_store_config(store_id)
    job = models.ScrapeJob(store_id=store_id, status="running")
    mongo = MongoService()
    job_id = mongo.create_job(job)
    stats = job.stats

    try:
        selectors = config.get("selectors", {})
        store_name = config.get("store_name", store_id)
        start_paths = config.get("navigation", {}).get("start_paths", [])
        for url in start_paths:
            LOGGER.info("Scraping %s", url)
            html = await fetch_category(url, use_playwright=use_playwright)
            raw_products = parsers.parse_products(html, selectors, config.get("base_url", url))
            stats.seen += len(raw_products)
            for raw in raw_products:
                product = parsers.raw_to_product(raw, store_id=store_id, store_name=store_name)
                product.updated_at = datetime.utcnow()
                product.created_at = datetime.utcnow()
                _, created = mongo.upsert_product(product)
                if created:
                    stats.saved += 1
                else:
                    stats.updated += 1
        mongo.update_job(
            job_id,
            {"status": "done", "stats": stats.model_dump(), "finished_at": datetime.utcnow()},
        )
    except Exception as exc:  # pragma: no cover
        LOGGER.exception("Scrape failed")
        mongo.update_job(job_id, {"status": "error", "errors": [{"url": "*", "reason": str(exc)}]})
    job_doc = mongo.get_job(str(job_id))
    if job_doc and "_id" in job_doc:
        job_doc["_id"] = str(job_doc["_id"])
    return job_doc


async def scrape_url(
    url: str, store_id: str | None = None, *, use_playwright: bool = True
) -> dict[str, Any] | None:
    config = get_store_config(store_id) if store_id else None
    selectors = config.get("selectors", {}) if config else {"product": ".product"}
    store_name = config.get("store_name", store_id or "external") if config else "external"
    base_url = config.get("base_url", url) if config else url
    mongo = MongoService()
    job = models.ScrapeJob(store_id=store_id, seed_url=url, status="running")
    job_id = mongo.create_job(job)
    stats = job.stats
    try:
        html = await fetch_category(url, use_playwright=use_playwright)
        raw_products = parsers.parse_products(html, selectors, base_url)
        stats.seen = len(raw_products)
        for raw in raw_products:
            product = parsers.raw_to_product(
                raw, store_id=store_id or "external", store_name=store_name
            )
            _, created = mongo.upsert_product(product)
            if created:
                stats.saved += 1
            else:
                stats.updated += 1
        mongo.update_job(
            job_id,
            {"status": "done", "stats": stats.model_dump(), "finished_at": datetime.utcnow()},
        )
    except Exception as exc:  # pragma: no cover
        LOGGER.exception("Scrape failed")
        mongo.update_job(job_id, {"status": "error", "errors": [{"url": url, "reason": str(exc)}]})
    job_doc = mongo.get_job(str(job_id))
    if job_doc and "_id" in job_doc:
        job_doc["_id"] = str(job_doc["_id"])
    return job_doc


def run_scrape(
    store_id: str | None = None, url: str | None = None, *, use_playwright: bool = True
) -> dict[str, Any] | None:
    if not store_id and not url:
        raise ValueError("store_id or url required")
    async def runner() -> dict[str, Any] | None:
        if store_id:
            return await scrape_store(store_id, use_playwright=use_playwright)
        return await scrape_url(url=url or "", store_id=store_id, use_playwright=use_playwright)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            return loop.run_until_complete(runner())
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    if loop.is_running():
        raise RuntimeError(
            "run_scrape cannot be called from within a running event loop; "
            "use `await scrape_store(...)` or `await scrape_url(...)` instead."
        )
    return loop.run_until_complete(runner())


__all__ = [
    "load_store_configs",
    "get_store_config",
    "scrape_store",
    "scrape_url",
    "run_scrape",
]
