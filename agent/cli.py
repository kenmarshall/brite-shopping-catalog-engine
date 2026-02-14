from typing import Optional

import typer

from agent.db.models import Product
from agent.db.mongo import MongoService
from agent.embeddings.faiss_index import FaissIndex
from agent.embeddings.featurizer import build_embedding_text
from agent.embeddings.ollama_client import embed_sync
from agent.scraping.pipeline import run_scrape
from agent.utils.logging import get_logger

app = typer.Typer(help="Brite Shopping Agent CLI")
LOGGER = get_logger(__name__)


@app.command()
def scrape(
    store_id: Optional[str] = typer.Option(None),
    url: Optional[str] = typer.Option(None),
    force: bool = typer.Option(False),
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
def search(q: str, k: int = 10) -> None:
    from fastapi.testclient import TestClient

    from agent.service.api import app as fastapi_app

    client = TestClient(fastapi_app)
    response = client.post("/search", json={"query": q, "k": k})
    typer.echo(response.json())


def main() -> None:
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
