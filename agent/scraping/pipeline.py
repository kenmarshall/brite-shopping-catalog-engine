from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import yaml
from bs4 import BeautifulSoup
from urllib.parse import urljoin

from agent.config.settings import get_settings
from agent.db import models
from agent.db.mongo import MongoService
from agent.embeddings.enricher import enrich_product
from agent.scraping import parsers
from agent.scraping.playwright_client import fetch_html
from agent.utils.logging import get_logger

LOGGER = get_logger(__name__)


def load_source_configs(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or []


def get_source_config(source_id: str) -> dict[str, Any]:
    settings = get_settings()
    configs = load_source_configs(settings.sources_config_path)
    for config in configs:
        if config.get("source_id") == source_id:
            return config
    raise ValueError(f"Source config not found for {source_id}")


async def fetch_category(url: str, use_playwright: bool = True) -> str:
    if use_playwright:
        return await fetch_html(url)
    async with httpx.AsyncClient(follow_redirects=True, timeout=120.0) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.text


def _next_page_url(
    html: str,
    navigation: dict[str, Any],
    current_url: str,
    base_url: str,
    page_count: int = 1,
) -> str | None:
    pagination = navigation.get("pagination", {})

    # Strategy 1: CSS selector for a "next" link (WooCommerce, etc.)
    selector = pagination.get("next_selector")
    if selector:
        soup = BeautifulSoup(html, "html.parser")
        next_el = soup.select_one(selector)
        if not next_el:
            return None
        href = next_el.get("href")
        if not href:
            return None
        resolved = urljoin(base_url, href)
        if resolved == current_url:
            return None
        return resolved

    # Strategy 2: Query-param pagination (Magento ?p=2, etc.)
    page_param = pagination.get("page_param")
    if page_param:
        from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

        parsed = urlparse(current_url)
        params = parse_qs(parsed.query)
        current_page_values = params.get(page_param, ["1"])
        current_page = int(current_page_values[0])
        next_page = current_page + 1
        params[page_param] = [str(next_page)]
        new_query = urlencode(params, doseq=True)
        return urlunparse(parsed._replace(query=new_query))

    return None


async def scrape_store(store_id: str, *, use_playwright: bool = True) -> dict[str, Any] | None:
    config = get_source_config(store_id)

    # Dispatch to LocCloud scraper for stores using that platform
    auth_strategy = config.get("auth", {}).get("strategy", "none")
    if auth_strategy == "loccloud":
        return await _scrape_loccloud(store_id, config)

    job = models.ScrapeJob(store_id=store_id, status="running")
    mongo = MongoService()
    job_id = mongo.create_job(job)
    stats = job.stats

    try:
        selectors = config.get("selectors", {})
        catalog_id, catalog_name = resolve_catalog_identity(config, store_id)
        defaults = config.get("defaults", {})
        default_brand = defaults.get("brand")
        if not default_brand and config.get("entity_type") == "brand":
            brand_cfg = config.get("brand", {})
            default_brand = brand_cfg.get("name")
        currency = defaults.get("currency", "JMD")
        start_paths = config.get("navigation", {}).get("start_paths", [])
        navigation = config.get("navigation", {})
        base_url = config.get("base_url", "")
        pagination = navigation.get("pagination", {})
        max_pages = pagination.get("max_pages")
        for url in start_paths:
            next_url = url
            visited: set[str] = set()
            page_count = 0
            while next_url and next_url not in visited:
                visited.add(next_url)
                page_count += 1
                LOGGER.info("Scraping %s", next_url)
                html = await fetch_category(next_url, use_playwright=use_playwright)
                raw_products = parsers.parse_products(html, selectors, base_url or next_url, currency=currency)
                if not raw_products:
                    LOGGER.info("No products found on %s — stopping pagination", next_url)
                    break
                stats.seen += len(raw_products)
                for raw in raw_products:
                    product = parsers.raw_to_product(
                        raw,
                        store_id=catalog_id,
                        store_name=catalog_name,
                        default_brand=default_brand,
                    )
                    product.updated_at = datetime.utcnow()
                    product.created_at = datetime.utcnow()
                    product = await enrich_product(product)
                    _, created = mongo.upsert_product(product)
                    if created:
                        stats.saved += 1
                    else:
                        stats.updated += 1
                if max_pages and page_count >= max_pages:
                    break
                next_url = _next_page_url(
                    html,
                    navigation,
                    next_url,
                    base_url or next_url,
                    page_count=page_count,
                )
                if next_url and next_url in visited:
                    break
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
    config = get_source_config(store_id) if store_id else None
    selectors = config.get("selectors", {}) if config else {"product": ".product"}
    catalog_id, catalog_name = (
        resolve_catalog_identity(config, store_id or "external") if config else (store_id or "external", "external")
    )
    base_url = config.get("base_url", url) if config else url
    defaults = config.get("defaults", {}) if config else {}
    default_brand = defaults.get("brand") if defaults else None
    currency = defaults.get("currency", "JMD") if defaults else "JMD"
    if not default_brand and config and config.get("entity_type") == "brand":
        brand_cfg = config.get("brand", {})
        default_brand = brand_cfg.get("name")
    mongo = MongoService()
    job = models.ScrapeJob(store_id=store_id, seed_url=url, status="running")
    job_id = mongo.create_job(job)
    stats = job.stats
    try:
        html = await fetch_category(url, use_playwright=use_playwright)
        raw_products = parsers.parse_products(html, selectors, base_url, currency=currency)
        stats.seen = len(raw_products)
        for raw in raw_products:
            product = parsers.raw_to_product(
                raw,
                store_id=catalog_id,
                store_name=catalog_name,
                default_brand=default_brand,
            )
            product = await enrich_product(product)
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


async def _scrape_loccloud(store_id: str, config: dict[str, Any]) -> dict[str, Any] | None:
    """Scrape a LocCloud/ExtJS e-store (e.g., Hi-Lo Food Stores)."""
    from agent.scraping.loccloud import (
        fetch_catalog_xml,
        login_and_get_session,
        parse_catalog_xml,
    )

    job = models.ScrapeJob(store_id=store_id, status="running")
    mongo = MongoService()
    job_id = mongo.create_job(job)
    stats = job.stats
    catalog_id, catalog_name = resolve_catalog_identity(config, store_id)
    defaults = config.get("defaults", {})
    default_brand = defaults.get("brand")
    currency = defaults.get("currency", "JMD")

    browser = None
    try:
        headless = config.get("auth", {}).get("headless", True)
        browser, context, session = await login_and_get_session(headless=headless)

        xml = await fetch_catalog_xml(session)
        raw_products = parse_catalog_xml(xml)
        LOGGER.info("Parsed %d products from LocCloud catalog", len(raw_products))

        stats.seen = len(raw_products)
        for raw in raw_products:
            product = parsers.raw_to_product(
                raw,
                store_id=catalog_id,
                store_name=catalog_name,
                default_brand=default_brand,
            )
            product.updated_at = datetime.utcnow()
            product.created_at = datetime.utcnow()
            product = await enrich_product(product)
            _, created = mongo.upsert_product(product)
            if created:
                stats.saved += 1
            else:
                stats.updated += 1

        mongo.update_job(
            job_id,
            {"status": "done", "stats": stats.model_dump(), "finished_at": datetime.utcnow()},
        )
    except Exception as exc:
        LOGGER.exception("LocCloud scrape failed")
        mongo.update_job(job_id, {"status": "error", "errors": [{"url": "*", "reason": str(exc)}]})
    finally:
        if browser:
            await browser.close()

    result = mongo.get_job(str(job_id))
    LOGGER.info("LocCloud scrape done: %s", result)
    return result


__all__ = [
    "load_source_configs",
    "get_source_config",
    "scrape_store",
    "scrape_url",
    "run_scrape",
]
def resolve_catalog_identity(config: dict[str, Any], fallback_id: str) -> tuple[str, str]:
    entity_type = (config.get("entity_type") or "store").lower()
    source_name = config.get("catalog_name")

    if entity_type == "brand":
        brand_cfg = config.get("brand", {})
        catalog_id = (
            brand_cfg.get("id")
            or brand_cfg.get("slug")
            or (brand_cfg.get("name") or "").lower().replace(" ", "-")
            or fallback_id
        )
        catalog_name = brand_cfg.get("name") or source_name or catalog_id
    else:
        store_cfg = config.get("store", {})
        catalog_id = store_cfg.get("id") or fallback_id
        catalog_name = store_cfg.get("name") or source_name or catalog_id

    return catalog_id, catalog_name
