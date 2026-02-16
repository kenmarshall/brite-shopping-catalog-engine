from __future__ import annotations

import math
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import yaml
from bson import ObjectId
from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from agent.db.models import LocationPrice, Product
from agent.db.mongo import MongoService
from agent.scraping.normalizer import (
    build_checksum,
    build_match_key,
    normalize_brand,
    normalize_category,
    normalize_name,
    parse_size,
)
from agent.scraping.pipeline import run_scrape
from agent.utils.logging import get_logger

LOGGER = get_logger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

DEFAULT_SELECTORS = {
    "product": ".products .product",
    "name": ".woocommerce-loop-product__title",
    "price": ".woocommerce-Price-amount",
    "image": "img::attr(src)",
    "size_hint": ".woocommerce-loop-product__title",
    "brand_hint": None,
    "category_hint": None,
    "location_hint": None,
}


def _get_mongo() -> MongoService:
    return MongoService()


def _sources_path() -> Path:
    from agent.config.settings import get_settings

    return get_settings().sources_config_path


def _load_sources() -> list[dict[str, Any]]:
    with _sources_path().open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or []


def _source_display_name(source: dict[str, Any]) -> str:
    return (
        source.get("catalog_name")
        or (source.get("store") or {}).get("name")
        or (source.get("brand") or {}).get("name")
        or source.get("source_id")
        or "unknown"
    )


def _append_source(source: dict[str, Any]) -> None:
    path = _sources_path()
    existing_text = path.read_text(encoding="utf-8")
    snippet = yaml.safe_dump([source], sort_keys=False, allow_unicode=False).strip()
    with path.open("a", encoding="utf-8") as fh:
        if existing_text and not existing_text.endswith("\n"):
            fh.write("\n")
        fh.write("\n")
        fh.write(snippet)
        fh.write("\n")


def _decorate_sources(
    sources: list[dict[str, Any]],
    counts: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    count_map = counts or {}
    for source in sources:
        source_id = source.get("source_id", "")
        rows.append(
            {
                **source,
                "source_id": source_id,
                "display_name": _source_display_name(source),
                "strategy": (source.get("auth") or {}).get("strategy", "none"),
                "product_count": count_map.get(source_id, 0),
            }
        )
    return sorted(rows, key=lambda item: item.get("display_name", ""))


def _object_id_or_none(value: str) -> ObjectId | None:
    if not ObjectId.is_valid(value):
        return None
    return ObjectId(value)


# ---- Dashboard ----


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    mongo = _get_mongo()
    pipeline = [
        {
            "$group": {
                "_id": "$store_id",
                "store_name": {"$first": "$store_name"},
                "count": {"$sum": 1},
            }
        },
        {"$sort": {"count": -1}},
    ]
    store_stats = list(mongo.products.aggregate(pipeline))
    total_products = sum(s["count"] for s in store_stats)
    recent_jobs = list(mongo.jobs.find().sort("started_at", -1).limit(10))
    for job in recent_jobs:
        job["_id"] = str(job["_id"])
    running_count = mongo.jobs.count_documents({"status": {"$in": ["running", "stopping"]}})

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "store_stats": store_stats,
            "total_products": total_products,
            "recent_jobs": recent_jobs,
            "running_count": running_count,
            "page_title": "Dashboard",
            "active_nav": "dashboard",
        },
    )


# ---- Products ----


@router.get("/products", response_class=HTMLResponse)
async def product_list(
    request: Request,
    q: str = Query("", alias="q"),
    store: str = Query("", alias="store"),
    page: int = Query(1),
    limit: int = Query(50),
):
    mongo = _get_mongo()
    skip = (page - 1) * limit
    query: dict[str, Any] = {}
    conditions = []
    if q:
        conditions.append(
            {
                "$or": [
                    {"name": {"$regex": q, "$options": "i"}},
                    {"brand": {"$regex": q, "$options": "i"}},
                    {"category": {"$regex": q, "$options": "i"}},
                ]
            }
        )
    if store:
        conditions.append({"store_id": store})
    if conditions:
        query = {"$and": conditions} if len(conditions) > 1 else conditions[0]

    products = list(
        mongo.products.find(query, {"embedding": 0}).sort("updated_at", -1).skip(skip).limit(limit)
    )
    total = mongo.products.count_documents(query)
    total_pages = math.ceil(total / limit) if total else 1
    for product in products:
        product["_id"] = str(product["_id"])

    store_pipeline = [
        {"$group": {"_id": "$store_id", "store_name": {"$first": "$store_name"}}},
        {"$sort": {"store_name": 1}},
    ]
    stores = list(mongo.products.aggregate(store_pipeline))

    if (
        request.headers.get("HX-Request")
        and request.headers.get("HX-Target") == "product-table-body"
    ):
        return templates.TemplateResponse(
            "products/_rows.html",
            {
                "request": request,
                "products": products,
            },
        )

    return templates.TemplateResponse(
        "products/list.html",
        {
            "request": request,
            "products": products,
            "query": q,
            "active_store": store,
            "stores": stores,
            "page": page,
            "limit": limit,
            "total": total,
            "total_pages": total_pages,
            "page_title": "Products",
            "active_nav": "products",
        },
    )


