from typing import Optional

import typer

from agent.db.models import Product, SizeInfo
from agent.db.mongo import MongoService
from agent.embeddings.faiss_index import FaissIndex
from agent.embeddings.featurizer import build_embedding_text
from agent.embeddings.ollama_client import embed_sync
from agent.scraping.normalizer import build_match_key, normalize_name, parse_size
from agent.scraping.pipeline import run_scrape
from agent.utils.logging import get_logger

app = typer.Typer(help="Brite Shopping Agent CLI")
LOGGER = get_logger(__name__)


@app.command()
def scrape(
    store_id: Optional[str] = typer.Option(None, "--store-id"),
    url: Optional[str] = typer.Option(None, "--url"),
    force: bool = typer.Option(False, "--force"),
) -> None:
    job = run_scrape(store_id=store_id, url=url, use_playwright=False)
    typer.echo(job)


@app.command()
def reindex() -> None:
    mongo = MongoService()
    index = FaissIndex()
    products = mongo.list_products()
    vectors: list[list[float]] = []
    ids: list[str] = []
    for product in products:
        vector = product.get("embedding")
        if not vector:
            model = Product.model_validate(product)
            text = build_embedding_text(model)
            vector = embed_sync([text])[0]
            mongo.update_embedding(product["_id"], vector)
        vectors.append(vector)
        ids.append(str(product["_id"]))
    if vectors:
        index.reset(len(vectors[0]))
        index.add_vectors(vectors, ids)
    typer.echo({"indexed": len(vectors)})


@app.command()
def search(
    query: str = typer.Argument(..., help="Search query"),
    top_k: int = typer.Option(10, "--top-k", help="Number of results"),
) -> None:
    from fastapi.testclient import TestClient

    from agent.service.api import app as fastapi_app

    client = TestClient(fastapi_app)
    response = client.post("/search", json={"query": query, "k": top_k})
    typer.echo(response.json())


@app.command()
def backfill() -> None:
    """Backfill match_key, estimated_price, and fix location_id on legacy products."""
    mongo = MongoService()
    products = mongo.list_products()
    fixed = 0
    for doc in products:
        updates: dict = {}

        # Fix location_id: "default" → store_id
        location_prices = doc.get("location_prices", [])
        lp_changed = False
        for lp in location_prices:
            if lp.get("location_id") == "default":
                lp["location_id"] = doc["store_id"]
                lp_changed = True
            if not lp.get("store_name"):
                lp["store_name"] = doc.get("store_name")
                lp_changed = True
        if lp_changed:
            updates["location_prices"] = location_prices

        # Add estimated_price if missing
        if doc.get("estimated_price") is None and location_prices:
            amounts = [float(lp["amount"]) for lp in location_prices if lp.get("amount") is not None]
            if amounts:
                updates["estimated_price"] = round(sum(amounts) / len(amounts), 2)

        # Add match_key if missing
        if not doc.get("match_key"):
            normalized = doc.get("normalized_name") or normalize_name(doc.get("name", ""))
            brand = doc.get("brand")
            size_doc = doc.get("size", {})
            size = SizeInfo(value=size_doc.get("value"), unit=size_doc.get("unit"))
            updates["match_key"] = build_match_key(normalized, brand, size)

        if updates:
            mongo.products.update_one({"_id": doc["_id"]}, {"$set": updates})
            fixed += 1

    typer.echo(f"Backfilled {fixed}/{len(products)} products")


def main() -> None:
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
