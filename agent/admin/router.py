from __future__ import annotations

import math
import threading
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import yaml
from bson import ObjectId
from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from agent.db.models import LocationPrice, Product, SizeInfo
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


def _non_empty(values: list[Any]) -> list[str]:
    cleaned: list[str] = []
    for value in values:
        text = str(value).strip() if value is not None else ""
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned


def _snapshot_docs(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [deepcopy(doc) for doc in docs]


def _record_curator_action(
    mongo: MongoService,
    *,
    action_type: str,
    normalized_name: str,
    summary: str,
    before_docs: list[dict[str, Any]],
    after_docs: list[dict[str, Any]],
) -> ObjectId:
    payload = {
        "action_type": action_type,
        "normalized_name": normalized_name,
        "summary": summary,
        "status": "applied",
        "before_count": len(before_docs),
        "after_count": len(after_docs),
        "before_docs": _snapshot_docs(before_docs),
        "after_docs": _snapshot_docs(after_docs),
        "created_at": datetime.utcnow(),
        "undone_at": None,
    }
    result = mongo.curator_actions.insert_one(payload)
    return result.inserted_id


def _list_recent_curator_actions(mongo: MongoService, limit: int = 20) -> list[dict[str, Any]]:
    actions = list(mongo.curator_actions.find().sort("created_at", -1).limit(limit))
    for action in actions:
        action["_id"] = str(action["_id"])
    return actions


def _collect_curator_conflicts(
    mongo: MongoService,
    *,
    top_k: int = 25,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    pipeline = [
        {"$match": {"normalized_name": {"$nin": [None, ""]}}},
        {
            "$group": {
                "_id": "$normalized_name",
                "count": {"$sum": 1},
                "brands": {"$addToSet": {"$ifNull": ["$brand", ""]}},
                "categories": {"$addToSet": {"$ifNull": ["$category", ""]}},
                "match_keys": {"$addToSet": {"$ifNull": ["$match_key", ""]}},
                "stores": {"$addToSet": {"$ifNull": ["$store_id", ""]}},
                "sample_names": {"$addToSet": {"$ifNull": ["$name", ""]}},
            }
        },
        {"$match": {"count": {"$gt": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 1000},
    ]
    rows = list(mongo.products.aggregate(pipeline))

    brand_conflicts: list[dict[str, Any]] = []
    category_conflicts: list[dict[str, Any]] = []
    match_key_conflicts: list[dict[str, Any]] = []

    for row in rows:
        normalized_name = str(row.get("_id", "")).strip()
        if not normalized_name:
            continue
        sample_names = _non_empty(row.get("sample_names") or [])
        sample_name = sample_names[0] if sample_names else normalized_name
        stores = _non_empty(row.get("stores") or [])
        brands = _non_empty(row.get("brands") or [])
        categories = _non_empty(row.get("categories") or [])
        match_keys = _non_empty(row.get("match_keys") or [])

        payload = {
            "normalized_name": normalized_name,
            "sample_name": sample_name,
            "count": int(row.get("count", 0)),
            "stores": stores,
            "brands": brands,
            "categories": categories,
            "match_keys_count": len(match_keys),
        }

        if len(brands) > 1:
            brand_conflicts.append(payload)
        if len(categories) > 1:
            category_conflicts.append(payload)
        if len(match_keys) > 1:
            match_key_conflicts.append(payload)

    return (
        brand_conflicts[:top_k],
        category_conflicts[:top_k],
        match_key_conflicts[:top_k],
    )


def _normalize_cluster_name(normalized_name: str) -> str:
    return normalize_name(normalized_name or "")


def _cluster_products(mongo: MongoService, normalized_name: str) -> list[dict[str, Any]]:
    key = _normalize_cluster_name(normalized_name)
    if not key:
        return []
    return list(mongo.products.find({"normalized_name": key}))


def _coerce_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _average_price(location_prices: list[dict[str, Any]]) -> float | None:
    amounts = [_coerce_float(item.get("amount")) for item in location_prices]
    values = [amount for amount in amounts if amount is not None]
    if not values:
        return None
    return round(sum(values) / len(values), 2)


def _merge_location_prices(
    current: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_location: dict[str, dict[str, Any]] = {}
    for source in [*current, *incoming]:
        location_id = str(source.get("location_id") or "").strip()
        if not location_id:
            continue
        payload = dict(source)
        amount = _coerce_float(payload.get("amount"))
        if amount is None:
            continue
        payload["amount"] = amount
        by_location[location_id] = payload
    return list(by_location.values())


def _size_info_from_doc(doc: dict[str, Any]) -> SizeInfo:
    size = doc.get("size") or {}
    if hasattr(size, "value") and hasattr(size, "unit"):
        return SizeInfo(value=size.value, unit=size.unit)
    if isinstance(size, dict):
        return SizeInfo(
            value=_coerce_float(size.get("value")),
            unit=str(size.get("unit")).strip().lower() if size.get("unit") else None,
        )
    return SizeInfo()


def _dominant_text_value(docs: list[dict[str, Any]], field: str) -> str | None:
    counts: dict[str, int] = {}
    display: dict[str, str] = {}
    for doc in docs:
        value = str(doc.get(field) or "").strip()
        if not value:
            continue
        key = value.lower()
        counts[key] = counts.get(key, 0) + 1
        display.setdefault(key, value)
    if not counts:
        return None
    winner = max(counts.items(), key=lambda item: item[1])[0]
    return display[winner]


def _dominant_size(docs: list[dict[str, Any]]) -> SizeInfo:
    counts: dict[tuple[float | None, str | None], int] = {}
    for doc in docs:
        size = _size_info_from_doc(doc)
        key = (size.value, size.unit)
        if size.value is None and size.unit is None:
            continue
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return _size_info_from_doc(docs[0]) if docs else SizeInfo()
    winner = max(counts.items(), key=lambda item: item[1])[0]
    return SizeInfo(value=winner[0], unit=winner[1])


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
async def curator_page(
    request: Request,
    limit: int = Query(25, ge=5, le=100),
    message: str = Query("", alias="message"),
    error: str = Query("", alias="error"),
):
    mongo = _get_mongo()
    brand_conflicts, category_conflicts, match_key_conflicts = _collect_curator_conflicts(
        mongo,
        top_k=limit,
    )
    recent_actions = _list_recent_curator_actions(mongo)
    return templates.TemplateResponse(
        "curator/list.html",
        {
            "request": request,
            "brand_conflicts": brand_conflicts,
            "category_conflicts": category_conflicts,
            "match_key_conflicts": match_key_conflicts,
            "recent_actions": recent_actions,
            "limit": limit,
            "message": message,
            "error": error,
            "page_title": "AI Curator",
            "active_nav": "curator",
        },
    )


@router.post("/curator/merge", response_class=HTMLResponse)
async def curator_merge_cluster(
    normalized_name: str = Form(...),
    limit: int = Form(25),
):
    mongo = _get_mongo()
    key = _normalize_cluster_name(normalized_name)
    if not key:
        msg = quote_plus("Invalid cluster name")
        return RedirectResponse(f"/admin/curator?limit={limit}&error={msg}", status_code=303)

    docs = _cluster_products(mongo, key)
    if len(docs) < 2:
        msg = quote_plus("Cluster has fewer than 2 products, nothing to merge")
        return RedirectResponse(f"/admin/curator?limit={limit}&error={msg}", status_code=303)
    before_docs = _snapshot_docs(docs)

    docs_sorted = sorted(
        docs,
        key=lambda item: (
            len(item.get("location_prices") or []),
            1 if item.get("estimated_price") is not None else 0,
            item.get("updated_at") or datetime.min,
        ),
        reverse=True,
    )
    canonical = docs_sorted[0]
    duplicates = docs_sorted[1:]

    dominant_brand = normalize_brand(_dominant_text_value(docs_sorted, "brand"))
    dominant_category = normalize_category(_dominant_text_value(docs_sorted, "category"))
    dominant_size = _dominant_size(docs_sorted)

    merged_prices = list(canonical.get("location_prices") or [])
    merged_tags = _non_empty(canonical.get("tags") or [])
    tag_keys = {tag.lower() for tag in merged_tags}
    merged_aliases = _non_empty(canonical.get("aliases") or [])
    alias_keys = {alias.lower() for alias in merged_aliases}

    canonical_name = str(canonical.get("name") or "").strip()
    image_url = canonical.get("image_url")
    url = canonical.get("url")

    for duplicate in duplicates:
        merged_prices = _merge_location_prices(
            merged_prices,
            duplicate.get("location_prices") or [],
        )
        duplicate_name = str(duplicate.get("name") or "").strip()
        if duplicate_name and duplicate_name.lower() != canonical_name.lower():
            if duplicate_name.lower() not in alias_keys:
                merged_aliases.append(duplicate_name)
                alias_keys.add(duplicate_name.lower())
        for tag in _non_empty(duplicate.get("tags") or []):
            lower_tag = tag.lower()
            if lower_tag not in tag_keys:
                merged_tags.append(tag)
                tag_keys.add(lower_tag)
        if not image_url and duplicate.get("image_url"):
            image_url = duplicate.get("image_url")
        if (not url or str(url).startswith("manual://")) and duplicate.get("url"):
            url = duplicate.get("url")

    if dominant_category:
        dominant_category_tag = dominant_category.lower()
        if dominant_category_tag not in tag_keys:
            merged_tags.append(dominant_category_tag)
            tag_keys.add(dominant_category_tag)

    updated_at = datetime.utcnow()
    updates = {
        "brand": dominant_brand,
        "category": dominant_category,
        "size": dominant_size.model_dump(),
        "location_prices": merged_prices,
        "estimated_price": _average_price(merged_prices),
        "tags": merged_tags,
        "aliases": merged_aliases,
        "image_url": image_url,
        "url": url,
        "match_key": build_match_key(key, dominant_brand, dominant_size),
        "checksum": build_checksum(
            str(canonical.get("store_id") or "unknown"),
            key,
            dominant_brand,
            dominant_size,
        ),
        "updated_at": updated_at,
    }

    mongo.products.update_one({"_id": canonical["_id"]}, {"$set": updates})
    duplicate_ids = [item["_id"] for item in duplicates]
    mongo.products.delete_many({"_id": {"$in": duplicate_ids}})
    after_docs = _cluster_products(mongo, key)
    action_id = _record_curator_action(
        mongo,
        action_type="merge_cluster",
        normalized_name=key,
        summary=(
            f"Merged {len(before_docs)} products into canonical product "
            f"'{canonical_name or key}'."
        ),
        before_docs=before_docs,
        after_docs=after_docs,
    )

    msg = quote_plus(
        f"Merged {len(docs)} products for '{canonical_name or key}'. "
        f"Removed {len(duplicates)} duplicates. Action #{action_id}."
    )
    return RedirectResponse(f"/admin/curator?limit={limit}&message={msg}", status_code=303)


@router.post("/curator/normalize-brand", response_class=HTMLResponse)
async def curator_normalize_brand(
    normalized_name: str = Form(...),
    limit: int = Form(25),
):
    mongo = _get_mongo()
    key = _normalize_cluster_name(normalized_name)
    if not key:
        msg = quote_plus("Invalid cluster name")
        return RedirectResponse(f"/admin/curator?limit={limit}&error={msg}", status_code=303)

    docs = _cluster_products(mongo, key)
    if len(docs) < 2:
        msg = quote_plus("Cluster has fewer than 2 products, nothing to normalize")
        return RedirectResponse(f"/admin/curator?limit={limit}&error={msg}", status_code=303)
    before_docs = _snapshot_docs(docs)

    dominant_brand = normalize_brand(_dominant_text_value(docs, "brand"))
    if not dominant_brand:
        msg = quote_plus("No non-empty brand found in cluster")
        return RedirectResponse(f"/admin/curator?limit={limit}&error={msg}", status_code=303)

    modified = 0
    for doc in docs:
        size = _size_info_from_doc(doc)
        updates = {
            "brand": dominant_brand,
            "match_key": build_match_key(key, dominant_brand, size),
            "checksum": build_checksum(
                str(doc.get("store_id") or "unknown"),
                key,
                dominant_brand,
                size,
            ),
            "updated_at": datetime.utcnow(),
        }
        result = mongo.products.update_one({"_id": doc["_id"]}, {"$set": updates})
        modified += int(result.modified_count)
    after_docs = _cluster_products(mongo, key)
    action_id = _record_curator_action(
        mongo,
        action_type="normalize_brand",
        normalized_name=key,
        summary=(
            f"Normalized brand to '{dominant_brand}' for "
            f"{len(before_docs)} products."
        ),
        before_docs=before_docs,
        after_docs=after_docs,
    )

    msg = quote_plus(
        f"Normalized brand to '{dominant_brand}' for {len(docs)} products "
        f"({modified} updated). Action #{action_id}."
    )
    return RedirectResponse(f"/admin/curator?limit={limit}&message={msg}", status_code=303)


@router.post("/curator/normalize-category", response_class=HTMLResponse)
async def curator_normalize_category(
    normalized_name: str = Form(...),
    limit: int = Form(25),
):
    mongo = _get_mongo()
    key = _normalize_cluster_name(normalized_name)
    if not key:
        msg = quote_plus("Invalid cluster name")
        return RedirectResponse(f"/admin/curator?limit={limit}&error={msg}", status_code=303)

    docs = _cluster_products(mongo, key)
    if len(docs) < 2:
        msg = quote_plus("Cluster has fewer than 2 products, nothing to normalize")
        return RedirectResponse(f"/admin/curator?limit={limit}&error={msg}", status_code=303)
    before_docs = _snapshot_docs(docs)

    dominant_category = normalize_category(_dominant_text_value(docs, "category"))
    if not dominant_category:
        msg = quote_plus("No non-empty category found in cluster")
        return RedirectResponse(f"/admin/curator?limit={limit}&error={msg}", status_code=303)

    category_tag = dominant_category.lower()
    modified = 0
    for doc in docs:
        tags = _non_empty(doc.get("tags") or [])
        lower_tags = {tag.lower() for tag in tags}
        if category_tag not in lower_tags:
            tags.append(category_tag)
        updates = {
            "category": dominant_category,
            "tags": tags,
            "updated_at": datetime.utcnow(),
        }
        result = mongo.products.update_one({"_id": doc["_id"]}, {"$set": updates})
        modified += int(result.modified_count)
    after_docs = _cluster_products(mongo, key)
    action_id = _record_curator_action(
        mongo,
        action_type="normalize_category",
        normalized_name=key,
        summary=(
            f"Normalized category to '{dominant_category}' for "
            f"{len(before_docs)} products."
        ),
        before_docs=before_docs,
        after_docs=after_docs,
    )

    msg = quote_plus(
        f"Normalized category to '{dominant_category}' for {len(docs)} products "
        f"({modified} updated). Action #{action_id}."
    )
    return RedirectResponse(f"/admin/curator?limit={limit}&message={msg}", status_code=303)


@router.post("/curator/actions/{action_id}/undo", response_class=HTMLResponse)
async def curator_undo_action(
    action_id: str,
    limit: int = Form(25),
):
    if not ObjectId.is_valid(action_id):
        msg = quote_plus("Invalid action ID")
        return RedirectResponse(f"/admin/curator?limit={limit}&error={msg}", status_code=303)

    mongo = _get_mongo()
    oid = ObjectId(action_id)
    action = mongo.curator_actions.find_one({"_id": oid})
    if not action:
        msg = quote_plus("Curator action not found")
        return RedirectResponse(f"/admin/curator?limit={limit}&error={msg}", status_code=303)
    if action.get("status") == "undone":
        msg = quote_plus("Action has already been undone")
        return RedirectResponse(f"/admin/curator?limit={limit}&error={msg}", status_code=303)

    before_docs = action.get("before_docs") or []
    after_docs = action.get("after_docs") or []
    if not before_docs:
        msg = quote_plus("Action has no stored snapshot to restore")
        return RedirectResponse(f"/admin/curator?limit={limit}&error={msg}", status_code=303)

    before_ids = {
        doc.get("_id")
        for doc in before_docs
        if isinstance(doc, dict) and doc.get("_id") is not None
    }
    restored = 0
    for doc in before_docs:
        if not isinstance(doc, dict) or doc.get("_id") is None:
            continue
        mongo.products.replace_one({"_id": doc["_id"]}, doc, upsert=True)
        restored += 1

    removed = 0
    for doc in after_docs:
        if not isinstance(doc, dict):
            continue
        doc_id = doc.get("_id")
        if doc_id is None or doc_id in before_ids:
            continue
        result = mongo.products.delete_one({"_id": doc_id})
        removed += int(result.deleted_count)

    mongo.curator_actions.update_one(
        {"_id": oid},
        {
            "$set": {
                "status": "undone",
                "undone_at": datetime.utcnow(),
                "undo_stats": {"restored": restored, "removed": removed},
            }
        },
    )

    msg = quote_plus(
        f"Undid action {action_id}: restored {restored} products, removed {removed} products."
    )
    return RedirectResponse(f"/admin/curator?limit={limit}&message={msg}", status_code=303)
