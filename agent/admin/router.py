from __future__ import annotations

import json
import math
import threading
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit

import yaml
from bson import ObjectId
from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from agent.config.categories import STANDARD_CATEGORIES
from agent.db.models import LocationPrice, Product, SizeInfo
from agent.db.mongo import MongoService
from agent.scraping.normalizer import (
    build_checksum,
    build_match_key,
    normalize_brand,
    normalize_category,
    normalize_display_name,
    normalize_name,
    parse_size,
    strip_size_from_name,
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

CURATOR_SNAPSHOT_KEY = "primary"
CURATOR_SNAPSHOT_ROW_CAP = 100

_EMPTY_ROW_MATCH_KEY = '<tr><td colspan="6" class="px-4 py-10 text-center text-gray-400">No match-key conflicts found in current dataset</td></tr>'
_EMPTY_ROW_BRAND = '<tr><td colspan="6" class="px-4 py-10 text-center text-gray-400">No brand conflicts found</td></tr>'
_EMPTY_ROW_CATEGORY = '<tr><td colspan="6" class="px-4 py-10 text-center text-gray-400">No category conflicts found</td></tr>'
_EMPTY_ROW_FLAGS = '<tr><td colspan="6" class="px-4 py-10 text-center text-gray-400">No manual merge flags yet</td></tr>'
_EMPTY_ROW_NAME_SIZE = '<tr><td colspan="5" class="px-4 py-10 text-center text-gray-400">No name/size normalization candidates found</td></tr>'
_EMPTY_ROW_PHOTOS = '<tr><td colspan="5" class="px-4 py-10 text-center text-gray-400">No missing-photo products found</td></tr>'


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


def _visible_cluster_keys(form: Any, field_name: str = "normalized_names") -> list[str]:
    values = form.getlist(field_name) if hasattr(form, "getlist") else []
    return _non_empty([_normalize_cluster_name(str(value)) for value in values])


def _visible_product_ids(form: Any, field_name: str = "product_ids") -> list[ObjectId]:
    values = form.getlist(field_name) if hasattr(form, "getlist") else []
    rows: list[ObjectId] = []
    seen: set[str] = set()
    for value in values:
        oid = _object_id_or_none(str(value))
        if oid is None:
            continue
        oid_text = str(oid)
        if oid_text in seen:
            continue
        seen.add(oid_text)
        rows.append(oid)
    return rows


def _is_missing_photo(doc: dict[str, Any]) -> bool:
    image_url = str(doc.get("image_url") or "").strip()
    return not image_url or image_url.startswith("data:")


def _float_text(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:g}"


def _format_size(size: SizeInfo) -> str:
    if size.pack_count and size.value is not None and size.unit:
        return f"{size.pack_count} x {_float_text(size.value)}{size.unit}"
    if size.pack_count:
        return f"{size.pack_count} pack"
    if size.value is not None and size.unit:
        return f"{_float_text(size.value)}{size.unit}"
    if size.value is not None:
        return _float_text(size.value)
    return "—"


def _format_pack(size: SizeInfo) -> str:
    """Format just the pack/qty portion, e.g. '6-pack'."""
    if size.pack_count and size.pack_count > 1:
        return f"{size.pack_count}-pack"
    return ""


def _format_measure(size: SizeInfo) -> str:
    """Format just the measure portion (value + unit), e.g. '330ml'.

    When unit is 'count', the info is purely a pack quantity (already shown
    by _format_pack), so we return '' to avoid redundant display like '3count'.
    """
    if size.unit in ("count", "pack"):
        return ""
    if size.value is not None and size.unit:
        return f"{_float_text(size.value)}{size.unit}"
    if size.value is not None:
        return _float_text(size.value)
    return ""


def _size_signature(size: SizeInfo) -> tuple[float | None, str | None, int | None]:
    return (size.value, size.unit, size.pack_count)


def _size_signatures_from_docs(
    docs: list[dict[str, Any]],
) -> set[tuple[float | None, str | None, int | None]]:
    """Collect distinct size signatures from docs.

    Includes (None, None, None) so that products missing size info are treated
    as a distinct variant — prevents merging a 3-pack with a single item that
    happens to have no size data.
    """
    signatures: set[tuple[float | None, str | None, int | None]] = set()
    for doc in docs:
        signatures.add(_size_signature(_size_info_from_doc(doc)))
    return signatures


def _size_labels_from_docs(docs: list[dict[str, Any]]) -> list[str]:
    labels: list[str] = []
    for doc in docs:
        label = _format_size(_size_info_from_doc(doc))
        if label == "—":
            continue
        if label not in labels:
            labels.append(label)
    return labels


def _split_size_labels_from_docs(
    docs: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    """Return deduplicated (pack_labels, measure_labels) from a list of docs."""
    packs: list[str] = []
    measures: list[str] = []
    for doc in docs:
        si = _size_info_from_doc(doc)
        pl = _format_pack(si)
        ml = _format_measure(si)
        if pl and pl not in packs:
            packs.append(pl)
        if ml and ml not in measures:
            measures.append(ml)
    return packs, measures


def _size_hint_from_size(size: SizeInfo) -> str:
    if size.pack_count and size.value is not None and size.unit:
        return f"{size.pack_count}x{_float_text(size.value)}{size.unit}"
    if size.pack_count:
        return f"{size.pack_count} pack"
    if size.value is not None and size.unit:
        return f"{_float_text(size.value)}{size.unit}"
    return ""


def _cleanup_name_suggestion(name: str) -> str:
    stripped = strip_size_from_name(name).strip()
    candidate = stripped or name
    normalized = normalize_display_name(candidate)
    return normalized.strip() or name.strip()


def _build_back_target(
    request: Request,
    fallback: str,
    *,
    message: str | None = None,
    error: str | None = None,
) -> str:
    referer = request.headers.get("referer", "")
    parsed = urlsplit(referer)
    if parsed.path.startswith("/admin/"):
        base_path = parsed.path
        query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    else:
        fallback_parsed = urlsplit(fallback)
        base_path = fallback_parsed.path or fallback
        query_pairs = parse_qsl(fallback_parsed.query, keep_blank_values=True)

    query_params: dict[str, str] = {}
    for key, value in query_pairs:
        query_params[key] = value

    if message:
        query_params["message"] = message
    if error:
        query_params["error"] = error

    query = urlencode(query_params)
    return f"{base_path}?{query}" if query else base_path


def _redirect_back(
    request: Request,
    fallback: str,
    *,
    message: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    target = _build_back_target(request, fallback, message=message, error=error)
    return RedirectResponse(target, status_code=303)


def _is_hx_request(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def _hx_trigger_response(
    *,
    message: str | None = None,
    error: str | None = None,
    events: list[str] | None = None,
    reset_form_id: str | None = None,
    status_code: int = 200,
    body: str = "",
) -> HTMLResponse:
    payload: dict[str, Any] = {}
    if message:
        payload["admin:notify"] = {"level": "success", "message": message}
    if error:
        payload["admin:notify"] = {"level": "error", "message": error}
    for event_name in events or []:
        payload[event_name] = True
    if reset_form_id:
        payload["admin:reset-form"] = {"id": reset_form_id}
    headers = {"HX-Trigger": json.dumps(payload)} if payload else {}
    return HTMLResponse(body, status_code=status_code, headers=headers)


def _feedback_response(
    request: Request,
    *,
    fallback_path: str,
    message: str | None = None,
    error: str | None = None,
    events: list[str] | None = None,
    reset_form_id: str | None = None,
    extra_params: dict[str, Any] | None = None,
) -> HTMLResponse | RedirectResponse:
    if _is_hx_request(request):
        return _hx_trigger_response(
            message=message,
            error=error,
            events=events,
            reset_form_id=reset_form_id,
        )

    params: dict[str, Any] = dict(extra_params or {})
    if message:
        params["message"] = message
    if error:
        params["error"] = error
    query = urlencode(params, quote_via=quote_plus)
    target = f"{fallback_path}?{query}" if query else fallback_path
    return RedirectResponse(target, status_code=303)


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
    include_sizes: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    dismissed = _dismissed_names(mongo, "conflicts")
    pipeline_limit = min(max(top_k * 8, top_k), 5000)
    excluded = [None, ""] + list(dismissed)
    pipeline = [
        {"$match": {"normalized_name": {"$nin": excluded}}},
        {
            "$group": {
                "_id": "$normalized_name",
                "count": {"$sum": 1},
                "brands": {"$addToSet": {"$ifNull": ["$brand", ""]}},
                "categories": {"$addToSet": {"$ifNull": ["$category", ""]}},
                "match_keys": {"$addToSet": {"$ifNull": ["$match_key", ""]}},
                "stores": {"$addToSet": {"$ifNull": ["$store_id", ""]}},
                "sample_names": {"$addToSet": {"$ifNull": ["$name", ""]}},
                "sizes": {"$addToSet": {"$ifNull": ["$size", {}]}},
            }
        },
        {"$match": {"count": {"$gt": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": pipeline_limit},
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
        size_docs = [{"size": size_doc} for size_doc in (row.get("sizes") or [])]
        size_signatures = _size_signatures_from_docs(size_docs)
        has_mixed_sizes = len(size_signatures) > 1

        payload = {
            "normalized_name": normalized_name,
            "sample_name": sample_name,
            "count": int(row.get("count", 0)),
            "stores": stores,
            "brands": brands,
            "categories": categories,
            "match_keys_count": len(match_keys),
        }
        if include_sizes:
            size_labels = _size_labels_from_docs(size_docs)
            payload["sizes"] = size_labels
            payload["sizes_label"] = ", ".join(size_labels) if size_labels else "—"
            pack_labels, measure_labels = _split_size_labels_from_docs(size_docs)
            payload["packs_label"] = ", ".join(pack_labels) if pack_labels else "—"
            payload["measures_label"] = ", ".join(measure_labels) if measure_labels else "—"

        if len(brands) > 1:
            brand_conflicts.append(payload)
        if len(categories) > 1:
            category_conflicts.append(payload)
        if len(match_keys) > 1 and not has_mixed_sizes:
            match_key_conflicts.append(payload)

    return (
        brand_conflicts[:top_k],
        category_conflicts[:top_k],
        match_key_conflicts[:top_k],
    )


def _collect_manual_merge_flags(
    mongo: MongoService,
    *,
    top_k: int = 25,
) -> tuple[list[dict[str, Any]], int]:
    dismissed = _dismissed_names(mongo, "manual-flags")
    excluded = [None, ""] + list(dismissed)
    pipeline = [
        {"$match": {"curator.manual_merge_flag": True, "normalized_name": {"$nin": excluded}}},
        {
            "$group": {
                "_id": "$normalized_name",
                "count": {"$sum": 1},
                "stores": {"$addToSet": {"$ifNull": ["$store_id", ""]}},
                "sample_names": {"$addToSet": {"$ifNull": ["$name", ""]}},
                "notes": {"$addToSet": {"$ifNull": ["$curator.manual_merge_note", ""]}},
                "sizes": {"$addToSet": {"$ifNull": ["$size", {}]}},
            }
        },
        {"$sort": {"count": -1}},
        {"$limit": top_k},
    ]
    rows = list(mongo.products.aggregate(pipeline))

    results: list[dict[str, Any]] = []
    for row in rows:
        normalized_name = str(row.get("_id", "")).strip()
        if not normalized_name:
            continue
        sample_names = _non_empty(row.get("sample_names") or [])
        notes = _non_empty(row.get("notes") or [])
        size_docs = [{"size": size_doc} for size_doc in (row.get("sizes") or [])]
        size_labels = _size_labels_from_docs(size_docs)
        pack_labels, measure_labels = _split_size_labels_from_docs(size_docs)
        results.append(
            {
                "normalized_name": normalized_name,
                "sample_name": sample_names[0] if sample_names else normalized_name,
                "count": int(row.get("count", 0)),
                "stores": _non_empty(row.get("stores") or []),
                "notes": notes,
                "sizes": size_labels,
                "sizes_label": ", ".join(size_labels) if size_labels else "—",
                "packs_label": ", ".join(pack_labels) if pack_labels else "—",
                "measures_label": ", ".join(measure_labels) if measure_labels else "—",
            }
        )

    total_flagged_products = mongo.products.count_documents({"curator.manual_merge_flag": True})
    return results, total_flagged_products


def _collect_name_cleanup_candidates(
    mongo: MongoService,
    *,
    top_k: int = 25,
    compute_total: bool = True,
) -> tuple[list[dict[str, Any]], int]:
    dismissed = _dismissed_names(mongo, "name-size")
    projection = {
        "name": 1,
        "store_id": 1,
        "store_name": 1,
        "normalized_name": 1,
        "size": 1,
        "updated_at": 1,
    }
    rows: list[dict[str, Any]] = []
    total_candidates = 0
    cursor = mongo.products.find({}, projection)

    for doc in cursor:
        raw_name = str(doc.get("name") or "").strip()
        if not raw_name:
            continue
        norm = str(doc.get("normalized_name") or "")
        if norm in dismissed:
            continue

        stripped = strip_size_from_name(raw_name).strip()
        suggested_name = _cleanup_name_suggestion(raw_name)
        inferred_size = parse_size(raw_name)
        existing_size = _size_info_from_doc(doc)

        has_size_tokens = bool(stripped and normalize_name(stripped) != normalize_name(raw_name))
        has_case_issue = raw_name != suggested_name
        all_caps = raw_name.upper() == raw_name and any(ch.isalpha() for ch in raw_name)
        size_missing = (
            existing_size.value is None
            and existing_size.unit is None
            and inferred_size.value is not None
            and inferred_size.unit is not None
        )
        pack_missing = (
            inferred_size.pack_count is not None
            and (existing_size.pack_count or 0) != inferred_size.pack_count
        )

        if not (has_size_tokens or has_case_issue or size_missing or pack_missing):
            continue

        if compute_total:
            total_candidates += 1
        if len(rows) >= top_k:
            if not compute_total:
                break
            continue

        reasons: list[str] = []
        if has_size_tokens:
            reasons.append("size in name")
        if has_case_issue:
            reasons.append("case mismatch")
        if all_caps:
            reasons.append("all caps")
        if size_missing:
            reasons.append("size missing")
        if pack_missing:
            reasons.append("pack count missing")

        rows.append(
            {
                "product_id": str(doc.get("_id")),
                "current_name": raw_name,
                "normalized_name": str(doc.get("normalized_name") or ""),
                "suggested_name": suggested_name,
                "store_name": doc.get("store_name") or doc.get("store_id") or "—",
                "current_size": _format_size(existing_size),
                "suggested_size": _format_size(inferred_size),
                "current_pack": _format_pack(existing_size),
                "current_measure": _format_measure(existing_size),
                "suggested_pack": _format_pack(inferred_size),
                "suggested_measure": _format_measure(inferred_size),
                "reasons": reasons,
            }
        )

    if not compute_total:
        total_candidates = len(rows)
    return rows, total_candidates


def _collect_missing_photo_candidates(
    mongo: MongoService,
    *,
    top_k: int = 25,
) -> tuple[list[dict[str, Any]], int]:
    dismissed = _dismissed_names(mongo, "missing-photos")
    missing_photo_query: dict[str, Any] = {
        "$or": [
            {"image_url": {"$in": [None, ""]}},
            {"image_url": {"$regex": "^data:", "$options": "i"}},
        ]
    }
    if dismissed:
        missing_photo_query["normalized_name"] = {"$nin": list(dismissed)}
    total_missing = mongo.products.count_documents(missing_photo_query)
    docs = list(
        mongo.products.find(
            missing_photo_query,
            {
                "name": 1,
                "store_id": 1,
                "store_name": 1,
                "brand": 1,
                "category": 1,
                "normalized_name": 1,
                "size": 1,
                "updated_at": 1,
                "curator.photo_upload_needed": 1,
            },
        )
        .sort("updated_at", -1)
        .limit(top_k)
    )
    for doc in docs:
        doc["_id"] = str(doc["_id"])
        curator_meta = doc.get("curator") or {}
        doc["_photo_upload_needed"] = bool(curator_meta.get("photo_upload_needed"))
        _si = _size_info_from_doc(doc)
        doc["_size_label"] = _format_size(_si)
        doc["_pack_label"] = _format_pack(_si)
        doc["_measure_label"] = _format_measure(_si)
    return docs, total_missing


PRICE_ANOMALY_RATIO = 2.5  # flag when max/min price ratio exceeds this


def _collect_price_anomalies(
    mongo: MongoService,
    *,
    top_k: int = 25,
    ratio_threshold: float = PRICE_ANOMALY_RATIO,
) -> list[dict[str, Any]]:
    """Find products sharing the same normalized_name where cross-store prices
    differ by more than *ratio_threshold*.  A 4x price gap for the same size
    usually means one product has the wrong size attached to its price
    (e.g. the 517g price was stored under a 184g record)."""
    dismissed = _dismissed_names(mongo, "price-anomalies")
    excluded = [None, ""] + list(dismissed)

    pipeline: list[dict[str, Any]] = [
        {"$match": {"normalized_name": {"$nin": excluded}, "estimated_price": {"$gt": 0}}},
        {
            "$group": {
                "_id": "$normalized_name",
                "count": {"$sum": 1},
                "min_price": {"$min": "$estimated_price"},
                "max_price": {"$max": "$estimated_price"},
                "stores": {"$addToSet": {"$ifNull": ["$store_id", ""]}},
                "sample_names": {"$addToSet": {"$ifNull": ["$name", ""]}},
                "sizes": {"$addToSet": {"$ifNull": ["$size", {}]}},
                "prices": {
                    "$push": {
                        "store": {"$ifNull": ["$store_name", "$store_id"]},
                        "price": "$estimated_price",
                        "size": "$size",
                    }
                },
            }
        },
        {"$match": {"count": {"$gt": 1}}},
        {
            "$addFields": {
                "price_ratio": {
                    "$cond": [
                        {"$gt": ["$min_price", 0]},
                        {"$divide": ["$max_price", "$min_price"]},
                        0,
                    ]
                }
            }
        },
        {"$match": {"price_ratio": {"$gte": ratio_threshold}}},
        {"$sort": {"price_ratio": -1}},
        {"$limit": top_k},
    ]

    rows = list(mongo.products.aggregate(pipeline))
    results: list[dict[str, Any]] = []
    for row in rows:
        normalized_name = str(row.get("_id", "")).strip()
        if not normalized_name:
            continue
        sample_names = _non_empty(row.get("sample_names") or [])
        stores = _non_empty(row.get("stores") or [])
        size_docs = [{"size": sd} for sd in (row.get("sizes") or [])]
        packs, measures = _split_size_labels_from_docs(size_docs)

        # Build per-store price breakdown for display
        price_details = []
        for entry in row.get("prices") or []:
            si = _size_info_from_doc({"size": entry.get("size") or {}})
            price_details.append(
                {
                    "store": entry.get("store") or "—",
                    "price": entry.get("price"),
                    "size_label": _format_size(si),
                }
            )
        price_details.sort(key=lambda d: d.get("price") or 0)

        results.append(
            {
                "normalized_name": normalized_name,
                "sample_name": sample_names[0] if sample_names else normalized_name,
                "count": int(row.get("count", 0)),
                "stores": stores,
                "packs_label": ", ".join(packs) if packs else "—",
                "measures_label": ", ".join(measures) if measures else "—",
                "min_price": round(row.get("min_price", 0), 2),
                "max_price": round(row.get("max_price", 0), 2),
                "price_ratio": round(row.get("price_ratio", 0), 1),
                "price_details": price_details,
            }
        )
    return results


def _dismissed_names(mongo: MongoService, section: str) -> set[str]:
    """Return normalized_names that have been dismissed for *section*."""
    docs = mongo.curator_dismissed.find(
        {"section": section},
        {"normalized_name": 1},
    )
    return {str(d.get("normalized_name", "")) for d in docs} - {""}


def _dismissed_names_all(mongo: MongoService) -> set[str]:
    """Return normalized_names dismissed in *any* section."""
    docs = mongo.curator_dismissed.find({}, {"normalized_name": 1})
    return {str(d.get("normalized_name", "")) for d in docs} - {""}


def _curator_row_limit(limit: int) -> int:
    return max(5, min(int(limit), CURATOR_SNAPSHOT_ROW_CAP))


def _build_curator_snapshot(
    mongo: MongoService,
    *,
    row_cap: int = CURATOR_SNAPSHOT_ROW_CAP,
) -> dict[str, Any]:
    capped_limit = _curator_row_limit(row_cap)
    brand_conflicts, category_conflicts, match_key_conflicts = _collect_curator_conflicts(
        mongo,
        top_k=capped_limit,
        include_sizes=True,
    )
    manual_merge_flags, flagged_product_total = _collect_manual_merge_flags(
        mongo,
        top_k=capped_limit,
    )
    name_cleanup_candidates, name_cleanup_total = _collect_name_cleanup_candidates(
        mongo,
        top_k=capped_limit,
        compute_total=False,
    )
    missing_photo_candidates, missing_photo_total = _collect_missing_photo_candidates(
        mongo,
        top_k=capped_limit,
    )
    price_anomalies = _collect_price_anomalies(
        mongo,
        top_k=capped_limit,
    )

    now = datetime.utcnow()
    return {
        "snapshot_key": CURATOR_SNAPSHOT_KEY,
        "row_cap": capped_limit,
        "generated_at": now,
        "updated_at": now,
        "stale": False,
        "stale_reason": "",
        "brand_conflicts": brand_conflicts,
        "category_conflicts": category_conflicts,
        "match_key_conflicts": match_key_conflicts,
        "manual_merge_flags": manual_merge_flags,
        "flagged_product_total": int(flagged_product_total),
        "name_cleanup_candidates": name_cleanup_candidates,
        "name_cleanup_total": int(name_cleanup_total),
        "missing_photo_candidates": missing_photo_candidates,
        "missing_photo_total": int(missing_photo_total),
        "price_anomalies": price_anomalies,
    }


def _refresh_curator_snapshot(
    mongo: MongoService,
    *,
    row_cap: int = CURATOR_SNAPSHOT_ROW_CAP,
) -> dict[str, Any]:
    payload = _build_curator_snapshot(mongo, row_cap=row_cap)
    mongo.curator_snapshots.update_one(
        {"snapshot_key": CURATOR_SNAPSHOT_KEY},
        {"$set": payload},
        upsert=True,
    )
    return payload


def _refresh_curator_section(
    mongo: MongoService,
    section: str,
    *,
    row_cap: int = CURATOR_SNAPSHOT_ROW_CAP,
) -> dict[str, Any]:
    """Re-scan a single curator section and update the cached snapshot."""
    capped_limit = _curator_row_limit(row_cap)
    now = datetime.utcnow()
    updates: dict[str, Any] = {"updated_at": now}

    if section == "conflicts":
        brand, category, match_key = _collect_curator_conflicts(
            mongo, top_k=capped_limit, include_sizes=True,
        )
        updates["brand_conflicts"] = brand
        updates["category_conflicts"] = category
        updates["match_key_conflicts"] = match_key
    elif section == "manual-flags":
        flags, total = _collect_manual_merge_flags(mongo, top_k=capped_limit)
        updates["manual_merge_flags"] = flags
        updates["flagged_product_total"] = int(total)
    elif section == "name-size":
        candidates, total = _collect_name_cleanup_candidates(
            mongo, top_k=capped_limit, compute_total=False,
        )
        updates["name_cleanup_candidates"] = candidates
        updates["name_cleanup_total"] = int(total)
    elif section == "missing-photos":
        candidates, total = _collect_missing_photo_candidates(mongo, top_k=capped_limit)
        updates["missing_photo_candidates"] = candidates
        updates["missing_photo_total"] = int(total)
    elif section == "price-anomalies":
        updates["price_anomalies"] = _collect_price_anomalies(mongo, top_k=capped_limit)
    else:
        return {}

    mongo.curator_snapshots.update_one(
        {"snapshot_key": CURATOR_SNAPSHOT_KEY},
        {"$set": updates},
        upsert=True,
    )
    return updates


def _mark_curator_snapshot_stale(
    mongo: MongoService,
    *,
    reason: str = "Catalog data changed since last scan",
) -> None:
    now = datetime.utcnow()
    mongo.curator_snapshots.update_one(
        {"snapshot_key": CURATOR_SNAPSHOT_KEY},
        {
            "$set": {
                "stale": True,
                "stale_reason": reason,
                "updated_at": now,
            },
            "$setOnInsert": {
                "snapshot_key": CURATOR_SNAPSHOT_KEY,
                "generated_at": None,
                "row_cap": CURATOR_SNAPSHOT_ROW_CAP,
            },
        },
        upsert=True,
    )


def _latest_product_updated_at(mongo: MongoService) -> datetime | None:
    doc = mongo.products.find_one({}, {"updated_at": 1}, sort=[("updated_at", -1)])
    if not doc:
        return None
    value = doc.get("updated_at")
    return value if isinstance(value, datetime) else None


def _load_curator_snapshot(
    mongo: MongoService,
    *,
    ensure: bool = True,
) -> dict[str, Any]:
    snapshot = mongo.curator_snapshots.find_one({"snapshot_key": CURATOR_SNAPSHOT_KEY})
    if not snapshot and ensure:
        snapshot = _refresh_curator_snapshot(mongo, row_cap=CURATOR_SNAPSHOT_ROW_CAP)
    if not snapshot:
        return {}

    generated_at = snapshot.get("generated_at")
    latest_product_update = _latest_product_updated_at(mongo)
    should_mark_stale = (
        latest_product_update is not None
        and (not isinstance(generated_at, datetime) or latest_product_update > generated_at)
    )
    if should_mark_stale and not bool(snapshot.get("stale")):
        reason = "New product updates detected after the last curator scan"
        _mark_curator_snapshot_stale(mongo, reason=reason)
        snapshot["stale"] = True
        snapshot["stale_reason"] = reason
        snapshot["updated_at"] = datetime.utcnow()

    return snapshot


def _snapshot_rows(snapshot: dict[str, Any], key: str, limit: int) -> list[dict[str, Any]]:
    rows = snapshot.get(key) or []
    if not isinstance(rows, list):
        return []
    return rows[:_curator_row_limit(limit)]


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
        pack_count = getattr(size, "pack_count", None)
        return SizeInfo(value=size.value, unit=size.unit, pack_count=pack_count)
    if isinstance(size, dict):
        return SizeInfo(
            value=_coerce_float(size.get("value")),
            unit=str(size.get("unit")).strip().lower() if size.get("unit") else None,
            pack_count=int(size.get("pack_count")) if size.get("pack_count") else None,
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
    counts: dict[tuple[float | None, str | None, int | None], int] = {}
    for doc in docs:
        size = _size_info_from_doc(doc)
        key = (size.value, size.unit, size.pack_count)
        if size.value is None and size.unit is None and size.pack_count is None:
            continue
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return _size_info_from_doc(docs[0]) if docs else SizeInfo()
    winner = max(counts.items(), key=lambda item: item[1])[0]
    return SizeInfo(value=winner[0], unit=winner[1], pack_count=winner[2])


def _execute_merge_cluster(mongo: MongoService, key: str) -> dict[str, Any]:
    docs = _cluster_products(mongo, key)
    if len(docs) < 2:
        raise ValueError("Cluster has fewer than 2 products, nothing to merge")
    size_signatures = _size_signatures_from_docs(docs)
    if len(size_signatures) > 1:
        raise ValueError("Cluster has multiple size variants, merge by size only")

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
    return {
        "docs": len(docs),
        "duplicates_removed": len(duplicates),
        "canonical_name": canonical_name or key,
        "action_id": str(action_id),
    }


def _execute_normalize_brand_cluster(mongo: MongoService, key: str) -> dict[str, Any]:
    docs = _cluster_products(mongo, key)
    if len(docs) < 2:
        raise ValueError("Cluster has fewer than 2 products, nothing to normalize")

    before_docs = _snapshot_docs(docs)
    dominant_brand = normalize_brand(_dominant_text_value(docs, "brand"))
    if not dominant_brand:
        raise ValueError("No non-empty brand found in cluster")

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
    return {
        "docs": len(docs),
        "updated": modified,
        "dominant_brand": dominant_brand,
        "action_id": str(action_id),
    }


def _execute_normalize_category_cluster(
    mongo: MongoService,
    key: str,
    *,
    target_category: str | None = None,
) -> dict[str, Any]:
    docs = _cluster_products(mongo, key)
    if len(docs) < 2:
        raise ValueError("Cluster has fewer than 2 products, nothing to normalize")

    before_docs = _snapshot_docs(docs)
    if target_category:
        dominant_category = target_category
    else:
        dominant_category = normalize_category(_dominant_text_value(docs, "category"))
    if not dominant_category:
        raise ValueError("No non-empty category found in cluster")

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
    return {
        "docs": len(docs),
        "updated": modified,
        "dominant_category": dominant_category,
        "action_id": str(action_id),
    }


def _execute_normalize_name_size_product(
    mongo: MongoService,
    oid: ObjectId,
) -> dict[str, Any]:
    doc = mongo.products.find_one({"_id": oid})
    if not doc:
        raise ValueError("Product not found")

    before_docs = _snapshot_docs([doc])
    original_name = str(doc.get("name") or "").strip()
    if not original_name:
        raise ValueError("Product has no name to normalize")

    cleaned_name = _cleanup_name_suggestion(original_name)
    normalized_name = normalize_name(cleaned_name)
    existing_size = _size_info_from_doc(doc)
    inferred_size = parse_size(original_name)
    if (
        inferred_size.value is not None
        or inferred_size.unit is not None
        or inferred_size.pack_count
    ):
        final_size = inferred_size
    else:
        final_size = existing_size

    normalized_brand = normalize_brand(str(doc.get("brand") or "").strip() or None)
    updates = {
        "name": cleaned_name,
        "normalized_name": normalized_name,
        "size": final_size.model_dump(),
        "match_key": build_match_key(normalized_name, normalized_brand, final_size),
        "checksum": build_checksum(
            str(doc.get("store_id") or "unknown"),
            normalized_name,
            normalized_brand,
            final_size,
        ),
        "updated_at": datetime.utcnow(),
    }
    mongo.products.update_one({"_id": oid}, {"$set": updates})

    after_docs = list(mongo.products.find({"_id": oid}))
    action_id = _record_curator_action(
        mongo,
        action_type="normalize_name_size",
        normalized_name=normalized_name,
        summary=(
            f"Normalized name to '{cleaned_name}' and refreshed size "
            f"({_format_size(final_size)})."
        ),
        before_docs=before_docs,
        after_docs=after_docs,
    )
    return {
        "original_name": original_name,
        "cleaned_name": cleaned_name,
        "size_label": _format_size(final_size),
        "action_id": str(action_id),
        "normalized_name": normalized_name,
    }


def _execute_clear_manual_flags_cluster(mongo: MongoService, key: str) -> dict[str, Any]:
    docs = list(
        mongo.products.find(
            {"normalized_name": key, "curator.manual_merge_flag": True}
        )
    )
    if not docs:
        raise ValueError("No manual flags found for that cluster")

    before_docs = _snapshot_docs(docs)
    ids = [doc["_id"] for doc in docs]
    mongo.products.update_many(
        {"_id": {"$in": ids}},
        {
            "$unset": {
                "curator.manual_merge_flag": "",
                "curator.manual_merge_note": "",
                "curator.flagged_at": "",
            },
            "$set": {"updated_at": datetime.utcnow()},
        },
    )
    after_docs = list(mongo.products.find({"_id": {"$in": ids}}))
    action_id = _record_curator_action(
        mongo,
        action_type="clear_manual_flags",
        normalized_name=key,
        summary=f"Cleared manual merge flags from {len(ids)} products.",
        before_docs=before_docs,
        after_docs=after_docs,
    )
    return {"cleared": len(ids), "action_id": str(action_id)}


def _dashboard_store_stats(mongo: MongoService) -> list[dict[str, Any]]:
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
    return list(mongo.products.aggregate(pipeline))


def _dashboard_recent_jobs(mongo: MongoService, limit: int = 10) -> list[dict[str, Any]]:
    recent_jobs = list(mongo.jobs.find().sort("started_at", -1).limit(limit))
    for job in recent_jobs:
        job["_id"] = str(job["_id"])
    return recent_jobs


def _dashboard_snapshot(mongo: MongoService) -> dict[str, Any]:
    store_stats = _dashboard_store_stats(mongo)
    total_products = sum(s["count"] for s in store_stats)
    recent_jobs = _dashboard_recent_jobs(mongo, limit=10)
    running_count = mongo.jobs.count_documents(
        {"status": {"$in": ["running", "stopping", "cancel_requested"]}}
    )
    return {
        "store_stats": store_stats,
        "total_products": total_products,
        "recent_jobs": recent_jobs,
        "running_count": running_count,
    }


def _build_products_query(
    *,
    q: str,
    store: str,
    flagged: str,
    photo: str,
) -> dict[str, Any]:
    query: dict[str, Any] = {}
    conditions: list[dict[str, Any]] = []
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
    if flagged == "1":
        conditions.append({"curator.manual_merge_flag": True})
    if photo == "missing":
        conditions.append(
            {
                "$or": [
                    {"image_url": {"$in": [None, ""]}},
                    {"image_url": {"$regex": "^data:", "$options": "i"}},
                ]
            }
        )
    elif photo == "present":
        conditions.append(
            {
                "$and": [
                    {"image_url": {"$exists": True, "$nin": [None, ""]}},
                    {"image_url": {"$not": {"$regex": "^data:", "$options": "i"}}},
                ]
            }
        )
    if conditions:
        query = {"$and": conditions} if len(conditions) > 1 else conditions[0]
    return query


def _product_list_payload(
    mongo: MongoService,
    *,
    q: str,
    store: str,
    flagged: str,
    photo: str,
    page: int,
    limit: int,
) -> dict[str, Any]:
    skip = (page - 1) * limit
    query = _build_products_query(q=q, store=store, flagged=flagged, photo=photo)

    products = list(
        mongo.products.find(query, {"embedding": 0}).sort("updated_at", -1).skip(skip).limit(limit)
    )
    total = mongo.products.count_documents(query)
    total_pages = max(math.ceil(total / limit), 1) if limit else 1
    for product in products:
        product["_id"] = str(product["_id"])
        curator_meta = product.get("curator") or {}
        product["_manual_merge_flag"] = bool(curator_meta.get("manual_merge_flag"))
        product["_missing_photo"] = _is_missing_photo(product)
        _si = _size_info_from_doc(product)
        product["_size_label"] = _format_size(_si)
        product["_pack_label"] = _format_pack(_si)
        product["_measure_label"] = _format_measure(_si)

    store_pipeline = [
        {"$group": {"_id": "$store_id", "store_name": {"$first": "$store_name"}}},
        {"$sort": {"store_name": 1}},
    ]
    stores = list(mongo.products.aggregate(store_pipeline))

    return {
        "products": products,
        "stores": stores,
        "total": total,
        "total_pages": total_pages,
        "page": page,
        "limit": limit,
        "query": q,
        "active_store": store,
        "active_flagged": flagged,
        "active_photo": photo,
    }


# ---- Dashboard ----


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "page_title": "Dashboard",
            "active_nav": "dashboard",
        },
    )


@router.get("/dashboard/sections/stats", response_class=HTMLResponse)
async def dashboard_stats_partial(request: Request):
    mongo = _get_mongo()
    store_stats = _dashboard_store_stats(mongo)
    total_products = sum(s["count"] for s in store_stats)
    running_count = mongo.jobs.count_documents(
        {"status": {"$in": ["running", "stopping", "cancel_requested"]}}
    )
    return templates.TemplateResponse(
        "dashboard/_stats.html",
        {
            "request": request,
            "total_products": total_products,
            "store_stats": store_stats,
            "running_count": running_count,
        },
    )


@router.get("/dashboard/sections/store-stats", response_class=HTMLResponse)
async def dashboard_store_stats_partial(request: Request):
    mongo = _get_mongo()
    store_stats = _dashboard_store_stats(mongo)
    return templates.TemplateResponse(
        "dashboard/_store_stats.html",
        {
            "request": request,
            "store_stats": store_stats,
        },
    )


@router.get("/dashboard/sections/recent-jobs", response_class=HTMLResponse)
async def dashboard_recent_jobs_partial(request: Request):
    mongo = _get_mongo()
    recent_jobs = _dashboard_recent_jobs(mongo, limit=10)
    return templates.TemplateResponse(
        "dashboard/_recent_jobs.html",
        {
            "request": request,
            "recent_jobs": recent_jobs,
        },
    )


# ---- Products ----


@router.get("/products", response_class=HTMLResponse)
async def product_list(
    request: Request,
    q: str = Query("", alias="q"),
    store: str = Query("", alias="store"),
    flagged: str = Query("", alias="flagged"),
    photo: str = Query("", alias="photo"),
    page: int = Query(1),
    limit: int = Query(50),
    message: str = Query("", alias="message"),
    error: str = Query("", alias="error"),
):
    mongo = _get_mongo()
    context = _product_list_payload(
        mongo,
        q=q,
        store=store,
        flagged=flagged,
        photo=photo,
        page=page,
        limit=limit,
    )

    if (
        _is_hx_request(request)
        and request.headers.get("HX-Target") == "products-results"
    ):
        return templates.TemplateResponse(
            "products/_results.html",
            {
                "request": request,
                **context,
            },
        )

    if (
        _is_hx_request(request)
        and request.headers.get("HX-Target") == "product-table-body"
    ):
        return templates.TemplateResponse(
            "products/_rows.html",
            {
                "request": request,
                "products": context["products"],
            },
        )

    return templates.TemplateResponse(
        "products/list.html",
        {
            "request": request,
            "message": message,
            "error": error,
            "page_title": "Products",
            "active_nav": "products",
            **context,
        },
    )


@router.get("/products/sections/results", response_class=HTMLResponse)
async def product_results_partial(
    request: Request,
    q: str = Query("", alias="q"),
    store: str = Query("", alias="store"),
    flagged: str = Query("", alias="flagged"),
    photo: str = Query("", alias="photo"),
    page: int = Query(1),
    limit: int = Query(50),
):
    mongo = _get_mongo()
    context = _product_list_payload(
        mongo,
        q=q,
        store=store,
        flagged=flagged,
        photo=photo,
        page=page,
        limit=limit,
    )
    return templates.TemplateResponse(
        "products/_results.html",
        {
            "request": request,
            **context,
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
            "standard_categories": STANDARD_CATEGORIES,
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
        return _feedback_response(
            request,
            fallback_path="/admin/products/new",
            error="Store and product name are required",
        )

    sources = _decorate_sources(_load_sources())
    source_name_map = {source.get("source_id"): source.get("display_name") for source in sources}

    store_name = (
        str(form.get("store_name", "")).strip() or source_name_map.get(store_id) or store_id
    )
    brand_raw = str(form.get("brand", "")).strip() or None
    category_raw = str(form.get("category", "")).strip() or None
    image_url = str(form.get("image_url", "")).strip() or None
    product_url = str(form.get("url", "")).strip() or f"manual://{store_id}"
    size_hint_raw = str(form.get("size_hint", "")).strip()
    pack_qty_raw = str(form.get("pack_qty", "")).strip()
    # Combine pack qty and size hint: "6" + "330ml" → "6x330ml"
    if pack_qty_raw and size_hint_raw:
        size_hint = f"{pack_qty_raw}x{size_hint_raw}"
    elif pack_qty_raw:
        size_hint = f"{pack_qty_raw} pack"
    else:
        size_hint = size_hint_raw or name
    currency = str(form.get("currency", "JMD")).strip() or "JMD"

    price_raw = str(form.get("price", "")).strip()
    price: float | None = None
    if price_raw:
        try:
            price = float(price_raw)
        except ValueError:
            return _feedback_response(
                request,
                fallback_path="/admin/products/new",
                error="Price must be a number",
            )

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
    _mark_curator_snapshot_stale(
        mongo,
        reason="Products changed. Run Scan Now to refresh curator snapshot.",
    )
    if _is_hx_request(request):
        return _hx_trigger_response(
            message=f"Saved product '{name}'",
            events=["admin:products-refresh", "admin:dashboard-refresh"],
            reset_form_id="new-product-form",
        )
    return RedirectResponse(f"/admin/products/{oid}", status_code=303)


def _product_detail_payload(mongo: MongoService, oid: ObjectId) -> dict[str, Any] | None:
    product = mongo.products.find_one({"_id": oid}, {"embedding": 0})
    if not product:
        return None
    product["_id"] = str(product["_id"])
    size_info = _size_info_from_doc(product)
    size_hint = _size_hint_from_size(size_info) or str(product.get("name") or "")
    curator_meta = product.get("curator") or {}
    return {
        "product": product,
        "size_hint": size_hint,
        "size_summary": _format_size(size_info),
        "size_pack_count": size_info.pack_count,
        "manual_merge_flag": bool(curator_meta.get("manual_merge_flag")),
        "manual_merge_note": str(curator_meta.get("manual_merge_note") or ""),
        "missing_photo": _is_missing_photo(product),
        "standard_categories": STANDARD_CATEGORIES,
    }


@router.get("/products/{product_id}", response_class=HTMLResponse)
async def product_detail(
    request: Request,
    product_id: str,
    message: str = Query("", alias="message"),
    error: str = Query("", alias="error"),
):
    oid = _object_id_or_none(product_id)
    if oid is None:
        return HTMLResponse("Invalid product ID", status_code=400)
    mongo = _get_mongo()
    payload = _product_detail_payload(mongo, oid)
    if not payload:
        return HTMLResponse("Product not found", status_code=404)
    return templates.TemplateResponse(
        "products/detail.html",
        {
            "request": request,
            **payload,
            "message": message,
            "error": error,
            "page_title": f"Edit: {payload['product'].get('name', '')[:40]}",
            "active_nav": "products",
        },
    )


@router.get("/products/{product_id}/section/content", response_class=HTMLResponse)
async def product_detail_content_partial(request: Request, product_id: str):
    oid = _object_id_or_none(product_id)
    if oid is None:
        return HTMLResponse("Invalid product ID", status_code=400)
    mongo = _get_mongo()
    payload = _product_detail_payload(mongo, oid)
    if not payload:
        return HTMLResponse("Product not found", status_code=404)
    return templates.TemplateResponse(
        "products/_detail_content.html",
        {
            "request": request,
            **payload,
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
    size_hint_raw = str(form.get("size_hint", "")).strip()
    pack_qty_raw = str(form.get("pack_qty", "")).strip()
    # Combine pack qty and size hint: "6" + "330ml" → "6x330ml"
    if pack_qty_raw and size_hint_raw:
        size_hint = f"{pack_qty_raw}x{size_hint_raw}"
    elif pack_qty_raw:
        size_hint = f"{pack_qty_raw} pack"
    else:
        size_hint = size_hint_raw or name

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
    _mark_curator_snapshot_stale(
        mongo,
        reason="Products changed. Run Scan Now to refresh curator snapshot.",
    )
    if _is_hx_request(request):
        return _hx_trigger_response(
            message="Product updated",
            events=["admin:product-detail-refresh", "admin:products-refresh"],
        )
    return RedirectResponse(f"/admin/products/{product_id}", status_code=303)


@router.post("/products/{product_id}/flag-merge", response_class=HTMLResponse)
async def product_flag_merge(request: Request, product_id: str):
    oid = _object_id_or_none(product_id)
    if oid is None:
        if _is_hx_request(request):
            return _hx_trigger_response(error="Invalid product ID")
        return _redirect_back(request, "/admin/products", error="Invalid product ID")

    mongo = _get_mongo()
    product = mongo.products.find_one({"_id": oid}, {"_id": 1})
    if not product:
        if _is_hx_request(request):
            return _hx_trigger_response(error="Product not found")
        return _redirect_back(request, "/admin/products", error="Product not found")

    form = await request.form()
    note = str(form.get("note", "")).strip()
    updates: dict[str, Any] = {
        "curator.manual_merge_flag": True,
        "curator.flagged_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
    }
    if note:
        updates["curator.manual_merge_note"] = note

    mongo.products.update_one({"_id": oid}, {"$set": updates})
    _mark_curator_snapshot_stale(
        mongo,
        reason="Merge flags changed. Run Scan Now to refresh curator snapshot.",
    )
    if _is_hx_request(request):
        return _hx_trigger_response(
            message="Product flagged for merge review",
            events=[
                "admin:products-refresh",
                "admin:product-detail-refresh",
                "admin:curator-refresh",
            ],
        )
    return _redirect_back(request, "/admin/products", message="Product flagged for merge review")


@router.post("/products/{product_id}/unflag-merge", response_class=HTMLResponse)
async def product_unflag_merge(request: Request, product_id: str):
    oid = _object_id_or_none(product_id)
    if oid is None:
        if _is_hx_request(request):
            return _hx_trigger_response(error="Invalid product ID")
        return _redirect_back(request, "/admin/products", error="Invalid product ID")

    mongo = _get_mongo()
    product = mongo.products.find_one({"_id": oid}, {"_id": 1})
    if not product:
        if _is_hx_request(request):
            return _hx_trigger_response(error="Product not found")
        return _redirect_back(request, "/admin/products", error="Product not found")

    mongo.products.update_one(
        {"_id": oid},
        {
            "$unset": {
                "curator.manual_merge_flag": "",
                "curator.manual_merge_note": "",
                "curator.flagged_at": "",
            },
            "$set": {"updated_at": datetime.utcnow()},
        },
    )
    _mark_curator_snapshot_stale(
        mongo,
        reason="Merge flags changed. Run Scan Now to refresh curator snapshot.",
    )
    if _is_hx_request(request):
        return _hx_trigger_response(
            message="Merge flag removed",
            events=[
                "admin:products-refresh",
                "admin:product-detail-refresh",
                "admin:curator-refresh",
            ],
        )
    return _redirect_back(request, "/admin/products", message="Merge flag removed")


@router.delete("/products/{product_id}", response_class=HTMLResponse)
async def product_delete(request: Request, product_id: str):
    oid = _object_id_or_none(product_id)
    if oid is None:
        if _is_hx_request(request):
            return _hx_trigger_response(error="Invalid product ID", status_code=400)
        return HTMLResponse("", status_code=400)
    mongo = _get_mongo()
    mongo.products.delete_one({"_id": oid})
    _mark_curator_snapshot_stale(
        mongo,
        reason="Products changed. Run Scan Now to refresh curator snapshot.",
    )
    if _is_hx_request(request):
        return _hx_trigger_response(
            message="Product deleted",
            events=["admin:products-refresh", "admin:dashboard-refresh", "admin:curator-refresh"],
        )
    return HTMLResponse("")


@router.post("/products/{product_id}/delete", response_class=HTMLResponse)
async def product_delete_post(request: Request, product_id: str):
    oid = _object_id_or_none(product_id)
    if oid is None:
        return _feedback_response(
            request,
            fallback_path="/admin/products",
            error="Invalid product ID",
            events=["admin:products-refresh"],
        )
    mongo = _get_mongo()
    mongo.products.delete_one({"_id": oid})
    _mark_curator_snapshot_stale(
        mongo,
        reason="Products changed. Run Scan Now to refresh curator snapshot.",
    )
    if (
        _is_hx_request(request)
        and request.headers.get("HX-Target") == "product-detail-content"
    ):
        response = templates.TemplateResponse(
            "products/_deleted_notice.html",
            {
                "request": request,
            },
        )
        response.headers["HX-Trigger"] = json.dumps(
            {
                "admin:notify": {"level": "success", "message": "Product deleted"},
                "admin:products-refresh": True,
                "admin:dashboard-refresh": True,
                "admin:curator-refresh": True,
            }
        )
        return response
    return _feedback_response(
        request,
        fallback_path="/admin/products",
        message="Product deleted",
        events=["admin:products-refresh", "admin:dashboard-refresh", "admin:curator-refresh"],
    )


# ---- Stores ----


def _stores_payload(mongo: MongoService) -> list[dict[str, Any]]:
    sources = _load_sources()
    pipeline = [{"$group": {"_id": "$store_id", "count": {"$sum": 1}}}]
    counts = {row["_id"]: row["count"] for row in mongo.products.aggregate(pipeline)}
    return _decorate_sources(sources, counts)


@router.get("/stores", response_class=HTMLResponse)
async def store_list(
    request: Request,
    message: str = Query("", alias="message"),
    error: str = Query("", alias="error"),
):
    return templates.TemplateResponse(
        "stores/list.html",
        {
            "request": request,
            "message": message,
            "error": error,
            "page_title": "Stores",
            "active_nav": "stores",
        },
    )


@router.get("/stores/sections/table", response_class=HTMLResponse)
async def store_table_partial(request: Request):
    mongo = _get_mongo()
    return templates.TemplateResponse(
        "stores/_table.html",
        {
            "request": request,
            "sources": _stores_payload(mongo),
        },
    )


@router.post("/stores", response_class=HTMLResponse)
async def store_create(
    request: Request,
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
        return _feedback_response(
            request,
            fallback_path="/admin/stores",
            error="Source ID, store name, and base URL are required",
        )

    sources = _load_sources()
    if any(source.get("source_id") == normalized_source_id for source in sources):
        return _feedback_response(
            request,
            fallback_path="/admin/stores",
            error=f"Store source '{normalized_source_id}' already exists",
        )

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
    return _feedback_response(
        request,
        fallback_path="/admin/stores",
        message=f"Added store source '{normalized_source_id}'",
        events=["admin:stores-refresh", "admin:dashboard-refresh"],
        reset_form_id="stores-create-form",
    )


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
async def start_scrape(request: Request, store_id: str = Form(...)):
    mongo = _get_mongo()
    existing = mongo.jobs.find_one(
        {
            "store_id": store_id,
            "status": {"$in": ["running", "stopping", "cancel_requested"]},
        }
    )
    if existing:
        return _feedback_response(
            request,
            fallback_path="/admin/scrapes",
            error=f"A scrape is already active for '{store_id}'",
            events=["admin:scrapes-refresh"],
        )

    thread = threading.Thread(
        target=run_scrape,
        kwargs={"store_id": store_id, "use_playwright": True},
        daemon=True,
    )
    thread.start()
    LOGGER.info("Started scrape for %s in background thread", store_id)

    return _feedback_response(
        request,
        fallback_path="/admin/scrapes",
        message=f"Started scrape for '{store_id}'",
        events=["admin:scrapes-refresh", "admin:dashboard-refresh"],
        reset_form_id="scrape-start-form",
    )


@router.post("/scrapes/{job_id}/stop", response_class=HTMLResponse)
async def stop_scrape(request: Request, job_id: str):
    if not ObjectId.is_valid(job_id):
        return _feedback_response(
            request,
            fallback_path="/admin/scrapes",
            error="Invalid job ID",
            events=["admin:scrapes-refresh"],
        )

    mongo = _get_mongo()
    oid = ObjectId(job_id)
    job = mongo.jobs.find_one({"_id": oid})
    if not job:
        return _feedback_response(
            request,
            fallback_path="/admin/scrapes",
            error="Scrape job not found",
            events=["admin:scrapes-refresh"],
        )

    status = job.get("status")
    if status == "cancel_requested":
        return _feedback_response(
            request,
            fallback_path="/admin/scrapes",
            error="Cancel already requested for this scrape",
            events=["admin:scrapes-refresh"],
        )

    if status not in {"running", "stopping"}:
        return _feedback_response(
            request,
            fallback_path="/admin/scrapes",
            error="Only running scrapes can be stopped",
            events=["admin:scrapes-refresh"],
        )

    if status == "stopping":
        return _feedback_response(
            request,
            fallback_path="/admin/scrapes",
            message="Stop already requested for this scrape",
            events=["admin:scrapes-refresh"],
        )

    mongo.update_job(oid, {"status": "stopping"})
    LOGGER.info("Stop requested for scrape job %s", job_id)

    return _feedback_response(
        request,
        fallback_path="/admin/scrapes",
        message="Stop requested. The scraper will stop safely on its next checkpoint",
        events=["admin:scrapes-refresh", "admin:dashboard-refresh"],
    )


@router.post("/scrapes/{job_id}/cancel", response_class=HTMLResponse)
async def cancel_scrape(request: Request, job_id: str):
    if not ObjectId.is_valid(job_id):
        return _feedback_response(
            request,
            fallback_path="/admin/scrapes",
            error="Invalid job ID",
            events=["admin:scrapes-refresh"],
        )

    mongo = _get_mongo()
    oid = ObjectId(job_id)
    job = mongo.jobs.find_one({"_id": oid})
    if not job:
        return _feedback_response(
            request,
            fallback_path="/admin/scrapes",
            error="Scrape job not found",
            events=["admin:scrapes-refresh"],
        )

    status = job.get("status")
    if status == "cancel_requested":
        return _feedback_response(
            request,
            fallback_path="/admin/scrapes",
            message="Cancel already requested for this scrape",
            events=["admin:scrapes-refresh"],
        )

    if status not in {"running", "stopping"}:
        return _feedback_response(
            request,
            fallback_path="/admin/scrapes",
            error="Only running scrapes can be cancelled",
            events=["admin:scrapes-refresh"],
        )

    mongo.update_job(oid, {"status": "cancel_requested"})
    LOGGER.info("Cancel requested for scrape job %s", job_id)

    return _feedback_response(
        request,
        fallback_path="/admin/scrapes",
        message="Cancel requested. The scraper will stop and mark the job as cancelled.",
        events=["admin:scrapes-refresh", "admin:dashboard-refresh"],
    )


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


@router.get("/curator/review/{normalized_name:path}", response_class=HTMLResponse)
async def curator_review(
    request: Request,
    normalized_name: str,
    message: str = Query("", alias="message"),
    error: str = Query("", alias="error"),
):
    """Review page showing all products that share a normalized_name, with inline actions."""
    mongo = _get_mongo()
    key = _normalize_cluster_name(normalized_name)
    if not key:
        return _feedback_response(
            request,
            fallback_path="/admin/curator",
            error="Invalid product name",
        )
    docs = list(mongo.products.find({"normalized_name": key}))
    if not docs:
        return _feedback_response(
            request,
            fallback_path="/admin/curator",
            error=f"No products found for '{normalized_name}'",
        )

    # Enrich each doc with display helpers
    brands: set[str] = set()
    categories: set[str] = set()
    stores: set[str] = set()
    for doc in docs:
        doc["_id"] = str(doc["_id"])
        si = _size_info_from_doc(doc)
        doc["_pack_label"] = _format_pack(si)
        doc["_measure_label"] = _format_measure(si)
        doc["_size_label"] = _format_size(si)
        doc["_has_image"] = bool(
            doc.get("image_url")
            and not str(doc.get("image_url", "")).startswith("data:image/svg")
        )
        brand = doc.get("brand")
        if brand:
            brands.add(brand)
        cat = doc.get("category")
        if cat:
            categories.add(cat)
        store = doc.get("store_name") or doc.get("store_id") or ""
        if store:
            stores.add(store)

    # Detect conflict types present
    has_brand_conflict = len(brands) > 1
    has_category_conflict = len(categories) > 1
    size_sigs = _size_signatures_from_docs(docs)
    has_mixed_sizes = len(size_sigs) > 1
    can_merge = len(docs) > 1 and not has_mixed_sizes

    # Compute merge preview (what the result would look like)
    merge_preview: dict[str, Any] | None = None
    if can_merge:
        preview_docs = sorted(
            docs,
            key=lambda d: (
                len(d.get("location_prices") or []),
                1 if d.get("estimated_price") is not None else 0,
            ),
            reverse=True,
        )
        all_prices: list[dict[str, Any]] = []
        for d in preview_docs:
            all_prices = _merge_location_prices(
                all_prices, d.get("location_prices") or []
            )
        merge_preview = {
            "name": preview_docs[0].get("name", key),
            "brand": normalize_brand(_dominant_text_value(preview_docs, "brand")),
            "category": normalize_category(
                _dominant_text_value(preview_docs, "category")
            ),
            "estimated_price": _average_price(all_prices),
            "location_count": len(all_prices),
            "store_count": len(stores),
            "image_url": next(
                (
                    d.get("image_url")
                    for d in preview_docs
                    if d.get("image_url")
                    and not str(d["image_url"]).startswith("data:image/svg")
                ),
                None,
            ),
        }

    return templates.TemplateResponse(
        "curator/review.html",
        {
            "request": request,
            "normalized_name": key,
            "products": docs,
            "product_count": len(docs),
            "brands": sorted(brands),
            "categories": sorted(categories),
            "stores": sorted(stores),
            "has_brand_conflict": has_brand_conflict,
            "has_category_conflict": has_category_conflict,
            "has_mixed_sizes": has_mixed_sizes,
            "can_merge": can_merge,
            "merge_preview": merge_preview,
            "standard_categories": STANDARD_CATEGORIES,
            "message": message,
            "error": error,
            "page_title": f"Review: {docs[0].get('name', key)}",
            "active_nav": "curator",
        },
    )


@router.get("/curator", response_class=HTMLResponse)
async def curator_page(
    request: Request,
    limit: int = Query(25, ge=5, le=100),
    message: str = Query("", alias="message"),
    error: str = Query("", alias="error"),
):
    return templates.TemplateResponse(
        "curator/list.html",
        {
            "request": request,
            "limit": limit,
            "message": message,
            "error": error,
            "page_title": "AI Curator",
            "active_nav": "curator",
        },
    )


@router.post("/curator/scan", response_class=HTMLResponse)
async def curator_scan_now(
    request: Request,
    limit: int = Form(25),
):
    mongo = _get_mongo()
    snapshot = _refresh_curator_snapshot(mongo, row_cap=CURATOR_SNAPSHOT_ROW_CAP)
    generated_at = snapshot.get("generated_at")
    generated_text = (
        f"{generated_at.strftime('%Y-%m-%d %H:%M:%S')} UTC"
        if isinstance(generated_at, datetime)
        else "just now"
    )
    row_cap = snapshot.get("row_cap", CURATOR_SNAPSHOT_ROW_CAP)
    return _curator_feedback(
        request,
        limit=limit,
        message=(
            f"Curator scan complete. Cached up to {row_cap} "
            f"rows per section at {generated_text}."
        ),
        mark_snapshot_stale=False,
    )


_SECTION_EVENTS: dict[str, list[str]] = {
    "conflicts": ["admin:curator-refresh-conflicts", "admin:curator-refresh-summary"],
    "manual-flags": ["admin:curator-refresh-flags", "admin:curator-refresh-summary"],
    "name-size": ["admin:curator-refresh-name-size", "admin:curator-refresh-summary"],
    "missing-photos": ["admin:curator-refresh-photos", "admin:curator-refresh-summary"],
    "price-anomalies": ["admin:curator-refresh-price-anomalies", "admin:curator-refresh-summary"],
}

_SECTION_LABELS: dict[str, str] = {
    "conflicts": "Conflicts (Match Key / Brand / Category)",
    "manual-flags": "Manual Merge Flags",
    "name-size": "Name & Size Normalization",
    "missing-photos": "Missing Photos",
    "price-anomalies": "Price Anomalies",
}


@router.post("/curator/scan/{section}", response_class=HTMLResponse)
async def curator_scan_section(
    request: Request,
    section: str,
    limit: int = Form(25),
):
    if section not in _SECTION_EVENTS:
        return _curator_feedback(request, limit=limit, error=f"Unknown section: {section}")
    mongo = _get_mongo()
    _refresh_curator_section(mongo, section, row_cap=CURATOR_SNAPSHOT_ROW_CAP)
    label = _SECTION_LABELS.get(section, section)
    events = _SECTION_EVENTS[section]
    if _is_hx_request(request):
        return _hx_trigger_response(
            message=f"Scanned {label}.",
            events=events,
        )
    return _feedback_response(
        request,
        fallback_path="/admin/curator",
        message=f"Scanned {label}.",
        events=["admin:curator-refresh"],
        extra_params={"limit": limit},
    )


@router.get("/curator/summary", response_class=HTMLResponse)
async def curator_summary_partial(
    request: Request,
    limit: int = Query(25, ge=5, le=100),
):
    mongo = _get_mongo()
    snapshot = _load_curator_snapshot(mongo, ensure=True)
    row_limit = _curator_row_limit(limit)
    match_key_conflicts = _snapshot_rows(snapshot, "match_key_conflicts", row_limit)
    brand_conflicts = _snapshot_rows(snapshot, "brand_conflicts", row_limit)
    category_conflicts = _snapshot_rows(snapshot, "category_conflicts", row_limit)
    name_cleanup_candidates = _snapshot_rows(snapshot, "name_cleanup_candidates", row_limit)

    return templates.TemplateResponse(
        "curator/_summary.html",
        {
            "request": request,
            "row_limit": row_limit,
            "snapshot_generated_at": snapshot.get("generated_at"),
            "snapshot_stale": bool(snapshot.get("stale")),
            "snapshot_stale_reason": str(snapshot.get("stale_reason") or ""),
            "snapshot_row_cap": int(snapshot.get("row_cap") or CURATOR_SNAPSHOT_ROW_CAP),
            "match_key_conflict_count": len(match_key_conflicts),
            "brand_conflict_count": len(brand_conflicts),
            "category_conflict_count": len(category_conflicts),
            "flagged_product_total": int(snapshot.get("flagged_product_total") or 0),
            "name_cleanup_total": len(name_cleanup_candidates),
            "missing_photo_total": int(snapshot.get("missing_photo_total") or 0),
        },
    )


@router.get("/curator/sections/manual-flags", response_class=HTMLResponse)
async def curator_manual_flags_partial(
    request: Request,
    limit: int = Query(25, ge=5, le=100),
):
    mongo = _get_mongo()
    snapshot = _load_curator_snapshot(mongo, ensure=True)
    row_limit = _curator_row_limit(limit)
    manual_merge_flags = _snapshot_rows(snapshot, "manual_merge_flags", row_limit)
    flagged_product_total = int(snapshot.get("flagged_product_total") or 0)
    return templates.TemplateResponse(
        "curator/_manual_flags.html",
        {
            "request": request,
            "manual_merge_flags": manual_merge_flags,
            "flagged_product_total": flagged_product_total,
            "limit": row_limit,
        },
    )


@router.get("/curator/sections/conflicts", response_class=HTMLResponse)
async def curator_conflicts_partial(
    request: Request,
    limit: int = Query(25, ge=5, le=100),
):
    mongo = _get_mongo()
    snapshot = _load_curator_snapshot(mongo, ensure=True)
    row_limit = _curator_row_limit(limit)
    brand_conflicts = _snapshot_rows(snapshot, "brand_conflicts", row_limit)
    category_conflicts = _snapshot_rows(snapshot, "category_conflicts", row_limit)
    match_key_conflicts = _snapshot_rows(snapshot, "match_key_conflicts", row_limit)
    return templates.TemplateResponse(
        "curator/_conflicts.html",
        {
            "request": request,
            "brand_conflicts": brand_conflicts,
            "category_conflicts": category_conflicts,
            "match_key_conflicts": match_key_conflicts,
            "standard_categories": STANDARD_CATEGORIES,
            "limit": row_limit,
        },
    )


@router.get("/curator/sections/name-size", response_class=HTMLResponse)
async def curator_name_size_partial(
    request: Request,
    limit: int = Query(25, ge=5, le=100),
):
    mongo = _get_mongo()
    snapshot = _load_curator_snapshot(mongo, ensure=True)
    row_limit = _curator_row_limit(limit)
    name_cleanup_candidates = _snapshot_rows(snapshot, "name_cleanup_candidates", row_limit)
    name_cleanup_total = int(snapshot.get("name_cleanup_total") or 0)
    return templates.TemplateResponse(
        "curator/_name_size.html",
        {
            "request": request,
            "name_cleanup_candidates": name_cleanup_candidates,
            "name_cleanup_total": name_cleanup_total,
            "limit": row_limit,
        },
    )


@router.get("/curator/sections/missing-photos", response_class=HTMLResponse)
async def curator_missing_photos_partial(
    request: Request,
    limit: int = Query(25, ge=5, le=100),
):
    mongo = _get_mongo()
    snapshot = _load_curator_snapshot(mongo, ensure=True)
    row_limit = _curator_row_limit(limit)
    missing_photo_candidates = _snapshot_rows(snapshot, "missing_photo_candidates", row_limit)
    missing_photo_total = int(snapshot.get("missing_photo_total") or 0)
    return templates.TemplateResponse(
        "curator/_missing_photos.html",
        {
            "request": request,
            "missing_photo_candidates": missing_photo_candidates,
            "missing_photo_total": missing_photo_total,
            "limit": row_limit,
        },
    )


@router.get("/curator/sections/price-anomalies", response_class=HTMLResponse)
async def curator_price_anomalies_partial(
    request: Request,
    limit: int = Query(25, ge=5, le=100),
):
    mongo = _get_mongo()
    snapshot = _load_curator_snapshot(mongo, ensure=True)
    row_limit = _curator_row_limit(limit)
    price_anomalies = _snapshot_rows(snapshot, "price_anomalies", row_limit)
    return templates.TemplateResponse(
        "curator/_price_anomalies.html",
        {
            "request": request,
            "price_anomalies": price_anomalies,
            "limit": row_limit,
        },
    )


@router.get("/curator/sections/actions", response_class=HTMLResponse)
async def curator_actions_partial(
    request: Request,
    limit: int = Query(25, ge=5, le=100),
):
    mongo = _get_mongo()
    recent_actions = _list_recent_curator_actions(mongo)
    return templates.TemplateResponse(
        "curator/_actions.html",
        {
            "request": request,
            "recent_actions": recent_actions,
            "limit": limit,
        },
    )


def _curator_feedback(
    request: Request,
    *,
    limit: int,
    message: str | None = None,
    error: str | None = None,
    mongo: MongoService | None = None,
    refresh_snapshot: bool = True,
    mark_snapshot_stale: bool = True,
    hx_body: str | None = None,
) -> HTMLResponse | RedirectResponse:
    if mark_snapshot_stale and message and not error:
        active_mongo = mongo or _get_mongo()
        if refresh_snapshot:
            try:
                _refresh_curator_snapshot(
                    active_mongo,
                    row_cap=CURATOR_SNAPSHOT_ROW_CAP,
                )
            except Exception as exc:  # pragma: no cover
                LOGGER.warning("Unable to refresh curator snapshot after action: %s", exc)
                _mark_curator_snapshot_stale(
                    active_mongo,
                    reason="Curator action changed data. Run Scan Now to refresh snapshot.",
                )
        else:
            _mark_curator_snapshot_stale(
                active_mongo,
                reason="Curator action changed data. Run Scan Now to refresh snapshot.",
            )
    if hx_body is not None and _is_hx_request(request):
        return _hx_trigger_response(
            message=message,
            error=error,
            events=["admin:curator-refresh-summary", "admin:dashboard-refresh"],
            body=hx_body,
        )
    return _feedback_response(
        request,
        fallback_path="/admin/curator",
        message=message,
        error=error,
        events=["admin:curator-refresh", "admin:products-refresh", "admin:dashboard-refresh"],
        extra_params={"limit": limit},
    )


@router.post("/curator/merge", response_class=HTMLResponse)
async def curator_merge_cluster(
    request: Request,
    normalized_name: str = Form(...),
    limit: int = Form(25),
):
    mongo = _get_mongo()
    key = _normalize_cluster_name(normalized_name)
    if not key:
        return _curator_feedback(request, limit=limit, error="Invalid cluster name")
    try:
        result = _execute_merge_cluster(mongo, key)
    except ValueError as exc:
        return _curator_feedback(request, limit=limit, error=str(exc))

    return _curator_feedback(
        request,
        limit=limit,
        mongo=mongo,
        hx_body="",
        message=(
            f"Merged {result['docs']} products for '{result['canonical_name']}'. "
            f"Removed {result['duplicates_removed']} duplicates. Action #{result['action_id']}."
        ),
    )


@router.post("/curator/normalize-brand", response_class=HTMLResponse)
async def curator_normalize_brand(
    request: Request,
    normalized_name: str = Form(...),
    limit: int = Form(25),
):
    mongo = _get_mongo()
    key = _normalize_cluster_name(normalized_name)
    if not key:
        return _curator_feedback(request, limit=limit, error="Invalid cluster name")
    try:
        result = _execute_normalize_brand_cluster(mongo, key)
    except ValueError as exc:
        return _curator_feedback(request, limit=limit, error=str(exc))

    return _curator_feedback(
        request,
        limit=limit,
        mongo=mongo,
        hx_body="",
        message=(
            f"Normalized brand to '{result['dominant_brand']}' for {result['docs']} products "
            f"({result['updated']} updated). Action #{result['action_id']}."
        ),
    )


@router.post("/curator/normalize-category", response_class=HTMLResponse)
async def curator_normalize_category(
    request: Request,
    normalized_name: str = Form(...),
    target_category: str = Form(""),
    limit: int = Form(25),
):
    mongo = _get_mongo()
    key = _normalize_cluster_name(normalized_name)
    if not key:
        return _curator_feedback(request, limit=limit, error="Invalid cluster name")
    try:
        result = _execute_normalize_category_cluster(
            mongo,
            key,
            target_category=target_category.strip() or None,
        )
    except ValueError as exc:
        return _curator_feedback(request, limit=limit, error=str(exc))

    return _curator_feedback(
        request,
        limit=limit,
        mongo=mongo,
        hx_body="",
        message=(
            f"Normalized category to '{result['dominant_category']}' for {result['docs']} products "
            f"({result['updated']} updated). Action #{result['action_id']}."
        ),
    )


@router.post("/curator/normalize-name-size", response_class=HTMLResponse)
async def curator_normalize_name_size(
    request: Request,
    product_id: str = Form(...),
    limit: int = Form(25),
):
    oid = _object_id_or_none(product_id)
    if oid is None:
        return _curator_feedback(request, limit=limit, error="Invalid product ID")

    mongo = _get_mongo()
    try:
        result = _execute_normalize_name_size_product(mongo, oid)
    except ValueError as exc:
        return _curator_feedback(request, limit=limit, error=str(exc))

    return _curator_feedback(
        request,
        limit=limit,
        mongo=mongo,
        hx_body="",
        message=(
            f"Normalized '{result['original_name']}' -> '{result['cleaned_name']}'. "
            f"Size now {result['size_label']}. Action #{result['action_id']}."
        ),
    )


@router.post("/curator/flags/clear", response_class=HTMLResponse)
async def curator_clear_manual_flags(
    request: Request,
    normalized_name: str = Form(...),
    limit: int = Form(25),
):
    key = _normalize_cluster_name(normalized_name)
    if not key:
        return _curator_feedback(request, limit=limit, error="Invalid cluster name")

    mongo = _get_mongo()
    try:
        result = _execute_clear_manual_flags_cluster(mongo, key)
    except ValueError as exc:
        return _curator_feedback(request, limit=limit, error=str(exc))

    return _curator_feedback(
        request,
        limit=limit,
        mongo=mongo,
        hx_body="",
        message=(
            f"Cleared {result['cleared']} manual flags for '{key}'. "
            f"Action #{result['action_id']}."
        ),
    )


@router.post("/curator/merge-all", response_class=HTMLResponse)
async def curator_merge_all(request: Request, limit: int = Form(25)):
    form = await request.form()
    keys = _visible_cluster_keys(form)
    if not keys:
        return _curator_feedback(
            request,
            limit=limit,
            error="No visible match-key conflicts selected to merge",
        )

    mongo = _get_mongo()

    merged_clusters = 0
    merged_docs = 0
    removed_docs = 0
    skipped = 0
    for key in keys:
        try:
            result = _execute_merge_cluster(mongo, key)
        except ValueError:
            skipped += 1
            continue
        merged_clusters += 1
        merged_docs += int(result["docs"])
        removed_docs += int(result["duplicates_removed"])

    if merged_clusters == 0:
        return _curator_feedback(
            request,
            limit=limit,
            error="No clusters were eligible for merge-all",
        )

    return _curator_feedback(
        request,
        limit=limit,
        mongo=mongo,
        hx_body=_EMPTY_ROW_MATCH_KEY,
        message=(
            f"Merged {merged_clusters} match-key clusters ({merged_docs} docs, "
            f"removed {removed_docs}, skipped {skipped})."
        ),
    )


@router.post("/curator/flags/merge-all", response_class=HTMLResponse)
async def curator_merge_all_flagged(request: Request, limit: int = Form(25)):
    form = await request.form()
    keys = _visible_cluster_keys(form)
    if not keys:
        return _curator_feedback(
            request,
            limit=limit,
            error="No visible manually flagged clusters selected to merge",
        )

    mongo = _get_mongo()

    merged_clusters = 0
    merged_docs = 0
    removed_docs = 0
    skipped = 0
    for key in keys:
        try:
            result = _execute_merge_cluster(mongo, key)
        except ValueError:
            skipped += 1
            continue
        merged_clusters += 1
        merged_docs += int(result["docs"])
        removed_docs += int(result["duplicates_removed"])

    if merged_clusters == 0:
        return _curator_feedback(
            request,
            limit=limit,
            error="No manually flagged clusters were eligible to merge",
        )

    return _curator_feedback(
        request,
        limit=limit,
        mongo=mongo,
        hx_body=_EMPTY_ROW_FLAGS,
        message=(
            f"Merged {merged_clusters} manually flagged clusters ({merged_docs} docs, "
            f"removed {removed_docs}, skipped {skipped})."
        ),
    )


@router.post("/curator/flags/clear-all", response_class=HTMLResponse)
async def curator_clear_all_manual_flags(request: Request, limit: int = Form(25)):
    form = await request.form()
    keys = _visible_cluster_keys(form)
    if not keys:
        return _curator_feedback(
            request,
            limit=limit,
            error="No visible manual flags selected to clear",
        )

    mongo = _get_mongo()

    cleared = 0
    clusters = 0
    skipped = 0
    for key in keys:
        try:
            result = _execute_clear_manual_flags_cluster(mongo, key)
        except ValueError:
            skipped += 1
            continue
        clusters += 1
        cleared += int(result["cleared"])

    if clusters == 0:
        return _curator_feedback(
            request,
            limit=limit,
            error="No manual flags were eligible to clear",
        )

    return _curator_feedback(
        request,
        limit=limit,
        mongo=mongo,
        hx_body=_EMPTY_ROW_FLAGS,
        message=(
            f"Cleared {cleared} manual merge flags across {clusters} clusters "
            f"(skipped {skipped})."
        ),
    )


@router.post("/curator/normalize-brand-all", response_class=HTMLResponse)
async def curator_normalize_brand_all(request: Request, limit: int = Form(25)):
    form = await request.form()
    keys = _visible_cluster_keys(form)
    if not keys:
        return _curator_feedback(
            request,
            limit=limit,
            error="No visible brand conflicts selected to normalize",
        )

    mongo = _get_mongo()

    clusters = 0
    updated = 0
    skipped = 0
    for key in keys:
        try:
            result = _execute_normalize_brand_cluster(mongo, key)
        except ValueError:
            skipped += 1
            continue
        clusters += 1
        updated += int(result["updated"])

    if clusters == 0:
        return _curator_feedback(
            request,
            limit=limit,
            error="No brand conflicts were eligible to normalize",
        )

    return _curator_feedback(
        request,
        limit=limit,
        mongo=mongo,
        hx_body=_EMPTY_ROW_BRAND,
        message=(
            f"Normalized brand labels across {clusters} clusters "
            f"({updated} products updated, skipped {skipped})."
        ),
    )


@router.post("/curator/normalize-category-all", response_class=HTMLResponse)
async def curator_normalize_category_all(request: Request, limit: int = Form(25)):
    form = await request.form()
    keys = _visible_cluster_keys(form)
    if not keys:
        return _curator_feedback(
            request,
            limit=limit,
            error="No visible category conflicts selected to normalize",
        )

    mongo = _get_mongo()

    clusters = 0
    updated = 0
    skipped = 0
    for key in keys:
        try:
            result = _execute_normalize_category_cluster(mongo, key)
        except ValueError:
            skipped += 1
            continue
        clusters += 1
        updated += int(result["updated"])

    if clusters == 0:
        return _curator_feedback(
            request,
            limit=limit,
            error="No category conflicts were eligible to normalize",
        )

    return _curator_feedback(
        request,
        limit=limit,
        mongo=mongo,
        hx_body=_EMPTY_ROW_CATEGORY,
        message=(
            f"Normalized categories across {clusters} clusters "
            f"({updated} products updated, skipped {skipped})."
        ),
    )


@router.post("/curator/normalize-name-size-all", response_class=HTMLResponse)
async def curator_normalize_name_size_all(request: Request, limit: int = Form(25)):
    form = await request.form()
    product_ids = _visible_product_ids(form)
    if not product_ids:
        return _curator_feedback(
            request,
            limit=limit,
            error="No visible name/size rows selected to normalize",
        )

    mongo = _get_mongo()

    updated = 0
    skipped = 0
    for oid in product_ids:
        try:
            _execute_normalize_name_size_product(mongo, oid)
        except ValueError:
            skipped += 1
            continue
        updated += 1

    if updated == 0:
        return _curator_feedback(
            request,
            limit=limit,
            error="No name/size candidates were eligible to normalize",
        )

    return _curator_feedback(
        request,
        limit=limit,
        mongo=mongo,
        hx_body=_EMPTY_ROW_NAME_SIZE,
        message=f"Normalized names/sizes for {updated} products (skipped {skipped}).",
    )


@router.post("/curator/photos/queue-all", response_class=HTMLResponse)
async def curator_queue_missing_photos(request: Request, limit: int = Form(25)):
    form = await request.form()
    product_ids = _visible_product_ids(form)
    if not product_ids:
        return _curator_feedback(
            request,
            limit=limit,
            error="No visible missing-photo rows selected to queue",
        )

    mongo = _get_mongo()
    missing_photo_query = {
        "_id": {"$in": product_ids},
        "$or": [
            {"image_url": {"$in": [None, ""]}},
            {"image_url": {"$regex": "^data:", "$options": "i"}},
        ]
    }
    updates = {
        "$set": {
            "curator.photo_upload_needed": True,
            "curator.photo_flagged_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }
    }
    result = mongo.products.update_many(missing_photo_query, updates)
    updated = int(getattr(result, "modified_count", 0))
    matched = int(getattr(result, "matched_count", 0))
    if matched == 0:
        return _curator_feedback(
            request,
            limit=limit,
            error="No missing-photo products found to queue",
        )

    return _curator_feedback(
        request,
        limit=limit,
        mongo=mongo,
        hx_body=_EMPTY_ROW_PHOTOS,
        message=(
            f"Queued {updated} missing-photo products for upload triage "
            f"({matched} matched)."
        ),
    )


@router.post("/curator/actions/{action_id}/undo", response_class=HTMLResponse)
async def curator_undo_action(
    request: Request,
    action_id: str,
    limit: int = Form(25),
):
    if not ObjectId.is_valid(action_id):
        return _curator_feedback(request, limit=limit, error="Invalid action ID")

    mongo = _get_mongo()
    oid = ObjectId(action_id)
    action = mongo.curator_actions.find_one({"_id": oid})
    if not action:
        return _curator_feedback(request, limit=limit, error="Curator action not found")
    if action.get("status") == "undone":
        return _curator_feedback(request, limit=limit, error="Action has already been undone")

    before_docs = action.get("before_docs") or []
    after_docs = action.get("after_docs") or []
    if not before_docs:
        return _curator_feedback(
            request,
            limit=limit,
            error="Action has no stored snapshot to restore",
        )

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

    return _curator_feedback(
        request,
        limit=limit,
        mongo=mongo,
        message=(
            f"Undid action {action_id}: restored {restored} products, "
            f"removed {removed} products."
        ),
    )


# ── Dismiss / Restore ─────────────────────────────────────────────

_DISMISS_SECTION_MAP: dict[str, str] = {
    "conflicts": "conflicts",
    "manual-flags": "manual-flags",
    "name-size": "name-size",
    "missing-photos": "missing-photos",
    "price-anomalies": "price-anomalies",
}


@router.post("/curator/dismiss", response_class=HTMLResponse)
async def curator_dismiss(
    request: Request,
    normalized_name: str = Form(...),
    section: str = Form(...),
    limit: int = Form(25),
):
    """Dismiss a cluster from a curator section so it no longer shows up."""
    key = _normalize_cluster_name(normalized_name)
    if not key:
        return _curator_feedback(request, limit=limit, error="Invalid cluster name")
    if section not in _DISMISS_SECTION_MAP:
        return _curator_feedback(request, limit=limit, error=f"Unknown section: {section}")

    mongo = _get_mongo()
    mongo.curator_dismissed.update_one(
        {"normalized_name": key, "section": section},
        {
            "$set": {
                "normalized_name": key,
                "section": section,
                "dismissed_at": datetime.utcnow(),
            }
        },
        upsert=True,
    )

    events = _SECTION_EVENTS.get(section, [])
    if _is_hx_request(request):
        return _hx_trigger_response(
            message=f"Dismissed '{key}' from {_SECTION_LABELS.get(section, section)}.",
            events=events,
            body="",
        )
    return _feedback_response(
        request,
        fallback_path="/admin/curator",
        message=f"Dismissed '{key}' from {_SECTION_LABELS.get(section, section)}.",
        events=["admin:curator-refresh"],
        extra_params={"limit": limit},
    )


@router.post("/curator/restore", response_class=HTMLResponse)
async def curator_restore(
    request: Request,
    normalized_name: str = Form(...),
    section: str = Form(""),
    limit: int = Form(25),
):
    """Restore a previously dismissed cluster."""
    key = _normalize_cluster_name(normalized_name)
    if not key:
        return _curator_feedback(request, limit=limit, error="Invalid cluster name")

    mongo = _get_mongo()
    query: dict[str, Any] = {"normalized_name": key}
    if section:
        query["section"] = section
    mongo.curator_dismissed.delete_many(query)

    if _is_hx_request(request):
        return _hx_trigger_response(
            message=f"Restored '{key}' — it will reappear on next scan.",
            events=["admin:curator-refresh-summary"],
        )
    return _feedback_response(
        request,
        fallback_path="/admin/curator",
        message=f"Restored '{key}' — it will reappear on next scan.",
        events=["admin:curator-refresh"],
        extra_params={"limit": limit},
    )