@router.get("/products/new", response_class=HTMLResponse)
async def product_new(
    request: Request,
    error: str = Query("", alias="error"),
):
    sources = _decorate_sources(_load_sources())
    return templates.TemplateResponse(
        "products/new.html",
        {
            "request": request,
            "sources": sources,
            "error": error,
            "page_title": "Add Product",
            "active_nav": "products",
        },
    )


@router.post("/products/new", response_class=HTMLResponse)
async def product_create(request: Request):
    form = await request.form()
    store_id = str(form.get("store_id", "")).strip().lower()
    name = str(form.get("name", "")).strip()

    if not store_id or not name:
        msg = quote_plus("Store and product name are required")
        return RedirectResponse(f"/admin/products/new?error={msg}", status_code=303)

    sources = _decorate_sources(_load_sources())
    source_name_map = {source.get("source_id"): source.get("display_name") for source in sources}

    store_name = (
        str(form.get("store_name", "")).strip() or source_name_map.get(store_id) or store_id
    )
    brand_raw = str(form.get("brand", "")).strip() or None
    category_raw = str(form.get("category", "")).strip() or None
    image_url = str(form.get("image_url", "")).strip() or None
    product_url = str(form.get("url", "")).strip() or f"manual://{store_id}"
    size_hint = str(form.get("size_hint", "")).strip() or name
    currency = str(form.get("currency", "JMD")).strip() or "JMD"

    price_raw = str(form.get("price", "")).strip()
    price: float | None = None
    if price_raw:
        try:
            price = float(price_raw)
        except ValueError:
            msg = quote_plus("Price must be a number")
            return RedirectResponse(f"/admin/products/new?error={msg}", status_code=303)

    normalized_name = normalize_name(name)
    normalized_brand = normalize_brand(brand_raw)
    normalized_category = normalize_category(category_raw)
    size = parse_size(size_hint)
    match_key = build_match_key(normalized_name, normalized_brand, size)
    checksum = build_checksum(store_id, normalized_name, normalized_brand, size)

    location_prices: list[LocationPrice] = []
    estimated_price: float | None = None
    if price is not None:
        location_prices = [
            LocationPrice(
                location_id=store_id,
                store_name=store_name,
                amount=price,
                currency=currency,
            )
        ]
        estimated_price = price

    tags: list[str] = [normalized_category.lower()] if normalized_category else []

    product = Product(
        store_id=store_id,
        store_name=store_name,
        name=name,
        normalized_name=normalized_name,
        brand=normalized_brand,
        size=size,
        category=normalized_category,
        tags=tags,
        url=product_url,
        image_url=image_url,
        checksum=checksum,
        match_key=match_key,
        location_prices=location_prices,
        estimated_price=estimated_price,
    )

    mongo = _get_mongo()
    oid, _ = mongo.upsert_product(product)
    return RedirectResponse(f"/admin/products/{oid}", status_code=303)


@router.get("/products/{product_id}", response_class=HTMLResponse)
async def product_detail(request: Request, product_id: str):
    oid = _object_id_or_none(product_id)
    if oid is None:
        return HTMLResponse("Invalid product ID", status_code=400)
    mongo = _get_mongo()
    product = mongo.products.find_one({"_id": oid}, {"embedding": 0})
    if not product:
        return HTMLResponse("Product not found", status_code=404)
    product["_id"] = str(product["_id"])
    return templates.TemplateResponse(
        "products/detail.html",
        {
            "request": request,
            "product": product,
            "page_title": f"Edit: {product.get('name', '')[:40]}",
            "active_nav": "products",
        },
    )


