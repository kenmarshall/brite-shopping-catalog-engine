from __future__ import annotations

from typing import Any

from bson import ObjectId
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from agent.config.settings import Settings, get_settings
from agent.db.mongo import MongoService
from agent.embeddings.faiss_index import FaissIndex
from agent.embeddings.featurizer import build_embedding_text
from agent.embeddings.ollama_client import embed_sync
from agent.scraping.pipeline import run_scrape
from agent.service.ranking import blend_scores, regex_search, text_search
from agent.utils.logging import get_logger

LOGGER = get_logger(__name__)

app = FastAPI(title="Brite Shopping Agent")


def get_mongo() -> MongoService:
    return MongoService()


def get_index() -> FaissIndex:
    return FaissIndex()


def get_app_settings() -> Settings:
    return get_settings()


class ScrapeRequest(BaseModel):
    store_id: str | None = None
    url: str | None = None
    force: bool = False


class SearchRequest(BaseModel):
    query: str
    k: int = 10
    prefer_tags: bool = False


class ReindexResponse(BaseModel):
    indexed: int


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/scrape/start")
def scrape_start(payload: ScrapeRequest) -> dict[str, Any]:
    if not payload.store_id and not payload.url:
        raise HTTPException(status_code=400, detail="store_id or url required")
    job = run_scrape(store_id=payload.store_id, url=payload.url, use_playwright=False)
    return job


@app.get("/scrape/jobs/{job_id}")
def scrape_job(job_id: str, mongo: MongoService = Depends(get_mongo)) -> dict[str, Any]:
    job = mongo.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    job["_id"] = str(job["_id"])
    return job


@app.post("/index/rebuild", response_model=ReindexResponse)
def rebuild_index(
    mongo: MongoService = Depends(get_mongo), index: FaissIndex = Depends(get_index)
) -> ReindexResponse:
    products = mongo.list_products({"embedding": {"$exists": True}})
    if not products:
        products = mongo.list_products()
    vectors: list[list[float]] = []
    ids: list[str] = []
    for product in products:
        vector = product.get("embedding")
        if not vector:
            product_model = build_product_model(product)
            text = build_embedding_text(product_model)
            vector = embed_sync([text])[0]
            mongo.update_embedding(product["_id"], vector)
        vectors.append(vector)
        ids.append(str(product["_id"]))
    if vectors:
        index.reset(len(vectors[0]))
        index.add_vectors(vectors, ids)
    return ReindexResponse(indexed=len(vectors))


class ProductOut(BaseModel):
    id: str
    name: str
    brand: str | None
    category: str | None
    tags: list[str]
    url: str
    image_url: str | None
    location_prices: list[dict[str, Any]]
    score: float


class SearchResponse(BaseModel):
    items: list[ProductOut]
    debug: dict[str, Any]


def build_product_model(data: dict[str, Any]):
    from agent.db.models import Product

    return Product.model_validate(data)


@app.post("/search", response_model=SearchResponse)
def search(
    payload: SearchRequest,
    mongo: MongoService = Depends(get_mongo),
    index: FaissIndex = Depends(get_index),
) -> SearchResponse:
    if not payload.query:
        raise HTTPException(status_code=400, detail="query required")
    products = mongo.products
    try:
        vector = embed_sync([payload.query])[0]
        vector_candidates = index.search(vector, k=payload.k)
    except Exception as exc:  # pragma: no cover
        LOGGER.warning("Vector search failed: %s", exc)
        vector_candidates = []

    text_candidates = text_search(products, payload.query, limit=payload.k)
    if not text_candidates:
        text_candidates = regex_search(products, payload.query, limit=payload.k)

    tag_candidates: dict[str, float] = {}
    if payload.prefer_tags:
        tag_matches = products.find({"tags": payload.query.lower()}).limit(payload.k)
        tag_candidates = {str(item["_id"]): 1.0 for item in tag_matches}

    blended = blend_scores(vector_candidates, text_candidates, tag_candidates)
    final_items: list[ProductOut] = []
    debug = {
        "vector_candidates": vector_candidates,
        "text_candidates": text_candidates,
        "tag_candidates": tag_candidates,
    }
    for product_id, score in blended[: payload.k]:
        obj_id = None
        try:
            obj_id = ObjectId(product_id)
        except Exception:
            obj_id = product_id  # type: ignore[assignment]
        product = products.find_one({"_id": obj_id})
        if not product and obj_id != product_id:
            product = products.find_one({"_id": product_id})
        if not product:
            continue
        final_items.append(
            ProductOut(
                id=str(product["_id"]),
                name=product.get("name"),
                brand=product.get("brand"),
                category=product.get("category"),
                tags=product.get("tags", []),
                url=product.get("url"),
                image_url=product.get("image_url"),
                location_prices=product.get("location_prices", []),
                score=score,
            )
        )
    return SearchResponse(items=final_items, debug=debug)


__all__ = ["app"]