@router.post("/products/{product_id}", response_class=HTMLResponse)
async def product_update(request: Request, product_id: str):
    oid = _object_id_or_none(product_id)
    if oid is None:
        return HTMLResponse("Invalid product ID", status_code=400)
    mongo = _get_mongo()
    existing = mongo.products.find_one({"_id": oid})
    if not existing:
        return HTMLResponse("Product not found", status_code=404)

    form = await request.form()

    name = str(form.get("name", "")).strip() or existing.get("name") or ""
    brand_input = str(form.get("brand", "")).strip() or None
    category_input = str(form.get("category", "")).strip() or None
    size_hint = str(form.get("size_hint", "")).strip() or name

    normalized_name = normalize_name(name)
    normalized_brand = normalize_brand(brand_input)
    normalized_category = normalize_category(category_input)
    size = parse_size(size_hint)

    tags_raw = str(form.get("tags", "")).strip()
    tags = [tag.strip().lower() for tag in tags_raw.split(",") if tag.strip()] if tags_raw else []
    if not tags and normalized_category:
        tags = [normalized_category.lower()]

    price_raw = str(form.get("estimated_price", "")).strip()
    estimated_price: float | None = existing.get("estimated_price")
    if price_raw:
        try:
            estimated_price = float(price_raw)
        except ValueError:
            pass
    elif price_raw == "":
        estimated_price = None

    updates: dict[str, Any] = {
        "name": name,
        "normalized_name": normalized_name,
        "brand": normalized_brand,
        "size": size.model_dump(),
        "category": normalized_category,
        "tags": tags,
        "estimated_price": estimated_price,
        "url": str(form.get("url", "")).strip() or existing.get("url"),
        "image_url": str(form.get("image_url", "")).strip() or None,
        "match_key": build_match_key(normalized_name, normalized_brand, size),
        "checksum": build_checksum(
            existing.get("store_id", "unknown"),
            normalized_name,
            normalized_brand,
            size,
        ),
        "updated_at": datetime.utcnow(),
    }

    mongo.products.update_one({"_id": oid}, {"$set": updates})
    return RedirectResponse(f"/admin/products/{product_id}", status_code=303)


@router.delete("/products/{product_id}", response_class=HTMLResponse)
async def product_delete(product_id: str):
    oid = _object_id_or_none(product_id)
    if oid is None:
        return HTMLResponse("", status_code=400)
    mongo = _get_mongo()
    mongo.products.delete_one({"_id": oid})
    return HTMLResponse("")


# ---- Stores ----


@router.get("/stores", response_class=HTMLResponse)
async def store_list(
    request: Request,
    message: str = Query("", alias="message"),
    error: str = Query("", alias="error"),
):
    mongo = _get_mongo()
    sources = _load_sources()
    pipeline = [{"$group": {"_id": "$store_id", "count": {"$sum": 1}}}]
    counts = {row["_id"]: row["count"] for row in mongo.products.aggregate(pipeline)}
    decorated_sources = _decorate_sources(sources, counts)

    return templates.TemplateResponse(
        "stores/list.html",
        {
            "request": request,
            "sources": decorated_sources,
            "message": message,
            "error": error,
            "page_title": "Stores",
            "active_nav": "stores",
        },
    )


@router.post("/stores", response_class=HTMLResponse)
async def store_create(
    source_id: str = Form(...),
    store_name: str = Form(...),
    base_url: str = Form(...),
    strategy: str = Form("none"),
    start_path: str = Form(""),
    currency: str = Form("JMD"),
):
    normalized_source_id = source_id.strip().lower()
    clean_store_name = store_name.strip()
    clean_base_url = base_url.strip().rstrip("/")
    clean_start_path = start_path.strip()
    clean_currency = currency.strip().upper() or "JMD"

    if not normalized_source_id or not clean_store_name or not clean_base_url:
        msg = quote_plus("Source ID, store name, and base URL are required")
        return RedirectResponse(f"/admin/stores?error={msg}", status_code=303)

    sources = _load_sources()
    if any(source.get("source_id") == normalized_source_id for source in sources):
        msg = quote_plus(f"Store source '{normalized_source_id}' already exists")
        return RedirectResponse(f"/admin/stores?error={msg}", status_code=303)

    selected_strategy = strategy.strip().lower() or "none"
    if selected_strategy not in {"none", "playwright", "loccloud", "manual"}:
        selected_strategy = "none"

    auth_strategy = "none" if selected_strategy == "manual" else selected_strategy
    start_paths: list[str] = []
    selectors: dict[str, Any] = {}
    pagination: dict[str, Any] = {}

    if selected_strategy in {"none", "playwright"}:
        selectors = dict(DEFAULT_SELECTORS)
        pagination = {"next_selector": "a.next.page-numbers", "max_pages": None}
        if clean_start_path:
            start_paths = [clean_start_path]
    elif selected_strategy == "loccloud":
        pagination = {}
        start_paths = [clean_start_path] if clean_start_path else [clean_base_url]

    new_source: dict[str, Any] = {
        "source_id": normalized_source_id,
        "entity_type": "store",
        "store": {"name": clean_store_name},
        "catalog_name": clean_store_name,
        "base_url": clean_base_url,
        "defaults": {"brand": None, "currency": clean_currency},
        "auth": {
            "strategy": auth_strategy,
            "storage_state_path": f"./data/sessions/{normalized_source_id}.json",
        },
        "navigation": {"start_paths": start_paths, "pagination": pagination},
        "selectors": selectors,
        "rate_limits": {"rpm": 15, "concurrency": 1},
    }

    _append_source(new_source)
    msg = quote_plus(f"Added store source '{normalized_source_id}'")
    return RedirectResponse(f"/admin/stores?message={msg}", status_code=303)


# ---- Scrapes ----


@router.get("/scrapes", response_class=HTMLResponse)
async def scrape_list(
    request: Request,
    message: str = Query("", alias="message"),
    error: str = Query("", alias="error"),
):
    mongo = _get_mongo()
    jobs = list(mongo.jobs.find().sort("started_at", -1).limit(50))
    for job in jobs:
        job["_id"] = str(job["_id"])

    sources = _decorate_sources(_load_sources())

    return templates.TemplateResponse(
        "scrapes/list.html",
        {
            "request": request,
            "jobs": jobs,
            "sources": sources,
            "message": message,
            "error": error,
            "page_title": "Scrapes",
            "active_nav": "scrapes",
        },
    )


@router.post("/scrapes/start", response_class=HTMLResponse)
async def start_scrape(store_id: str = Form(...)):
    mongo = _get_mongo()
    existing = mongo.jobs.find_one(
        {
            "store_id": store_id,
            "status": {"$in": ["running", "stopping"]},
        }
    )
    if existing:
        msg = quote_plus(f"A scrape is already active for '{store_id}'")
        return RedirectResponse(f"/admin/scrapes?error={msg}", status_code=303)

    thread = threading.Thread(
        target=run_scrape,
        kwargs={"store_id": store_id, "use_playwright": True},
        daemon=True,
    )
    thread.start()
    LOGGER.info("Started scrape for %s in background thread", store_id)

    msg = quote_plus(f"Started scrape for '{store_id}'")
    return RedirectResponse(f"/admin/scrapes?message={msg}", status_code=303)


@router.post("/scrapes/{job_id}/stop", response_class=HTMLResponse)
async def stop_scrape(job_id: str):
    if not ObjectId.is_valid(job_id):
        msg = quote_plus("Invalid job ID")
        return RedirectResponse(f"/admin/scrapes?error={msg}", status_code=303)

    mongo = _get_mongo()
    oid = ObjectId(job_id)
    job = mongo.jobs.find_one({"_id": oid})
    if not job:
        msg = quote_plus("Scrape job not found")
        return RedirectResponse(f"/admin/scrapes?error={msg}", status_code=303)

    if job.get("status") not in {"running", "stopping"}:
        msg = quote_plus("Only running scrapes can be stopped")
        return RedirectResponse(f"/admin/scrapes?error={msg}", status_code=303)

    mongo.update_job(oid, {"status": "stopping"})
    LOGGER.info("Stop requested for scrape job %s", job_id)

    msg = quote_plus("Stop requested. The scraper will stop safely on its next checkpoint")
    return RedirectResponse(f"/admin/scrapes?message={msg}", status_code=303)


@router.get("/scrapes/status", response_class=HTMLResponse)
async def scrape_status(request: Request):
    mongo = _get_mongo()
    jobs = list(mongo.jobs.find().sort("started_at", -1).limit(50))
    for job in jobs:
        job["_id"] = str(job["_id"])
    return templates.TemplateResponse(
        "scrapes/_status.html",
        {
            "request": request,
            "jobs": jobs,
        },
    )


# ---- Curator ----


@router.get("/curator", response_class=HTMLResponse)
async def curator_page(request: Request):
    return templates.TemplateResponse(
        "curator/list.html",
        {
            "request": request,
            "discrepancies": [],
            "page_title": "AI Curator",
            "active_nav": "curator",
        },
    )
